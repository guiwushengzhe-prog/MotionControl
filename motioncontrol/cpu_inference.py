"""以跟上相机供帧为目标配置计算线程，保留原识别计算图。"""
from __future__ import annotations

import copy
import math
import os
import types
import warnings


def available_commit_memory():
    """Windows 当前进程还能申请的内存，包含虚拟内存；读取失败不冒充内存不足。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in ("physical", "physical_free", "commit_limit",
                "commit_free", "virtual", "virtual_free", "reserved")]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    try:
        query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
        query.argtypes = [ctypes.POINTER(MemoryStatus)]
        query.restype = wintypes.BOOL
        return int(status.commit_free) if query(ctypes.byref(status)) else None
    except (AttributeError, OSError):
        return None


def require_inference_memory(minimum_mb=512):
    """先保留内存余量再进入原生识别库；原生线程的分配失败无法靠 Python 捕获。"""
    available = available_commit_memory()
    if available is not None and available < minimum_mb * 1024 ** 2:
        raise MemoryError(f"系统可用内存（含虚拟内存）只剩 {available / 1024 ** 2:.0f} MB，"
                          "已暂停识别以保留控制页面；释放内存后可重试识别")
    return available


class FrameThreadBudget:
    """根据正常识别的整帧耗时分配线程，不轮流跑各个线程档位。"""
    def __init__(self):
        self.interval_ms = None
        self.samples = []
        self.warmup = 0
        self.next_change_at = self.next_down_at = 0.0
        self.down_plan, self.down_since = None, 0.0
        self.slow_plans = set()
        self.hand_active = None
        self.ready = False
        self.reason = "以跟上相机供帧为目标"

    def reset_samples(self):
        self.samples.clear()
        self.warmup = 0
        self.ready = False
        self.down_plan = None

    def defer(self, now, reason):
        self.reset_samples()
        self.next_change_at = now + 15
        self.reason = reason
        self.ready = True

    def observe(self, stages, fps, body_threads, hand_threads, *, visible, now):
        if not fps or not math.isfinite(fps) or fps < 2 or body_threads is None:
            return None
        interval = 1000 / fps
        hand_active = hand_threads is not None
        if ((self.interval_ms is not None and abs(interval / self.interval_ms - 1) > .15)
                or (self.hand_active is not None and hand_active != self.hand_active)):
            self.reset_samples()
            self.slow_plans.clear()
            self.next_change_at = self.next_down_at = 0.0
        self.interval_ms = interval
        self.hand_active = hand_active
        if not visible:
            self.reset_samples()
            return None
        self.warmup += 1
        if self.warmup <= 6 or now < self.next_change_at:
            return None
        self.samples.append((stages['pose'], stages['hands'], stages['prepare'] + stages['control']))
        if len(self.samples) < 24:
            return None
        # 各环节取九成帧能满足的耗时，避免平均值掩盖偶发跟不上。
        pose, hands, other = (sorted(row[i] for row in self.samples)[math.ceil(len(self.samples)*.9)-1] for i in range(3))
        self.samples.clear()
        self.ready = True
        current = (body_threads, hand_threads or 1)
        target = interval * .9
        if pose + hands + other > target:
            self.slow_plans.add(current)
        down = pose + hands + other <= target
        if down and now < self.next_down_at:
            return None
        # 只用现有计时估计需要的计算量；降低线程时额外留余量，变更后再观察。
        candidates = []
        for body in range(1, 7):
            for hand in range(1, 3):
                plan = (body, hand)
                predicted = pose * current[0] / body + hands * current[1] / hand + other
                if plan not in self.slow_plans and predicted <= interval * (.8 if down else .9):
                    candidates.append(plan)
        plan = min(candidates, key=lambda p: (sum(p), p[1], p[0])) if candidates else ((6, 2) if not down else current)
        if plan == current or (down and sum(plan) >= sum(current)):
            self.down_plan = None
            self.reason = "已跟上相机供帧，保留当前线程" if down else "已使用当前可用线程"
            return None
        if down:
            if self.down_plan != plan:
                self.down_plan, self.down_since = plan, now
            if now - self.down_since < 20:
                self.reason = "已跟上相机，继续观察减少线程的余量"
                return None
        self.reset_samples()
        self.next_change_at, self.next_down_at = now + 2, now + 20
        self.reason = "按整帧耗时增加线程" if not down else "够用后减少计算线程"
        return plan


def thread_budget(*, hands=False):
    logical = os.cpu_count() or 1
    physical = max(1, logical // 2)
    try:
        import psutil
    except ImportError:
        pass
    else:
        try:
            logical = min(logical, len(psutil.Process().cpu_affinity()))
            physical = min(logical, psutil.cpu_count(logical=False) or physical)
        except (psutil.Error, AttributeError, OSError):
            pass
    return threads_for_capacity(logical, physical, hands=hands)


def threads_for_capacity(logical, physical, *, hands=False):
    # 以跟上相机为先，不因游戏占用整机而主动让出线程；实际用多少由整帧耗时决定。
    # 留一个物理核心给采集和控制；不强行绑定具体核心。
    return max(1, min(2 if hands else 6, min(logical, physical) - 1))


def prioritize_process():
    """识别所在进程略高于普通程序，并退出 Windows 对后台程序的节能降速。

    游戏在前台时，后台识别默认会被系统压低调度或放到节能核心；这里只抬到“高于普通”，
    不用“高”或“实时”，避免反过来卡住游戏输入和系统。
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.SetProcessInformation.restype = wintypes.BOOL

    class PowerThrottling(ctypes.Structure):
        _fields_ = (("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG))

    process = kernel32.GetCurrentProcess()
    raised = bool(kernel32.SetPriorityClass(process, 0x8000))  # ABOVE_NORMAL_PRIORITY_CLASS
    # ProcessPowerThrottling：执行速度与计时精度两项都明确关闭节能限制。
    state = PowerThrottling(1, 0x1 | 0x4, 0)
    kernel32.SetProcessInformation(process, 4, ctypes.byref(state), ctypes.sizeof(state))
    return "above_normal" if raised else None


class ResourceShortage:
    """持续跟不上相机时提醒降配；线程还在调整或短暂波动时不提醒。"""
    HOLD_S, CLEAR_S = 10.0, 5.0

    def __init__(self):
        self.kind = None
        self._since = {}
        self._clear_since = None

    def observe(self, now, *, camera_fps, capture_fps, inference_fps, visible, threads_maxed):
        active = {
            # 原厂报告的供帧明显高于实际收到的：采集被拖慢，多为显卡被占满。
            "capture": bool(camera_fps and capture_fps and capture_fps < camera_fps * .75),
            # 线程已加到上限仍跟不上已收到的画面：处理器不够。
            "inference": bool(visible and threads_maxed and capture_fps and inference_fps
                              and inference_fps < capture_fps * .85),
        }
        for kind, on in active.items():
            if on:
                self._since.setdefault(kind, now)
            else:
                self._since.pop(kind, None)
        due = [kind for kind in ("capture", "inference")
               if kind in self._since and now - self._since[kind] >= self.HOLD_S]
        if due:
            self.kind, self._clear_since = due[0], None
        elif self.kind:
            self._clear_since = self._clear_since or now
            if now - self._clear_since >= self.CLEAR_S:
                self.kind, self._clear_since = None, None
        return self.kind


def cpu_options(base_options, threads):
    """使用识别库的原生计算参数；不全局修改基础选项类。"""
    convert = getattr(base_options, "to_pb2", None)
    if convert is None:
        return base_options, None
    proto = convert()
    if not hasattr(getattr(getattr(proto, "acceleration", None), "xnnpack", None), "num_threads"):
        return base_options, None
    result = copy.copy(base_options)

    def to_pb2(_self):
        proto = convert()
        proto.acceleration.xnnpack.num_threads = threads
        return proto

    result.to_pb2 = types.MethodType(to_pb2, result)
    return result, threads


def create_cpu_task(task_type, options, *, hands=False, threads=None):
    require_inference_memory()
    original = options.base_options
    capacity = thread_budget(hands=hands)
    requested = min(capacity, threads if threads is not None else (1 if hands else 2))
    options.base_options, threads = cpu_options(original, requested)
    try:
        detector = task_type.create_from_options(options)
    except Exception:
        if threads is None:
            raise
        options.base_options = original
        require_inference_memory()
        return task_type.create_from_options(options), None, "计算线程设置未被运行库接受"
    if threads is None:
        return detector, None, "当前运行库未开放计算线程设置"
    try:
        from mediapipe.calculators.tensor import inference_calculator_pb2
        extension = inference_calculator_pb2.InferenceCalculatorOptions.ext
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="MessageFactory class is deprecated.*", category=UserWarning)
            graph = detector.get_graph_config()
        nodes = [node for node in graph.node if "InferenceCalculator" in node.calculator]
        if not nodes or any(not node.options.HasExtension(extension)
                or node.options.Extensions[extension].delegate.xnnpack.num_threads != threads for node in nodes):
            raise ValueError("实际计算图没有使用请求的线程设置")
    except Exception:
        detector.close()
        options.base_options = original
        require_inference_memory()
        return task_type.create_from_options(options), None, "实际计算图未能确认线程设置"
    return detector, threads, None
