"""融合注意力的数学等价与预览按需生成；不接触摄像头或系统输出。"""
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.depth_comparison import DepthComparison
from motioncontrol.depth_optimization import FastImageTransform


def test_gpu_wait_preserves_other_flags_and_unsupported_runtime_falls_back(tmp_path, monkeypatch):
    from motioncontrol.depth_optimization import blocking_gpu_wait
    import ctypes
    monkeypatch.setattr("sys.platform", "win32")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/cudart64_12.dll").touch()
    torch = SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
    seen = []
    def get_flags(target):
        target._obj.value = 8 | 1
        return 0
    def set_flags(value):
        seen.append(value)
        return 0
    monkeypatch.setattr(ctypes, "CDLL", lambda _: SimpleNamespace(cudaGetDeviceFlags=get_flags, cudaSetDeviceFlags=set_flags))
    assert blocking_gpu_wait(torch) and seen == [8 | 4]
    def unavailable(_):
        raise OSError("运行库无法加载")
    monkeypatch.setattr(ctypes, "CDLL", unavailable)
    assert not blocking_gpu_wait(torch)


def test_fast_transform_matches_original_normalization_and_has_single_precision():
    image = np.random.default_rng(7).random((31, 27, 3), dtype=np.float32)
    original = SimpleNamespace(transforms=[lambda sample: sample])
    target = np.ascontiguousarray(((image - [.485, .456, .406]) / [.229, .224, .225]).transpose(2, 0, 1)).astype(np.float32)
    observed = FastImageTransform(original)({"image": image.copy()})["image"]
    np.testing.assert_allclose(observed, target, atol=5e-7, rtol=2e-5)
    assert observed.dtype == np.float32 and observed.flags.c_contiguous


def test_hidden_preview_does_not_render_and_visible_preview_expires_and_limits_rate(monkeypatch):
    clock = [10.]
    monkeypatch.setattr("motioncontrol.depth_comparison.time.monotonic", lambda: clock[0])
    comparison = DepthComparison(SimpleNamespace())
    comparison.config["enabled"] = True
    assert not comparison._preview_requested(clock[0])
    assert comparison.preview() is None  # 首次页面请求只激活需求，下一帧才有图。
    assert comparison._preview_requested(clock[0])
    clock[0] += .1
    assert not comparison._preview_requested(clock[0])
    clock[0] += .1
    assert comparison._preview_requested(clock[0])
    clock[0] += 1.1
    assert not comparison._preview_requested(clock[0])
    comparison._preview = b"image"
    assert comparison.preview() == b"image"
    comparison.suspend()
    assert comparison._preview is None and not comparison._preview_requested(clock[0])


def test_fused_image_and_temporal_attention_match_explicit_math():
    torch = pytest.importorskip("torch")
    from motioncontrol.depth_optimization import image_attention, temporal_attention
    torch.manual_seed(5)
    layer = SimpleNamespace(qkv=torch.nn.Linear(24, 72), num_heads=3, scale=8**-.5,
                            proj=torch.nn.Linear(24, 24), proj_drop=torch.nn.Identity(), training=False)
    x = torch.randn(2, 17, 24)
    q, k, v = layer.qkv(x).reshape(2, 17, 3, 3, 8).permute(2, 0, 3, 1, 4)
    explicit = layer.proj(((q * layer.scale @ k.transpose(-2, -1)).softmax(-1) @ v).transpose(1, 2).reshape(2, 17, 24))
    torch.testing.assert_close(image_attention(layer, x), explicit, atol=1e-6, rtol=1e-5)
    temporal = SimpleNamespace(heads=3, scale=8**-.5, upcast_attention=False, upcast_softmax=False)
    q, k, v = torch.randn(6, 1, 8), torch.randn(6, 32, 8), torch.randn(6, 32, 8)
    explicit = (((q @ k.transpose(-2, -1)) * temporal.scale).softmax(-1) @ v).reshape(2, 3, 1, 8).transpose(1, 2).reshape(2, 1, 24)
    torch.testing.assert_close(temporal_attention(temporal, q, k, v), explicit, atol=1e-6, rtol=1e-5)


def test_cached_temporal_projection_matches_history_with_changing_positions():
    torch = pytest.importorskip("torch")
    from motioncontrol.depth_optimization import cached_temporal_attention
    torch.manual_seed(17)
    module = SimpleNamespace(training=False, heads=3, scale=4**-.5,
        to_q=torch.nn.Linear(12, 12, bias=False), to_k=torch.nn.Linear(12, 12, bias=False),
        to_v=torch.nn.Linear(12, 12, bias=False), to_out=[torch.nn.Linear(12, 12), torch.nn.Identity()],
        pos_encoder=SimpleNamespace(pe=torch.randn(1, 32, 12)))
    raw, projected = [], []
    for frame in range(40):
        current = torch.randn(1, 7, 12)
        previous = raw[:2] + raw[-29:] if len(raw) > 31 else raw
        cached = projected[:2] + projected[-29:] if len(projected) > 31 else projected
        history = torch.cat([*previous, current.transpose(0, 1)], dim=1) + module.pos_encoder.pe[:, :len(previous)+1]
        q, k, v = module.to_q(history[:, -1:]), module.to_k(history), module.to_v(history)
        q, k, v = (p.reshape(7, p.shape[1], 3, 4).transpose(1, 2) for p in (q,k,v))
        expected = (((q @ k.transpose(-2,-1)) * module.scale).softmax(-1) @ v).transpose(1,2).reshape(7,1,12)
        expected = module.to_out[0](expected).transpose(0,1)
        observed, new_cache = cached_temporal_attention(module, current, video_length=1,
            cached_hidden_states=torch.cat(cached,dim=1) if cached else None)
        torch.testing.assert_close(observed, expected, atol=1e-6, rtol=1e-5)
        raw.append(current.transpose(0,1))
        projected.append(new_cache)
