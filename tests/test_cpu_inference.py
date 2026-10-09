"""验证实际计算线程参数、负载适配及不能确认时的回退。"""
from types import SimpleNamespace
import pytest
from motioncontrol.cpu_inference import FrameThreadBudget, cpu_options, threads_for_capacity, create_cpu_task


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


@pytest.mark.parametrize("fps,body,hands,counts,expected", [
    (15, 25, 20, (2, 1), None),
    (15, 35, 15, (1, 1), None),
    (30, 35, 15, (1, 1), (3, 1)),
    (15, 18, 12, (6, 2), (3, 2)),
    (15, 18, 50, (2, 1), (2, 2)),
])
def test_allocation_follows_whole_frame_budget(fps, body, hands, counts, expected):
    budget = FrameThreadBudget()
    stages = dict(pose=body, hands=hands, prepare=1, control=2)
    results = [budget.observe(stages, fps, *counts, visible=True, now=n/fps) for n in range(30)]
    if expected and sum(expected) < sum(counts):
        assert results[-1] is None  # 减少线程需要持续余量，不能只凭这一小段。
        results += [budget.observe(stages, fps, *counts, visible=True, now=30+n/fps) for n in range(24)]
    assert results[-1] == expected
    assert all(plan is None for plan in results[:-1])
    assert budget.interval_ms == pytest.approx(1000/fps)


def test_absent_human_and_isolated_slow_frame_do_not_reconfigure_models():
    budget = FrameThreadBudget()
    stages = dict(pose=25, hands=20, prepare=1, control=2)
    for n in range(60):
        assert budget.observe(stages, 30, 2, 1, visible=False, now=n/30) is None
    for n in range(30):
        sample = dict(stages, pose=500) if n == 10 else stages
        assert budget.observe(sample, 15, 2, 1, visible=True, now=3+n/15) is None
    assert budget.ready


def test_supply_change_increases_budget_and_failed_reduction_is_not_repeated():
    budget = FrameThreadBudget()
    stages = dict(pose=18, hands=12, prepare=1, control=2)
    for n in range(30):
        plan = budget.observe(stages, 15, 6, 2, visible=True, now=n/15)
    assert plan is None
    for n in range(24):
        plan = budget.observe(stages, 15, 6, 2, visible=True, now=30+n/15)
    assert plan == (3, 2)
    slow = dict(stages, pose=58)
    for n in range(60):
        plan = budget.observe(slow, 15, 3, 2, visible=True, now=34+n/15)
        if plan:
            break
    assert plan and (3, 2) in budget.slow_plans
    faster = dict(stages, pose=10, hands=5)
    for n in range(60):
        assert budget.observe(faster, 15, *plan, visible=True, now=40+n/15) is None
    # 不按固定分钟过期，避免对同一个已失败配置反复减线程、加线程。
    budget.observe(faster, 15, *plan, visible=True, now=120)
    assert (3, 2) in budget.slow_plans
    # 相机供帧变快后重新观察，不沿用旧供帧的判断与冷却。
    for n in range(30):
        result = budget.observe(dict(pose=35, hands=15, prepare=1, control=2), 30, 1, 1,
                                visible=True, now=130+n/30)
    assert result == (3, 1)
    assert budget.interval_ms == pytest.approx(1000/30)


def test_short_lived_headroom_does_not_reload_and_hand_demand_restarts_learning():
    budget = FrameThreadBudget()
    fast = dict(pose=18, hands=12, prepare=1, control=2)
    for n in range(30):
        assert budget.observe(fast, 15, 6, 2, visible=True, now=n/15) is None
    assert budget.down_plan == (3, 2)
    # 余量消失时撤销降线程计划；此前的快速片段不能累积凑足二十秒。
    for n in range(24):
        assert budget.observe(dict(fast, pose=50), 15, 6, 2, visible=True, now=5+n/15) is None
    assert budget.down_plan is None
    budget.slow_plans.add((2, 1))
    assert budget.observe(fast, 15, 6, None, visible=True, now=10) is None
    assert not budget.slow_plans and not budget.ready


@pytest.mark.parametrize("hands,requested,capacity,expected", [
    (False, None, 6, 2), (True, None, 2, 1), (False, 6, 2, 2),
])
def test_task_starts_small_and_respects_available_capacity(monkeypatch, hands, requested, capacity, expected):
    import motioncontrol.cpu_inference as module
    counts = []
    monkeypatch.setattr(module, "thread_budget", lambda **_kwargs: capacity)
    monkeypatch.setattr(module, "cpu_options", lambda base, count: (counts.append(count) or base, None))
    task_type = SimpleNamespace(create_from_options=lambda _options: object())
    create_cpu_task(task_type, SimpleNamespace(base_options=object()), hands=hands, threads=requested)
    assert counts == [expected]
