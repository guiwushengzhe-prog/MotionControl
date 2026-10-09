"""以跟上相机供帧为目标配置计算线程，保留原识别计算图。"""
from __future__ import annotations

import copy
import math
import os
import types
import warnings


class FrameThreadBudget:
    """根据正常识别的整帧耗时分配线程，不轮流跑各个线程档位。"""
    def __init__(self):
        self.interval_ms = None
        self.samples = []
        self.warmup = 0
        self.next_change_at = self.next_down_at = 0.0
        self.slow_plans = {}
        self.ready = False
        self.reason = "以跟上相机供帧为目标"

    def reset_samples(self):
        self.samples.clear()
        self.warmup = 0
        self.ready = False

    def defer(self, now, reason):
        self.reset_samples()
        self.next_change_at = now + 15
        self.reason = reason
        self.ready = True

    def observe(self, stages, fps, body_threads, hand_threads, *, visible, now):
        if not fps or not math.isfinite(fps) or fps < 2 or body_threads is None:
            return None
        interval = 1000 / fps
        if self.interval_ms is not None and abs(interval / self.interval_ms - 1) > .15:
            self.reset_samples()
            self.slow_plans.clear()
            self.next_change_at = self.next_down_at = 0.0
        self.interval_ms = interval
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
        self.slow_plans = {plan: at for plan, at in self.slow_plans.items() if now - at < 60}
        target = interval * .9
        if pose + hands + other > target:
            self.slow_plans[current] = now
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
            self.reason = "已跟上相机供帧，保留当前线程" if down else "已使用当前可用线程"
            return None
        self.reset_samples()
        self.next_change_at, self.next_down_at = now + 2, now + 20
        self.reason = "按整帧耗时增加线程" if not down else "够用后减少计算线程"
        return plan


def thread_budget(*, hands=False):
    logical = os.cpu_count() or 1
    physical, load = max(1, logical // 2), 0.0
    try:
        import psutil
    except ImportError:
        pass
    else:
        try:
            logical = min(logical, len(psutil.Process().cpu_affinity()))
            physical = min(logical, psutil.cpu_count(logical=False) or physical)
            load = psutil.cpu_percent(interval=.05)
        except (psutil.Error, AttributeError, OSError):
            pass
    return threads_for_capacity(logical, physical, load, hands=hands)


def threads_for_capacity(logical, physical, load, *, hands=False):
    # 留一个可用物理核心给游戏、采集和控制；不强行绑定具体核心。
    free = max(1, math.floor(min(logical, physical) * (1 - min(100, max(0, load)) / 100)))
    return max(1, min(2 if hands else 6, free - 1))


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
        return task_type.create_from_options(options), None, "实际计算图未能确认线程设置"
    return detector, threads, None
