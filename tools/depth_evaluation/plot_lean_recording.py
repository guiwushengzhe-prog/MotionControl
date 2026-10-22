"""展示前后倾实录曲线，模型尺度固定，不重拟合动作或调换方向。"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np


parser = argparse.ArgumentParser()
parser.add_argument("--recording", type=Path, required=True)
args = parser.parse_args()
directory = args.recording / "analysis"
font = Path("C:/Windows/Fonts/msyh.ttc")
if font.exists():
    font_manager.fontManager.addfont(str(font))
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
plt.rcParams.update({"axes.unicode_minus": False, "font.size": 10})
traces = json.loads((directory / "lean-traces.json").read_text())
fig, axes = plt.subplots(3, 1, figsize=(11, 7.7), sharex=True, layout="constrained")
base = np.asarray(traces["vda_metric_s-person"])
t = base[:, 0]
axes[0].plot(t, base[:, 3], color="#222222", lw=1.8)
axes[0].axhline(7, color="#99623a", ls=":", lw=1)
axes[0].axhline(-7, color="#99623a", ls=":", lw=1)
axes[0].set(title="微软二代骨骼：相对起始基准的肩髋前后角度", ylabel="后倾 ↑ / 前倾 ↓（度）")
axes[0].set_ylim(-16, 12)
axes[0].text(19.8, 7.7, "+7°", ha="right", fontsize=9)
axes[0].text(19.8, -6.5, "−7°", ha="right", fontsize=9)
for axis, ident, name, color in zip(axes[1:], ("dav2_metric_s", "vda_metric_s"), ("V2-S", "VDA-S"), ("#2974b0", "#c27331")):
    a = np.asarray(traces[ident + "-person"])
    metrics = json.loads((directory / f"{ident}-person-392-product-metrics.json").read_text())
    gain = metrics["mapping"]["gain"]
    axis.plot(a[:, 0], a[:, 2] * 100, color="#222222", lw=1.6, label="Kinect 同点表面深度")
    axis.plot(a[:, 0], a[:, 1] * gain * 100, color=color, lw=1.6, label=f"{name} 固定尺度后")
    axis.set(title=f"{name} 人体范围：肩部相对髋部的前后深浅变化", ylabel="肩髋距离差变化（厘米）")
    axis.legend(loc="upper left", frameon=False, ncols=2)
for axis in axes:
    axis.axvspan(0, 3, color="#ededed", zorder=-2)
    axis.axvline(6, color="#666666", ls="--", lw=1)
    axis.axhline(0, color="#999999", lw=.7)
    axis.grid(axis="y", color="#dddddd", lw=.6)
    axis.spines[["top", "right"]].set_visible(False)
    axis.set_xlim(0, 20)
axes[0].text(1.5, 9.6, "固定起始基准", ha="center", fontsize=9, color="#666666")
axes[0].text(6.15, 9.6, "此后用于方向统计", fontsize=9, color="#666666")
axes[-1].set_xlabel("第二段录制时间（秒）")
fig.suptitle("平均距离校准后，前后倾的方向仍需要单独验证\n前3秒固定基准，第6秒起验证；两种模型均复用人体范围", fontsize=14)
fig.savefig(directory / "前后倾对照.png", dpi=160)
print(directory / "前后倾对照.png")
