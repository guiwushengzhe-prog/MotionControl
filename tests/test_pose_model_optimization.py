"""检查模型图裁剪的共享计算、原文件保留，以及抠图和不兼容运行库的回退。"""
import io
import sys
from types import SimpleNamespace
import zipfile

import pytest

from motioncontrol.control_kernel import NativeCameraService
from motioncontrol.pose_model_optimization import _Model, _without_mask, pose_model_without_mask


def model_graph(*, unknown_shape=False):
    flatbuffers = pytest.importorskip("flatbuffers")
    builder = flatbuffers.Builder(1024)

    def vector(values, *, tables=False):
        builder.StartVector(4, len(values), 4)
        for value in reversed(values):
            (builder.PrependUOffsetTRelative if tables else builder.PrependInt32)(value)
        return builder.EndVector()

    shapes = [[1], [1], [1, 195], [1, 1], [1, 256, 256, 1], [1, 64, 64, 39], [1, 117]]
    if unknown_shape:
        shapes[4] = [1, 128, 128, 1]
    tensors = []
    for shape in shapes:
        shape_at = vector(shape)
        builder.StartObject(3)
        builder.PrependUOffsetTRelativeSlot(0, shape_at, 0)
        tensors.append(builder.EndObject())
    operations = []
    for inputs, outputs in [([0], [1]), ([1], [2, 3]), ([1], [4]), ([2], [5]), ([1], [6])]:
        inputs_at, outputs_at = vector(inputs), vector(outputs)
        builder.StartObject(3)
        builder.PrependUOffsetTRelativeSlot(1, inputs_at, 0)
        builder.PrependUOffsetTRelativeSlot(2, outputs_at, 0)
        operations.append(builder.EndObject())
    tensors_at, outputs_at, operations_at = vector(tensors, tables=True), vector([2, 3, 4, 5, 6]), vector(operations, tables=True)
    builder.StartObject(4)
    for index, value in [(0, tensors_at), (2, outputs_at), (3, operations_at)]:
        builder.PrependUOffsetTRelativeSlot(index, value, 0)
    graph_at = builder.EndObject()
    graphs_at = vector([graph_at], tables=True)
    builder.StartObject(3)
    builder.PrependUOffsetTRelativeSlot(2, graphs_at, 0)
    root = builder.EndObject()
    builder.Finish(root, file_identifier=b"TFL3")
    return bytes(builder.Output())


def test_pruning_preserves_shared_body_operations_and_all_other_bytes():
    original = model_graph()
    changed = _without_mask(original)
    assert changed and len(changed) == len(original)
    before, after = _Model(original), _Model(changed)
    graph = before.tables(before.root, 2)[0]
    operators = before.tables(graph, 3)
    assert after.tables(graph, 3) == [operators[i] for i in [0, 1, 3, 4]]
    assert after.integers(graph, 2) == [2, 3, 3, 5, 6]
    output_at, _ = before.vector(graph, 2)
    operations_at, count = before.vector(graph, 3)
    allowed = set(range(output_at + 12, output_at + 16)) | set(range(operations_at, operations_at + 4 + count * 4))
    assert all(index in allowed for index, (a, b) in enumerate(zip(original, changed)) if a != b)


def test_task_keeps_original_file_and_pose_detector_and_rejects_unknown_layout(tmp_path):
    path = tmp_path / "heavy.task"
    with zipfile.ZipFile(path, "w") as task:
        task.writestr("pose_detector.tflite", b"unchanged detector")
        task.writestr("pose_landmarks_detector.tflite", model_graph())
    original = path.read_bytes()
    changed = pose_model_without_mask(path)
    with zipfile.ZipFile(io.BytesIO(changed)) as task:
        assert task.read("pose_detector.tflite") == b"unchanged detector"
    assert path.read_bytes() == original
    assert _without_mask(model_graph(unknown_shape=True)) is None
    path.write_bytes(b"not a model")
    assert pose_model_without_mask(path) is None
    assert pose_model_without_mask(tmp_path / "missing.task") is None


@pytest.mark.parametrize("choice,masks,fails,expected", [
    ("heavy", False, False, True), ("heavy", True, False, False),
    ("full", False, False, False), ("heavy", False, True, False),
])
def test_detector_uses_original_for_masks_standard_model_and_incompatible_runtime(tmp_path, monkeypatch, choice, masks, fails, expected):
    import motioncontrol.pose_model_optimization as optimization
    calls = []

    def create(options):
        calls.append(options)
        if fails and getattr(options.base_options, "model_asset_buffer", None):
            raise ValueError("模拟不兼容的输出布局")
        return SimpleNamespace(close=lambda: None)

    python = SimpleNamespace(BaseOptions=lambda **kw: SimpleNamespace(**kw))
    vision = SimpleNamespace(PoseLandmarkerOptions=lambda **kw: SimpleNamespace(**kw),
                             PoseLandmarker=SimpleNamespace(create_from_options=create),
                             RunningMode=SimpleNamespace(VIDEO=1))
    monkeypatch.setitem(sys.modules, "mediapipe", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mediapipe.tasks", SimpleNamespace(python=python))
    monkeypatch.setitem(sys.modules, "mediapipe.tasks.python", SimpleNamespace(vision=vision))
    monkeypatch.setattr(optimization, "pose_model_without_mask", lambda path: b"optimized model")
    path = tmp_path / "model.task"
    path.write_bytes(b"original model")
    camera = NativeCameraService(SimpleNamespace(general_setting=lambda key, default: default))
    camera.configure_models({"full": path, "heavy": path})
    camera._studio_require_mask = masks
    camera._create_detector(choice)
    assert camera.actual_model == choice
    assert camera.pose_model_optimization == ("skip_unused_mask" if expected else None)
    assert calls[-1].output_segmentation_masks is masks
    assert getattr(calls[-1].base_options, "model_asset_buffer", None) == (b"optimized model" if expected else None)
    if not expected:
        assert calls[-1].base_options.model_asset_path == str(path)
    assert len(calls) == (2 if fails else 1)


def test_auto_uses_full_with_limited_memory_without_rewriting_user_preference(tmp_path, monkeypatch):
    import motioncontrol.cpu_inference as cpu
    monkeypatch.setattr(cpu, "available_commit_memory", lambda: 1500 * 1024 ** 2)
    path = tmp_path / "model.task"
    path.write_bytes(b"original")
    camera = NativeCameraService(SimpleNamespace(general_setting=lambda key, default: default))
    camera.configure_models({"full": path, "heavy": path})
    camera.model_preference = "auto"
    calls = []
    python = SimpleNamespace(BaseOptions=lambda **kw: SimpleNamespace(**kw))
    vision = SimpleNamespace(PoseLandmarkerOptions=lambda **kw: SimpleNamespace(**kw),
                             PoseLandmarker=object(), RunningMode=SimpleNamespace(VIDEO=1))
    monkeypatch.setitem(sys.modules, "mediapipe", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mediapipe.tasks", SimpleNamespace(python=python))
    monkeypatch.setitem(sys.modules, "mediapipe.tasks.python", SimpleNamespace(vision=vision))
    def create(task, options, **kwargs):
        calls.append(options)
        return object(), 2, None
    monkeypatch.setattr(cpu, "create_cpu_task", create)
    camera._create_detector()
    assert camera.actual_model == "full" and camera.model_preference == "auto"
    assert "内存" in camera._auto_pose.reason
    assert calls[0].base_options.model_asset_path == str(path)
