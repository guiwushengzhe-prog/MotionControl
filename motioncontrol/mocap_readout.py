"""外部个人模型的躯干角度换算；参数只从本机登记的模型文件读取。"""
from __future__ import annotations

import numpy as np


class TorsoReadout:
    def __init__(self, head):
        self.keys = head["keys"]
        if len(self.keys) != 42 or len(set(self.keys)) != 42:
            raise ValueError("个人倾角输出名称无效")
        self.indices = [i for i, key in enumerate(self.keys)
            if key.split("_")[0] in ("hip", "abdomen", "chest") and key.endswith("rotation")]
        if len(self.indices) != 9:
            raise ValueError("个人倾角需要完整的髋、腹、胸旋转量")
        self.kind = head.get("method", "linear_torso_v1")
        self.coef = None
        if self.kind == "linear_torso_v1":
            self.coef = np.asarray(head["weights"], dtype=np.float64)
            excluded = [i for i in range(42) if i not in self.indices]
            if (self.coef.shape != (42,) or not np.isfinite(self.coef).all()
                    or np.any(self.coef[excluded] != 0)):
                raise ValueError("基础倾角必须仅使用躯干旋转量，请更新个人角度换算文件")
        elif self.kind == "local_torso_v1":
            local = head["local"]
            self.scale = np.asarray(local["scale"], dtype=np.float64)
            points = np.asarray(local["features"], dtype=np.float64)
            self.targets = np.asarray(local["targets"], dtype=np.float64)
            self.neighbors, self.ridge = local["neighbors"], local["ridge"]
            if (local["feature_indices"] != self.indices or self.scale.shape != (9,)
                    or not np.isfinite(self.scale).all() or np.any(self.scale <= 0)
                    or points.ndim != 2 or points.shape[1] != 9
                    or not 8 <= len(points) <= 1024 or not np.isfinite(points).all()
                    or self.targets.shape != (len(points),) or not np.isfinite(self.targets).all()
                    or np.any(np.abs(self.targets) > 70)
                    or type(self.neighbors) is not int or not 8 <= self.neighbors <= len(points)
                    or not isinstance(self.ridge, (int, float)) or not np.isfinite(self.ridge)
                    or self.ridge <= 0):
                raise ValueError("局部躯干角度换算参数无效")
            self.points = points / self.scale
            self.penalty = np.eye(10) * self.ridge
            self.penalty[-1, -1] = 1e-8
        else:
            raise ValueError("个人角度换算方式不支持")

    def infer(self, features):
        values = np.asarray(features, dtype=np.float64)
        if values.shape != (42,) or not np.isfinite(values).all():
            raise ValueError("个人倾角特征无效")
        if self.coef is not None:
            return float(values @ self.coef)
        query = values[self.indices] / self.scale
        distance = np.sum((self.points - query) ** 2, axis=1)
        nearest = np.argpartition(distance, self.neighbors - 1)[:self.neighbors]
        weight = 1 / (distance[nearest] + .05)
        weight /= weight.sum()
        # 只在相近躯干姿态内拟合角度，避免全局权重放大手臂和位置变化。
        design = np.column_stack((self.points[nearest] - query, np.ones(self.neighbors)))
        gram = design.T @ (design * weight[:, None]) + self.penalty
        fitted = np.linalg.solve(gram, design.T @ (self.targets[nearest] * weight))
        return float(fitted[-1])
