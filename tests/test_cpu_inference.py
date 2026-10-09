"""验证实际计算线程参数、负载适配及不能确认时的回退。"""
from types import SimpleNamespace
import pytest
from motioncontrol.cpu_inference import cpu_options, threads_for_capacity, create_cpu_task


@pytest.mark.parametrize("logical,physical,load,expected", [
    (24, 16, 0, 6), (24, 16, 70, 3), (24, 16, 95, 1),
    (4, 4, 0, 3), (2, 2, 0, 1), (1, 1, 0, 1),
])
def test_thread_budget_adapts_without_benchmarking_thread_slots(logical, physical, load, expected):
    assert threads_for_capacity(logical, physical, load) == expected
    assert 1 <= threads_for_capacity(logical, physical, load, hands=True) <= 2


def test_thread_options_are_instance_local_and_do_not_replace_model_or_delegates():
    class Base:
        model_asset_buffer = b"same model"
        def to_pb2(self):
            return SimpleNamespace(model_asset=self.model_asset_buffer,
                acceleration=SimpleNamespace(xnnpack=SimpleNamespace(num_threads=-1)))
    base = Base()
    wrapped, count = cpu_options(base, 6)
    assert count == 6 and wrapped is not base
    assert wrapped.to_pb2().acceleration.xnnpack.num_threads == 6
    assert wrapped.to_pb2().model_asset == b"same model"
    assert base.to_pb2().acceleration.xnnpack.num_threads == -1


def test_runtime_without_native_thread_api_does_not_claim_multicore():
    base = SimpleNamespace(model_asset_buffer=b"same model")
    wrapped, threads = cpu_options(base, 6)
    assert wrapped is base and threads is None


@pytest.mark.parametrize("failure", ["create", "graph"])
def test_unconfirmed_threads_close_partial_task_and_restore_original_options(monkeypatch, failure):
    import motioncontrol.cpu_inference as module
    original, wrapped = object(), object()
    options = SimpleNamespace(base_options=original)
    monkeypatch.setattr(module, "cpu_options", lambda *_args: (wrapped, 6))
    monkeypatch.setattr(module, "thread_budget", lambda **_kwargs: 6)
    calls, closed = [], []
    class Task:
        @staticmethod
        def create_from_options(options):
            calls.append(options.base_options)
            if len(calls) == 1 and failure == "create":
                raise ValueError("线程参数不支持")
            return SimpleNamespace(close=lambda: closed.append(True))
    task, count, reason = create_cpu_task(Task, options)
    assert task is not None and count is None and reason
    assert calls == [wrapped, original] and options.base_options is original
    assert closed == ([] if failure == "create" else [True])
