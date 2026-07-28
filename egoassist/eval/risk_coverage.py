"""risk–coverage 曲线的计算与导出（评委一眼能懂的结果）。"""

from __future__ import annotations

import csv
from typing import List, Sequence, Tuple


def dump_curve(curve: List[Tuple[float, float, float]], path: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["threshold", "coverage", "risk"])
        for tau, cov, risk in curve:
            w.writerow([f"{tau:.6f}", f"{cov:.6f}", f"{risk:.6f}"])


def ascii_plot(curve: List[Tuple[float, float, float]], width: int = 40, height: int = 10) -> str:
    """无需 matplotlib 的终端 risk–coverage 曲线（x=coverage, y=risk）。"""
    pts = sorted([(c, r) for _, c, r in curve])
    if not pts:
        return "(empty)"
    grid = [[" "] * (width + 1) for _ in range(height + 1)]
    for cov, risk in pts:
        x = int(round(cov * width))
        y = int(round((1 - min(risk, 1.0)) * height))
        grid[y][x] = "*"
    lines = ["risk↑ (top=0)  vs  coverage→"]
    for row in grid:
        lines.append("".join(row))
    lines.append("0" + " " * (width - 4) + "cov=1")
    return "\n".join(lines)
