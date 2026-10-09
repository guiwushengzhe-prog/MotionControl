"""省掉普通高精度识别不消费的抠图计算；骨骼模型权重和计算保持原样。

只支持已验证的单图、五输出姿态模型。抠图开启时仍使用原始模型。
在内存中改写模型文件的计算列表，不改动用户模型文件，不新增运行库。
"""
from __future__ import annotations

from functools import lru_cache
import io
from pathlib import Path
import struct
import zipfile


class _Model:
    def __init__(self, data):
        if data[4:8] != b"TFL3":
            raise ValueError("模型格式不匹配")
        self.data = data
        self.root = self.u32(0)

    def u32(self, at):
        return struct.unpack_from("<I", self.data, at)[0]

    def field(self, table, index):
        vtable = table - struct.unpack_from("<i", self.data, table)[0]
        length = struct.unpack_from("<H", self.data, vtable)[0]
        offset = 4 + index * 2
        if offset >= length:
            return 0
        relative = struct.unpack_from("<H", self.data, vtable + offset)[0]
        return table + relative if relative else 0

    def vector(self, table, index):
        field = self.field(table, index)
        if not field:
            raise ValueError("缺少模型列表")
        at = field + self.u32(field)
        count = self.u32(at)
        if at + 4 + count * 4 > len(self.data):
            raise ValueError("模型列表长度无效")
        return at, count

    def integers(self, table, index):
        at, count = self.vector(table, index)
        return list(struct.unpack_from(f"<{count}i", self.data, at + 4))

    def tables(self, table, index):
        at, count = self.vector(table, index)
        return [position + self.u32(position) for position in range(at + 4, at + 4 + count * 4, 4)]


def _without_mask(data):
    model = _Model(data)
    graphs = model.tables(model.root, 2)
    if len(graphs) != 1:
        return None
    graph = graphs[0]
    tensors = model.tables(graph, 0)
    outputs = model.integers(graph, 2)
    expected = [[1, 195], [1, 1], [1, 256, 256, 1], [1, 64, 64, 39], [1, 117]]
    if len(outputs) != 5 or any(index < 0 or index >= len(tensors) for index in outputs):
        return None
    if [model.integers(tensors[index], 0) for index in outputs] != expected:
        return None
    # 模型必须使用浮点输出，不能误改同形状的量化模型。
    if any(model.field(tensors[index], 1) and model.data[model.field(tensors[index], 1)] != 0 for index in outputs):
        return None
    operations = model.tables(graph, 3)
    needed = set(outputs[:2] + outputs[3:])
    retained = []
    for operation in reversed(operations):
        if needed.intersection(model.integers(operation, 2)):
            retained.append(operation)
            needed.update(index for index in model.integers(operation, 1) if index >= 0)
    retained.reverse()
    if not retained or len(retained) == len(operations):
        return None
    result = bytearray(data)
    # 未消费的输出槽引用已计算的置信度。其余四个输出顺序与张量保持不变。
    output_at, _ = model.vector(graph, 2)
    struct.pack_into("<i", result, output_at + 4 + 2 * 4, outputs[1])
    operations_at, _ = model.vector(graph, 3)
    struct.pack_into("<I", result, operations_at, len(retained))
    for index, operation in enumerate(retained):
        position = operations_at + 4 + index * 4
        if operation <= position:
            return None
        struct.pack_into("<I", result, position, operation - position)
    return bytes(result)


@lru_cache(maxsize=1)
def _optimized(path, modified_ns, size):
    with zipfile.ZipFile(path) as source:
        data = _without_mask(source.read("pose_landmarks_detector.tflite"))
        if data is None:
            return None
        target = io.BytesIO()
        with zipfile.ZipFile(target, "w") as result:
            for item in source.infolist():
                result.writestr(item, data if item.filename == "pose_landmarks_detector.tflite" else source.read(item.filename))
        return target.getvalue()


def pose_model_without_mask(path):
    """未知版本或格式自动沿用原模型；缓存随模型替换而失效。"""
    try:
        path = Path(path)
        stat = path.stat()
        return _optimized(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, ValueError, KeyError, IndexError, struct.error, zipfile.BadZipFile):
        return None
