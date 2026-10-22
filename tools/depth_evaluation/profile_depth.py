"""仅测单目深度进程的阶段耗时、处理器时间、内存和显卡模块；不打开设备。"""
from __future__ import annotations

import argparse
from collections import defaultdict
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from motioncontrol.depth_comparison_worker import Adapter, write_packet
from motioncontrol.depth_comparison_metrics import DepthMetrics
from motioncontrol.depth_input import crop_references, person_rect
from motioncontrol.monocular_lean import MonocularLean
from motioncontrol.monocular_lean_features import torso_features
from tools.depth_evaluation.prepare_recording import read_rows


class Memory(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("peak_working", "working", "peak_paged", "paged",
        "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]


def memory_mb():
    info = Memory()
    info.cb = ctypes.sizeof(info)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(wintypes.HANDLE(-1), ctypes.byref(info), info.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"resident_mb": info.working / 2**20, "private_committed_mb": info.private / 2**20}


def stats(values):
    return {"mean_ms": float(np.mean(values)), "median_ms": float(np.median(values)),
            "p95_ms": float(np.percentile(values, 95)), "max_ms": float(np.max(values)), "count": len(values)}


def preview_bytes(depth, comparison, inverse, width, height):
    values = depth[np.isfinite(depth) & (depth > 0)]
    lo, hi = np.percentile(values, (2, 98))
    view = np.uint8(np.clip((np.nan_to_num(depth) - lo) / max(1e-6, hi - lo), 0, 1) * 255)
    if not inverse:
        view = 255 - view
    view = cv2.applyColorMap(view, cv2.COLORMAP_TURBO)
    for p in comparison["points"]:
        cv2.circle(view, (int(p["x"] * width), int(p["y"] * height)), 4, (255, 255, 255), 1)
    return cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


class Timeline:
    def __init__(self, torch):
        self.torch, self.current = torch, None

    def wrap(self, original, name, gpu=True):
        def measured(*args, **kwargs):
            if self.current is None:
                return original(*args, **kwargs)
            begin, end = (self.torch.cuda.Event(enable_timing=True), self.torch.cuda.Event(enable_timing=True)) if gpu else (None, None)
            if begin:
                begin.record()
            started = time.perf_counter()
            result = original(*args, **kwargs)
            cpu_ms = (time.perf_counter() - started) * 1000
            if end:
                end.record()
            self.current[name].append((begin, end, cpu_ms))
            return result
        return measured


def profile(recording, ident, count, output):
    registry = json.loads((Path.home() / "AppData/Local/MotionControl/depth_comparison_runtime.json").read_text(encoding="utf-8-sig"))
    samples = read_rows(Path(recording) / "analysis/samples.jsonl")
    rect = person_rect(samples[0]["pose"], 640, 360)
    x, y, w, h = rect
    started = time.perf_counter()
    adapter = Adapter(ident, registry["models"][ident], 392)
    load_ms = (time.perf_counter() - started) * 1000
    torch = adapter.torch
    if adapter.device != "cuda":
        raise RuntimeError("这份显卡分段测量需要已配置的显卡运行环境")
    engine, metrics = MonocularLean(), DepthMetrics(relative=ident == "zipdepth")
    stages, stage_cpu, cpu_times = defaultdict(list), defaultdict(list), []
    previous_cpu = 0.
    def finish(name, begin):
        nonlocal previous_cpu
        stages[name].append((time.perf_counter() - begin) * 1000)
        current_cpu = time.process_time()
        stage_cpu[name].append((current_cpu - previous_cpu) * 1000)
        previous_cpu = current_cpu
    parameter_bytes = sum(p.numel() * p.element_size() for p in adapter.model.parameters())

    def frame(index):
        sample = samples[index % len(samples)]
        image = cv2.imread(sample["file"])
        if image is None:
            raise ValueError("录制彩色帧无法读取")
        image = cv2.flip(image[1::3, 1::3].copy(), 1)
        return sample, image

    # VDA的历史缓存需覆盖32帧，48帧预热后再作无插桩的主体测量。
    for i in range(48):
        _, image = frame(i)
        depth = adapter.infer(image[y:y+h, x:x+w].copy(), ("profile", rect))
    # 数值库和预览首次初始化也单独预热，否则少数首次调用会抬高稳态均值。
    warm = samples[47]
    warm_pose = {n: {**p, "x": (p["x"] * 640 - x) / w, "y": (p["y"] * 360 - y) / h} for n, p in warm["pose"].items()}
    for i in range(8):
        torso = torso_features(depth, warm_pose, inverse=ident == "zipdepth", source_height=360)
        prediction = engine.update(torso, samples[48]["t"] - 4 + i * .5, reference=None, learn=False)
        comparison = metrics.evaluate(depth, crop_references(warm["references"], rect, 640, 360), now=warm["t"])
        preview = preview_bytes(depth, comparison, ident == "zipdepth", w, h)
        write_packet(io.BytesIO(), {"torso": torso, "prediction": prediction, "comparison": comparison}, preview)
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    for i in range(48, 48 + count):
        sample, image = frame(i)
        cpu_start, begin = time.process_time(), time.perf_counter()
        previous_cpu = cpu_start
        cropped = image[y:y+h, x:x+w].copy()
        finish("crop", begin)
        begin = time.perf_counter()
        depth = adapter.infer(cropped, ("profile", rect))
        torch.cuda.synchronize()
        finish("model_infer", begin)
        begin = time.perf_counter()
        pose = {n: {**p, "x": (p["x"] * 640 - x) / w, "y": (p["y"] * 360 - y) / h} for n, p in sample["pose"].items()}
        torso = torso_features(depth, pose, inverse=ident == "zipdepth", source_height=360)
        prediction = engine.update(torso, sample["t"], reference=None, learn=False)
        finish("torso_readout", begin)
        begin = time.perf_counter()
        references = crop_references(sample["references"], rect, 640, 360)
        comparison = metrics.evaluate(depth, references, now=sample["t"])
        finish("reference_statistics", begin)
        begin = time.perf_counter()
        preview = preview_bytes(depth, comparison, ident == "zipdepth", w, h)
        finish("diagnostic_preview", begin)
        begin = time.perf_counter()
        write_packet(io.BytesIO(), {"torso": torso, "prediction": prediction, "comparison": comparison}, preview)
        finish("packet_build_in_memory", begin)
        cpu_times.append((time.process_time() - cpu_start) * 1000)
    usage = {**memory_mb(), "gpu_allocated_mb": torch.cuda.memory_allocated() / 2**20,
             "gpu_reserved_mb": torch.cuda.memory_reserved() / 2**20, "gpu_peak_allocated_mb": torch.cuda.max_memory_allocated() / 2**20}

    # 显卡分段另测16帧，不把插桩开销混入上面的主体耗时。
    timeline, gpu_rows, detailed_wall = Timeline(torch), defaultdict(list), []
    if ident == "vda_metric_s":
        adapter.model.forward_features = timeline.wrap(adapter.model.forward_features, "encoder")
        adapter.model.forward_depth = timeline.wrap(adapter.model.forward_depth, "decoder_including_temporal")
        adapter.model.transform = timeline.wrap(adapter.model.transform, "preprocess_cpu", gpu=False)
        for module in adapter.model.head.motion_modules:
            module.forward = timeline.wrap(module.forward, "temporal_inside_decoder")
    elif ident == "dav2_metric_s":
        adapter.model.pretrained.get_intermediate_layers = timeline.wrap(adapter.model.pretrained.get_intermediate_layers, "encoder")
        adapter.model.depth_head.forward = timeline.wrap(adapter.model.depth_head.forward, "decoder")
        adapter.model.image2tensor = timeline.wrap(adapter.model.image2tensor, "preprocess_cpu", gpu=False)
    else:
        adapter.model.encoder.forward = timeline.wrap(adapter.model.encoder.forward, "encoder")
        adapter.model.decoder.forward = timeline.wrap(adapter.model.decoder.forward, "decoder")
    for i in range(48 + count, 64 + count):
        _, image = frame(i)
        cropped = image[y:y+h, x:x+w].copy()
        timeline.current = defaultdict(list)
        begin = time.perf_counter()
        adapter.infer(cropped, ("profile", rect))
        torch.cuda.synchronize()
        detailed_wall.append((time.perf_counter() - begin) * 1000)
        for name, events in timeline.current.items():
            gpu_rows[name].append(sum(start.elapsed_time(end) if start else cpu for start, end, cpu in events))
    report = {"model": ident, "source_rect": list(rect), "input_shape": adapter.shape,
        "frames": count, "warmup_frames": 48, "device": torch.cuda.get_device_name(), "load_ms": load_ms,
        "parameters_million": sum(p.numel() for p in adapter.model.parameters()) / 1e6,
        "parameter_tensor_mb": parameter_bytes / 2**20, "stages": {k: stats(v) for k, v in stages.items()},
        "cpu_stage_mean_ms": {k: float(np.mean(v)) for k, v in stage_cpu.items()},
        "opencv_threads": cv2.getNumThreads(), "torch_threads": torch.get_num_threads(),
        "depth_total_ms": float(sum(np.mean(v) for v in stages.values())), "cpu_process_ms_per_frame": float(np.mean(cpu_times)),
        "frame_total": stats([sum(v) for v in zip(*stages.values())]), "postprocess_warmup": 8,
        "cpu_whole_machine_at_15fps_pct": float(np.mean(cpu_times) * 15 / (10 * os.cpu_count())),
        "cpu_whole_machine_at_30fps_pct": float(np.mean(cpu_times) * 30 / (10 * os.cpu_count())),
        "logical_processors": os.cpu_count(), "memory": usage,
        "module_timeline": {k: stats(v) for k, v in gpu_rows.items()}, "instrumented_model_wall": stats(detailed_wall),
        "notes": ["主体阶段为无插桩实录重放，排除磁盘解码、摄像头、人体定位、手部与游戏输出",
                  "模型耗时包括预处理、显卡执行及结果读回；显卡模块时间线另测，不与处理器时间相加",
                  "VDA的时序项包含在解码器内；打包为内存内测量，未包含操作系统管道传输",
                  "处理器占用按每帧进程处理器时间和目标频率折算，非全机当前占用或独占显卡测速"]}
    output.write_text(json.dumps({**report, "stage_raw_ms": stages, "cpu_stage_raw_ms": stage_cpu}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--recording", required=True, type=Path)
    parser.add_argument("--model", choices=("dav2_metric_s", "vda_metric_s", "zipdepth"), required=True)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 16 <= args.frames <= 96:
        parser.error("测量帧数需在16到96之间")
    profile(args.recording, args.model, args.frames, args.output)
