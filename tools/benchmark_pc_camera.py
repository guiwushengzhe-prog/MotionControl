"""实测电脑摄像头识别与资源，输出关闭，用户设置和采集缓存不写盘。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import statistics
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("tensorflow", None)


def graphics_adapters():
    """把系统图形计数器的适配器标识对应到实际显卡名称。"""
    class Luid(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]

    class Description(ctypes.Structure):
        _fields_ = [("description", wintypes.WCHAR * 128), ("vendor", wintypes.UINT),
                    ("device", wintypes.UINT), ("subsystem", wintypes.UINT),
                    ("revision", wintypes.UINT), ("video_memory", ctypes.c_size_t),
                    ("system_memory", ctypes.c_size_t), ("shared_memory", ctypes.c_size_t),
                    ("luid", Luid), ("flags", wintypes.UINT)]

    def method(pointer, index, restype, *args):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *args)(table[index])

    factory = ctypes.c_void_p()
    iid = ctypes.create_string_buffer(uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    if ctypes.WinDLL("dxgi.dll").CreateDXGIFactory1(iid, ctypes.byref(factory)):
        return {}
    output = {}
    try:
        for index in range(16):
            adapter = ctypes.c_void_p()
            rc = method(factory, 12, ctypes.c_long, wintypes.UINT,
                        ctypes.POINTER(ctypes.c_void_p))(factory, index, ctypes.byref(adapter))
            if rc:
                break
            try:
                desc = Description()
                if not method(adapter, 10, ctypes.c_long, ctypes.POINTER(Description))(
                        adapter, ctypes.byref(desc)):
                    key = f"luid_0x{desc.luid.high & 0xffffffff:08x}_0x{desc.luid.low:08x}"
                    output[key] = {"name": desc.description, "vendor": desc.vendor}
            finally:
                method(adapter, 2, wintypes.ULONG)(adapter)
    finally:
        method(factory, 2, wintypes.ULONG)(factory)
    return output


class NvidiaMeter:
    class Usage(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class Memory(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                    ("used", ctypes.c_ulonglong)]

    def __init__(self):
        self.dll = ctypes.WinDLL("nvml.dll")
        if self.dll.nvmlInit_v2():
            raise RuntimeError("显卡监测初始化失败")
        self.device = ctypes.c_void_p()
        if self.dll.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self.device)):
            raise RuntimeError("显卡不可用")

    def sample(self):
        usage, memory = self.Usage(), self.Memory()
        power = ctypes.c_uint()
        out = {}
        if not self.dll.nvmlDeviceGetUtilizationRates(self.device, ctypes.byref(usage)):
            out["gpu_total_percent"] = usage.gpu
        if not self.dll.nvmlDeviceGetMemoryInfo(self.device, ctypes.byref(memory)):
            out["gpu_total_memory_mb"] = memory.used / 1024**2
        if not self.dll.nvmlDeviceGetPowerUsage(self.device, ctypes.byref(power)):
            out["gpu_power_w"] = power.value / 1000
        return out


class ProcessGpuMeter:
    class Value(ctypes.Structure):
        _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]

    class Item(ctypes.Structure):
        pass

    Item._fields_ = [("name", wintypes.LPWSTR), ("value", Value)]

    def __init__(self):
        self.dll = ctypes.WinDLL("pdh.dll")
        self.adapters = graphics_adapters()
        self.query = ctypes.c_void_p()
        self.dll.PdhOpenQueryW(None, 0, ctypes.byref(self.query))
        self.counters = {}
        for name, path in (("gpu_process_percent", r"\GPU Engine(*)\Utilization Percentage"),
                           ("gpu_process_memory_mb", r"\GPU Process Memory(*)\Dedicated Usage")):
            counter = ctypes.c_void_p()
            rc = self.dll.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(counter))
            if not rc:
                self.counters[name] = counter
        self.dll.PdhCollectQueryData(self.query)

    def sample(self):
        self.dll.PdhCollectQueryData(self.query)
        out = {}
        for name, counter in self.counters.items():
            size, count = wintypes.DWORD(), wintypes.DWORD()
            self.dll.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size),
                                                  ctypes.byref(count), None)
            if not size.value:
                continue
            buffer = ctypes.create_string_buffer(size.value)
            rc = self.dll.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size),
                                                       ctypes.byref(count), buffer)
            if rc:
                continue
            items = ctypes.cast(buffer, ctypes.POINTER(self.Item))
            values = [items[i].value.value for i in range(count.value)
                      if f"pid_{os.getpid()}_" in items[i].name
                      and items[i].value.status in (0, 1)]
            # 百分比采用最忙的引擎，显存对适配器求和。
            out[name] = max(values, default=0) if name.endswith("percent") else sum(values) / 1024**2
            by_adapter = {}
            for i in range(count.value):
                item = items[i]
                if f"pid_{os.getpid()}_" not in item.name or item.value.status not in (0, 1):
                    continue
                match = re.search(r"luid_0x([\da-f]+)_0x([\da-f]+)", item.name, re.IGNORECASE)
                if match:
                    key = f"luid_0x{int(match.group(1), 16):08x}_0x{int(match.group(2), 16):08x}"
                    by_adapter.setdefault(key, []).append(item.value.value)
            for key, adapter in self.adapters.items():
                vendor = "nvidia" if adapter["vendor"] == 0x10de else "intel" if adapter["vendor"] == 0x8086 else key
                values = by_adapter.get(key, [])
                metric = name.replace("gpu_process", vendor + "_process")
                out[metric] = (max(out.get(metric, 0), max(values, default=0))
                               if name.endswith("percent")
                               else out.get(metric, 0) + sum(values) / 1024**2)
        return out


def summary(samples):
    keys = {key for sample in samples for key, value in sample.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)}
    return {key: {"median": round(statistics.median(sample[key] for sample in samples
                                                   if isinstance(sample.get(key), (int, float))), 3),
                  "max": round(max(sample[key] for sample in samples
                                   if isinstance(sample.get(key), (int, float))), 3)} for key in sorted(keys)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--fps", type=int, help="不传时使用程序默认请求帧率")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--backend", choices=("auto", "msmf", "dshow"), help="不传时使用程序默认采集方式")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--gpu", action="store_true", help="尝试当前安装包的显卡执行器")
    parser.add_argument("--compute-only", action="store_true", help="内存循环真实画面，测计算上限，不是摄像头实时帧率")
    parser.add_argument("--opencv-threads", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.seconds <= 0 or args.warmup < 0 or min(args.width, args.height) <= 0
            or (args.fps is not None and args.fps <= 0)):
        parser.error("测试时间、分辨率、请求帧率必须大于零，预热时间不能为负数")
    args.output.mkdir(parents=True, exist_ok=True)
    saved = Path(os.environ.get("LOCALAPPDATA", "")) / "MotionControl" / "general_settings.json"
    settings = json.loads(saved.read_text(encoding="utf-8")) if saved.is_file() else {}
    os.environ["MOTIONCONTROL_USER_DIR"] = str(args.output / "isolated-user")
    import cv2
    import mediapipe as mp
    import psutil
    from motioncontrol.control_kernel import ControlKernel, NativeCameraService
    from motioncontrol_shared.pose_points import MP_NAMES
    if args.opencv_threads is not None:
        cv2.setNumThreads(args.opencv_threads)

    class SilentOutput:
        enabled = False

        def __getattr__(self, name):
            return lambda *a, **kw: None

    detector_times = []

    class DetectorTimer:
        def __init__(self, inner):
            self.inner = inner

        def detect_for_video(self, *a):
            start = time.perf_counter()
            result = self.inner.detect_for_video(*a)
            detector_times.append((time.perf_counter() - start) * 1000)
            return result

        def close(self):
            self.inner.close()

    class Camera(NativeCameraService):
        def _save_backend_cache(self, *_a):
            pass

        def _create_detector(self):
            if not args.gpu:
                mp_module, detector = super()._create_detector()
            else:
                from mediapipe.tasks import python
                from mediapipe.tasks.python import vision
                options = vision.PoseLandmarkerOptions(
                    base_options=python.BaseOptions(model_asset_path=str(self.model_path),
                                                    delegate=python.BaseOptions.Delegate.GPU),
                    running_mode=vision.RunningMode.VIDEO, num_poses=1,
                    min_pose_detection_confidence=.35, min_pose_presence_confidence=.35,
                    min_tracking_confidence=.35)
                mp_module, detector = mp, vision.PoseLandmarker.create_from_options(options)
            return mp_module, DetectorTimer(detector)

    process = psutil.Process()
    gpu_error = None
    try:
        gpu = NvidiaMeter()
    except (OSError, RuntimeError) as exc:
        gpu, gpu_error = None, str(exc)
    process_gpu = ProcessGpuMeter()
    kernel = ControlKernel(SilentOutput(), persist=False)
    for key in ("camera_rotation", "camera_auto_rotation"):
        if key in settings:
            kernel.remember_general_setting(key, settings[key])
    camera = Camera(kernel, args.model, args.camera)
    if args.fps is not None:
        camera.requested_fps = args.fps
    camera.requested_width, camera.requested_height = args.width, args.height
    if args.backend is not None:
        camera.backend_preference = args.backend
    samples, idle = [], []
    error = None
    started = time.monotonic()

    def resource_sample():
        return {"t": round(time.monotonic() - started, 3),
                "cpu_process_percent": process.cpu_percent() / psutil.cpu_count(),
                "ram_process_mb": process.memory_info().rss / 1024**2,
                **(gpu.sample() if gpu else {}), **process_gpu.sample()}

    try:
        process.cpu_percent()
        for _ in range(3):
            time.sleep(1)
            idle.append(resource_sample())
        print("开始电脑摄像头实测，游戏输出关闭", flush=True)
        camera.start()
        stop_preview = threading.Event()

        def demand_preview():
            while not stop_preview.wait(.15):
                camera.latest_preview()

        if args.preview:
            threading.Thread(target=demand_preview, daemon=True).start()
        time.sleep(args.warmup)
        if args.compute_only:
            # 真实画面只留在内存，测速结束即释放，不保存视频或照片。
            frames = []
            for _ in range(30):
                frame = camera.latest_frame()
                if frame is not None:
                    frames.append(frame)
                time.sleep(1 / 30)
            stop_preview.set()
            camera.stop()
            if not frames:
                raise RuntimeError("没有可用于计算测速的摄像头画面")
            _, detector = camera._create_detector()
            resource_stop = threading.Event()

            def poll_resources():
                while not resource_stop.wait(1):
                    samples.append(resource_sample())

            detector_times.clear()
            process.cpu_percent()
            counter, person_frames = 0, 0
            measured_at = time.monotonic()
            worker = threading.Thread(target=poll_resources, daemon=True)
            worker.start()
            try:
                while time.monotonic() - measured_at < args.seconds:
                    frame = frames[counter % len(frames)]
                    image = mp.Image(image_format=mp.ImageFormat.SRGB,
                                     data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    result = detector.detect_for_video(image, counter * 16)
                    pose = result.pose_landmarks[0] if result.pose_landmarks else None
                    world = result.pose_world_landmarks[0] if result.pose_world_landmarks else None

                    def points(landmarks):
                        return {MP_NAMES[i]: {"x": p.x, "y": p.y, "z": p.z, "score": p.visibility}
                                for i, p in enumerate(landmarks)} if landmarks else None

                    kernel.handle_pose_map("computer_camera", points(pose),
                                           width=frame.shape[1], height=frame.shape[0],
                                           world_pose=points(world))
                    counter += 1
                    person_frames += bool(pose)
                results = {"compute_capacity_fps": counter / (time.monotonic() - measured_at),
                           "detector_ms_median": statistics.median(detector_times),
                           "person_frame_fraction": person_frames / counter,
                           "frames": counter, "source_frames_in_ram": len(frames)}
            finally:
                resource_stop.set()
                worker.join()
                detector.close()
            results["samples_summary"] = summary(samples)
        else:
            results = measure_live(camera, process, detector_times, resource_sample, samples, args)
            stop_preview.set()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        results = {"error": error}
    finally:
        camera.stop()
        kernel.close()
    report = {"arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
              "pid": os.getpid(), "python": sys.version, "mediapipe": mp.__version__,
              "opencv_threads": cv2.getNumThreads(), "cpu_logical_count": psutil.cpu_count(),
              "graphics_adapters": process_gpu.adapters,
              "nvidia_monitor_error": gpu_error,
              "idle_summary": summary(idle), "result": results, "samples": samples}
    (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    return 1 if error else 0


def measure_live(camera, process, detector_times, resource_sample, samples, args):
    import cv2

    def capture_properties():
        return {name: camera._capture.get(key) for name, key in (
            ("reported_fps", cv2.CAP_PROP_FPS), ("exposure", cv2.CAP_PROP_EXPOSURE),
            ("auto_exposure", cv2.CAP_PROP_AUTO_EXPOSURE), ("gain", cv2.CAP_PROP_GAIN),
            ("fourcc", cv2.CAP_PROP_FOURCC))}

    def frame_boundary():
        # 同一时刻的输入/完成计数会差一帧正在计算的画面。
        # 在短暂空档取起止点，把窗口首尾跨界的一帧排除出比较。
        deadline = time.monotonic() + .2
        while True:
            perf = camera.performance()
            if (perf["captured_frames"] == perf["processed_frames"] + perf["skipped_frames"]
                    or time.monotonic() >= deadline):
                return perf
            time.sleep(.001)

    properties_start = capture_properties()
    detector_times.clear()
    initial = frame_boundary()
    frame_start = initial["processed_frames"]
    capture_start = initial["captured_frames"]
    measured_at = time.monotonic()
    process.cpu_percent()
    while time.monotonic() - measured_at < args.seconds:
        time.sleep(1)
        row = {**camera.performance(), **resource_sample()}
        samples.append(row)
        print(json.dumps({k: row.get(k) for k in ("inference_fps", "capture_fps",
                        "inference_avg_ms", "recent_humans", "cpu_process_percent",
                        "gpu_process_percent", "gpu_total_percent")}, ensure_ascii=False), flush=True)
        if row.get("last_error"):
            raise RuntimeError(row["last_error"])
    final = frame_boundary()
    measured_s = time.monotonic() - measured_at
    captured = final["captured_frames"] - capture_start
    processed = final["processed_frames"] - frame_start
    return {"actual_completed_fps": processed / measured_s,
            "actual_capture_fps": captured / measured_s,
            "captured_frames": captured, "processed_frames": processed,
            "processed_capture_ratio": processed / captured if captured else None,
            "skipped_during_measurement": final["skipped_frames"] - initial["skipped_frames"],
            "dropped_during_measurement": final["dropped_frames"] - initial["dropped_frames"],
            "capture_properties_start": properties_start,
            "capture_properties_end": capture_properties(),
            "detector_ms_median": statistics.median(detector_times) if detector_times else None,
            "detector_ms_p95": sorted(detector_times)[int(.95 * (len(detector_times) - 1))]
            if detector_times else None,
            "person_sample_fraction": sum(s["recent_humans"] > 0 for s in samples) / len(samples),
            "status": camera.status(), "samples_summary": summary(samples)}


if __name__ == "__main__":
    raise SystemExit(main())
