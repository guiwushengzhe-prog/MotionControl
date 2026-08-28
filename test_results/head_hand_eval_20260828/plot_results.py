"""Make small read-only plots from the generated CSVs (test artifact only)."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "final"
OUT = ROOT / "plots"
OUT.mkdir(parents=True, exist_ok=True)


def value(row: dict, key: str) -> float | None:
    raw = row.get(key, "")
    if raw in (None, "", "None"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


with (DATA / "head_ab_per_frame.csv").open(encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))

videos = sorted({row["video"] for row in rows})
fig, axes = plt.subplots(len(videos), 2, figsize=(13, 4.8 * len(videos)), squeeze=False)
for row_index, video in enumerate(videos):
    for col, axis in enumerate(("x", "y")):
        ax = axes[row_index][col]
        for algorithm, color in (("pnp", "tab:blue"), ("ratio", "tab:orange")):
            selected = [row for row in rows if row["video"] == video and row["algorithm"] == algorithm]
            x = [value(row, "t") for row in selected]
            key = f"norm_{axis}"
            y = [value(row, key) for row in selected]
            ax.plot(x, y, color=color, linewidth=0.8, label=algorithm)
        ax.axhline(0.0, color="0.45", linewidth=0.6)
        ax.axhline(0.05, color="0.75", linewidth=0.5, linestyle="--")
        ax.axhline(-0.05, color="0.75", linewidth=0.5, linestyle="--")
        ax.set_title(f"{video[:8]} · normalized {axis}")
        ax.set_xlabel("video time (s)")
        ax.set_ylabel("normalized signal")
        ax.grid(alpha=0.2)
        ax.legend(loc="upper right")
fig.tight_layout()
fig.savefig(OUT / "head_normalized_ab.png", dpi=160)
plt.close(fig)

with (DATA / "hand_wrist_group_per_frame.csv").open(encoding="utf-8-sig", newline="") as handle:
    hand_rows = list(csv.DictReader(handle))
fig, axes = plt.subplots(len(videos), 2, figsize=(13, 4.0 * len(videos)), squeeze=False)
for row_index, video in enumerate(videos):
    for col, side in enumerate(("left", "right")):
        ax = axes[row_index][col]
        selected = [row for row in hand_rows if row["video"] == video and row["side"] == side]
        x = [value(row, "t") for row in selected]
        wrist = [1.0 if row["wrist_valid"] == "True" else 0.0 for row in selected]
        group = [1.0 if row["group_valid"] == "True" else 0.0 for row in selected]
        ax.plot(x, wrist, color="tab:red", linewidth=0.7, label="single wrist")
        ax.plot(x, group, color="tab:green", linewidth=0.7, label="shoulder-elbow-wrist")
        ax.set_ylim(-0.1, 1.1)
        ax.set_yticks((0, 1), labels=("missing", "valid"))
        ax.set_title(f"{video[:8]} · {side} arm coverage")
        ax.set_xlabel("video time (s)")
        ax.grid(alpha=0.2)
        ax.legend(loc="lower right")
fig.tight_layout()
fig.savefig(OUT / "arm_wrist_group_coverage.png", dpi=160)
plt.close(fig)

print(OUT)
