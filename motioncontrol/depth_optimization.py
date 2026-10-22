"""深度进程内的计算复用；保留输入尺寸、视频历史和姿态门限。"""
from __future__ import annotations

from types import MethodType


def blocking_gpu_wait(torch):
    """仅在深度子进程中让处理器休眠等待显卡；运行库不支持时沿用原行为。"""
    import ctypes
    from pathlib import Path
    import sys
    if sys.platform != "win32":
        return False
    try:
        library = next((Path(torch.__file__).parent / "lib").glob("cudart64_*.dll"), None)
        if library is None:
            return False
        runtime = ctypes.CDLL(str(library))
        runtime.cudaGetDeviceFlags.argtypes = [ctypes.POINTER(ctypes.c_uint)]
        runtime.cudaGetDeviceFlags.restype = ctypes.c_int
        runtime.cudaSetDeviceFlags.argtypes = [ctypes.c_uint]
        runtime.cudaSetDeviceFlags.restype = ctypes.c_int
        flags = ctypes.c_uint()
        if runtime.cudaGetDeviceFlags(ctypes.byref(flags)) != 0:
            return False
        # 只改等待方式的三位，保留运行库已有的内存映射等设置。
        return runtime.cudaSetDeviceFlags((flags.value & ~7) | 4) == 0
    except (OSError, AttributeError):
        return False


def image_attention(module, x, attn_bias=None):
    import torch.nn.functional as functional
    if module.training:
        return module._depth_original_forward(x)
    if attn_bias is not None:
        return module._depth_original_forward(x, attn_bias=attn_bias)
    batch, length, channels = x.shape
    qkv = module.qkv(x).reshape(batch, length, 3, module.num_heads, channels // module.num_heads).permute(2, 0, 3, 1, 4)
    attended = functional.scaled_dot_product_attention(qkv[0], qkv[1], qkv[2], dropout_p=0., scale=module.scale)
    return module.proj_drop(module.proj(attended.transpose(1, 2).reshape(batch, length, channels)))


def temporal_attention(module, query, key, value, attention_mask=None):
    import torch.nn.functional as functional
    if module.upcast_attention or module.upcast_softmax or attention_mask is not None:
        return module._depth_original_attention(query, key, value, attention_mask)
    # 原库已把头折入批次；恢复四维后由计算库选择融合显卡算子。
    batch = query.shape[0] // module.heads
    q, k, v = (part.reshape(batch, module.heads, part.shape[1], part.shape[2]) for part in (query, key, value))
    result = functional.scaled_dot_product_attention(q, k, v, dropout_p=0., scale=module.scale)
    return result.transpose(1, 2).reshape(batch, query.shape[1], module.heads * value.shape[2])


def cached_temporal_attention(module, hidden_states, encoder_hidden_states=None, attention_mask=None,
                              video_length=None, cached_hidden_states=None):
    """缓存历史的键值投影，每帧只投影新帧；位置编码按当前窗口重新叠加。"""
    import torch
    import torch.nn.functional as functional
    if (module.training or hidden_states.shape[0] != 1 or video_length != 1 or encoder_hidden_states is not None
            or attention_mask is not None or (cached_hidden_states is not None
            and cached_hidden_states.shape[-1] != module.to_k.out_features + module.to_v.out_features)):
        return module._depth_original_stream_forward(hidden_states, encoder_hidden_states, attention_mask,
                                                      video_length, cached_hidden_states)
    current = hidden_states.transpose(0, 1)
    length = 0 if cached_hidden_states is None else cached_hidden_states.shape[1]
    if not hasattr(module, "_depth_kv_weight"):
        module._depth_kv_weight = torch.cat((module.to_k.weight, module.to_v.weight))
        module._depth_kv_bias = (None if module.to_k.bias is None else torch.cat((module.to_k.bias, module.to_v.bias)))
        module._depth_position_kv = functional.linear(module.pos_encoder.pe.to(current.dtype), module._depth_kv_weight)
    projected = functional.linear(current, module._depth_kv_weight, module._depth_kv_bias)
    history = projected if cached_hidden_states is None else torch.cat((cached_hidden_states, projected), dim=1)
    key, value = (history + module._depth_position_kv[:, :length + 1]).chunk(2, dim=-1)
    query = module.to_q(current + module.pos_encoder.pe[:, length:length + 1].to(current.dtype))
    q, k, v = (part.reshape(part.shape[0], part.shape[1], module.heads, part.shape[-1] // module.heads).transpose(1, 2)
               for part in (query, key, value))
    result = functional.scaled_dot_product_attention(q, k, v, dropout_p=0., scale=module.scale)
    result = result.transpose(1, 2).reshape(current.shape[0], 1, -1)
    return module.to_out[1](module.to_out[0](result)).transpose(0, 1), projected


def efficient_attention(model):
    """只替换已接入的两种注意力模块；第三方源码和参数文件保持原样。"""
    count = 0
    for module in model.modules():
        source = type(module).__module__
        if source.endswith("dinov2_layers.attention") and not hasattr(module, "_depth_original_forward"):
            module._depth_original_forward = module.forward
            module.forward = MethodType(image_attention, module)
            count += 1
        elif (source.endswith("motion_module.motion_module") and type(module).__name__ == "TemporalAttention"
              and not hasattr(module, "_depth_original_attention")):
            module._depth_original_attention = module._attention
            module._attention = MethodType(temporal_attention, module)
            if (module.group_norm is None and module.added_kv_proj_dim is None and module.pos_encoder is not None
                    and module.freqs_cis is None and not module.upcast_attention and not module.upcast_softmax
                    and module.to_k.out_features == module.to_v.out_features
                    and (module.to_k.bias is None) == (module.to_v.bias is None)):
                module._depth_original_stream_forward = module.forward
                module.forward = MethodType(cached_temporal_attention, module)
            count += 1
    return count


class FastImageTransform:
    """沿用官方浮点双三次缩放，预分配单精度归一化常量，避免逐帧双精度数组。"""
    def __init__(self, original):
        import numpy as np
        self.resize = original.transforms[0]
        self.mean = np.asarray([.485, .456, .406], dtype=np.float32)
        self.std = np.asarray([.229, .224, .225], dtype=np.float32)

    def __call__(self, sample):
        import numpy as np
        image = self.resize(sample)["image"]
        image -= self.mean
        image /= self.std
        return {**sample, "image": np.ascontiguousarray(image.transpose(2, 0, 1))}
