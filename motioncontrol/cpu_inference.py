"""按可用核心和负载配置同一次模型推理的线程池，保留原识别计算图。"""
from __future__ import annotations

import copy
import math
import os
import types
import warnings


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


def create_cpu_task(task_type, options, *, hands=False):
    original = options.base_options
    options.base_options, threads = cpu_options(original, thread_budget(hands=hands))
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
