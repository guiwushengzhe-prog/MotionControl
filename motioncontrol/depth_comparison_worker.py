"""独立深度推理进程；标准输出仅传包，库的日志写入标准错误。"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import struct
import sys
import time


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError
        result.extend(part)
    return bytes(result)


def read_packet(stream):
    meta_size, data_size = struct.unpack("<II", read_exact(stream, 8))
    if not 0 < meta_size <= 200_000 or data_size > 32_000_000:
        raise ValueError("深度进程数据包大小无效")
    return json.loads(read_exact(stream, meta_size)), read_exact(stream, data_size)


def write_packet(stream, meta, data=b""):
    encoded = json.dumps(meta, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<II", len(encoded), len(data)))
    stream.write(encoded)
    stream.write(data)
    stream.flush()


class Adapter:
    def __init__(self, ident, spec, size):
        import torch
        self.torch, self.ident, self.size = torch, ident, size
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        torch.set_num_threads(2)
        self.shape = None
        sys.path.insert(0, spec["source"])
        if ident == "dav2_metric_s":
            sys.path.insert(0, str(Path(spec["source"]) / "metric_depth"))
            from depth_anything_v2.dpt import DepthAnythingV2
            self.model = DepthAnythingV2(encoder="vits", features=64, out_channels=[48, 96, 192, 384], max_depth=20)
        elif ident == "vda_metric_s":
            from video_depth_anything.video_depth_stream import VideoDepthAnything
            self.model = VideoDepthAnything(encoder="vits", features=64, out_channels=[48, 96, 192, 384])
            original = self.model.forward_features
            def features(value):
                self.shape = list(value.shape[-2:])
                return original(value)
            self.model.forward_features = features
        elif ident == "zipdepth":
            from zipdepth.model.architecture import create_model
            self.model = create_model(variant="base", upsample_unfold=True)
        else:
            raise ValueError("未知深度模型")
        checkpoint = torch.load(spec["checkpoint"], map_location="cpu", weights_only=True)
        if ident == "zipdepth":
            from zipdepth.utils.model_utils import strip_state_dict_prefixes
            checkpoint = strip_state_dict_prefixes(checkpoint.get("model_state_dict", checkpoint))
        self.model.load_state_dict(checkpoint, strict=True)
        self.model = self.model.to(self.device).eval()
        if ident == "zipdepth":
            self.model.fuse_for_inference()
            if self.device == "cuda":
                self.model = self.model.half().to(memory_format=torch.channels_last)
        self.signature = None

    def infer(self, frame, signature):
        import cv2
        import numpy as np
        torch = self.torch
        h, w = frame.shape[:2]
        with torch.inference_mode():
            if self.ident == "vda_metric_s":
                if signature != self.signature:
                    self.model.transform = None
                    self.model.frame_id_list, self.model.frame_cache_list, self.model.id = [], [], -1
                    self.signature = signature
                depth = self.model.infer_video_depth_one(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), self.size,
                                                        device=self.device, fp32=self.device != "cuda")
            else:
                if self.ident == "dav2_metric_s":
                    tensor, _ = self.model.image2tensor(frame, self.size)
                else:
                    scale = self.size / min(h, w)
                    target = tuple(max(32, round(v * scale / 32) * 32) for v in (w, h))
                    image = cv2.cvtColor(cv2.resize(frame, target), cv2.COLOR_BGR2RGB)
                    dtype = torch.float16 if self.device == "cuda" else torch.float32
                    tensor = torch.from_numpy(image).permute(2, 0, 1).unsqueeze(0).to(self.device, dtype=dtype) / 255
                    if self.device == "cuda":
                        tensor = tensor.to(memory_format=torch.channels_last)
                self.shape = list(tensor.shape[-2:])
                with torch.autocast(device_type=self.device, enabled=self.device == "cuda"):
                    output = self.model(tensor)
                output = output.reshape(1, 1, *output.shape[-2:])
                depth = torch.nn.functional.interpolate(output, (h, w), mode="bilinear", align_corners=True)[0, 0].float().cpu().numpy()
        return np.asarray(depth, dtype=np.float32)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--size", type=int, default=392)
    args = parser.parse_args()
    binary_out, binary_in = sys.stdout.buffer, sys.stdin.buffer
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import cv2
            import numpy as np
            from motioncontrol.depth_comparison_metrics import DepthMetrics
            registry = json.loads(Path(args.runtime).read_text(encoding="utf-8-sig"))
            started = time.perf_counter()
            adapter = Adapter(args.model, registry["models"][args.model], args.size)
            metrics = DepthMetrics(relative=args.model == "zipdepth")
            torch = adapter.torch
        write_packet(binary_out, {"kind": "ready", "device": adapter.device,
                                 "device_name": torch.cuda.get_device_name(0) if adapter.device == "cuda" else "处理器",
                                 "load_ms": round((time.perf_counter() - started) * 1000, 1)})
        previous_signature, previous_at = None, None
        while True:
            meta, payload = read_packet(binary_in)
            height, width = int(meta["height"]), int(meta["width"])
            if not 0 < height <= 4096 or not 0 < width <= 4096 or len(payload) != height * width * 3:
                raise ValueError("深度输入画面尺寸无效")
            frame = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3)
            signature = meta.get("signature")
            if signature != previous_signature:
                metrics = DepthMetrics(relative=args.model == "zipdepth")
                previous_signature = signature
            stream_signature = (signature, height, width)
            if previous_at is not None and not 0 < meta["sample_at"] - previous_at <= .6:
                adapter.signature = None
            previous_at = meta["sample_at"]
            if meta.get("calibrate"):
                metrics.start_calibration()
            if meta.get("reset"):
                metrics.reset_statistics()
            started = time.perf_counter()
            with contextlib.redirect_stdout(sys.stderr):
                depth = adapter.infer(frame, stream_signature)
            if adapter.device == "cuda":
                torch.cuda.synchronize()
            inference_ms = (time.perf_counter() - started) * 1000
            comparison = metrics.evaluate(depth, meta.get("references", []))
            values = depth[np.isfinite(depth) & (depth > 0)]
            lo, hi = np.percentile(values, (2, 98)) if values.size else (0, 1)
            view = np.uint8(np.clip((np.nan_to_num(depth) - lo) / max(1e-6, hi - lo), 0, 1) * 255)
            if args.model != "zipdepth":
                view = 255 - view
            view = cv2.applyColorMap(view, cv2.COLORMAP_TURBO)
            for point in comparison["points"]:
                cv2.circle(view, (int(point["x"] * width), int(point["y"] * height)), 4, (255, 255, 255), 1)
            preview = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
            result = {"kind": "frame", "sequence": meta["sequence"], "sample_at": meta["sample_at"],
                      "inference_ms": round(inference_ms, 2), "input_shape": adapter.shape,
                      "gpu_allocated_mb": round(torch.cuda.memory_allocated() / 2**20, 1) if adapter.device == "cuda" else None,
                      "gpu_reserved_mb": round(torch.cuda.memory_reserved() / 2**20, 1) if adapter.device == "cuda" else None,
                      "comparison": comparison}
            write_packet(binary_out, result, preview)
    except EOFError:
        pass
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        write_packet(binary_out, {"kind": "error", "error": str(exc)})


if __name__ == "__main__":
    main()
