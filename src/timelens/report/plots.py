"""Plots as matplotlib Figures (no GUI backend): frontier, benchmark, conflict and schedule.

Colors follow the dataviz reference palette: one blue series, the critical status red for a
shortfall, and text in ink tokens. Every chart has a table next to it in the UI.
"""

from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Any

import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import StrMethodFormatter

from timelens.analysis.conflict import Conflict
from timelens.analysis.frontier import FrontierPoint
from timelens.units import format_minutes

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#1b2530", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
BLUE, BLUE_LIGHT, CRITICAL = "#2a78d6", "#86b6ef", "#d03b3b"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")  # validated categorical slots 1-4
DAY = 1440


def _chrome(ax: Axes) -> None:
    """Recessive axes: vertical hairline grid, no box except the baseline."""
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK_2, length=0, labelsize=9)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _label_inside(ax: Axes, left: float, width: float, y: float, texts: tuple[str, ...],
                  color: str) -> None:
    """Write the first of `texts` that fits centred in a bar segment with padding (measured)."""
    seg = ax.transData.transform([(left, y), (left + width, y)])
    for text in texts:
        t = ax.text(left + width / 2, y, text, ha="center", va="center", fontsize=8, color=color)
        if t.get_window_extent().width + 12 <= seg[1][0] - seg[0][0]:
            return
        t.remove()


def conflict_figure(c: Conflict) -> Figure:
    """Budget vs demand of one conflict in days. The red segment is the shortfall."""
    fig = Figure(figsize=(7.5, 2.4), facecolor=SURFACE)
    ax = fig.subplots()
    _chrome(ax)
    budget = sum(abs(t.coefficient) * t.value_min for t in c.budget) / DAY
    demand = sum(abs(t.coefficient) * t.value_min for t in c.demand) / DAY
    ax.set_xlim(0, max(budget, demand) * 1.3)
    for y, terms, fill, ink in ((0, c.budget, BLUE_LIGHT, INK), (1, c.demand, BLUE, "#ffffff")):
        left = 0.0
        for t in terms:
            width = abs(t.coefficient) * t.value_min / DAY
            ax.barh(y, width, left=left, height=0.55, color=fill, edgecolor=SURFACE, linewidth=2)
            name = t.label if abs(t.coefficient) == 1 else f"{abs(t.coefficient)} x {t.label}"
            # a narrow segment shows its time; the table under the chart names it
            _label_inside(ax, left, width, y,
                          (name, format_minutes(abs(t.coefficient) * t.value_min)), ink)
            left += width
    gap = c.shortfall_min / DAY
    ax.barh(0, gap, left=budget, height=0.55, color=CRITICAL, edgecolor=SURFACE, linewidth=2,
            hatch="////")
    ax.text(budget + gap + 0.02 * ax.get_xlim()[1], 0,
            f"Shortfall {format_minutes(c.shortfall_min)}", va="center", fontsize=9, color=INK,
            fontweight="bold")
    ax.set_yticks([0, 1], ["Budget", "Demand"], fontsize=10, color=INK)
    ax.invert_yaxis()
    ax.set_xlabel("Days", color=INK_2, fontsize=9)
    fig.tight_layout()
    return fig


def schedule_figure(rows: list[dict[str, Any]]) -> Figure:
    """Worst-case schedule: one bar per task (every contingent duration at its maximum), a tick
    at its latest safe start, and its start slack as text. `rows` is report["schedule"]."""
    points: dict[str, dict[str, Any]] = {}
    for r in rows:
        points.setdefault(r["element"], {"name": r["name"]})[r["point"]] = r
    tasks = sorted((p for p in points.values() if "end" in p), key=lambda p: p["start"]["est_min"])
    fig = Figure(figsize=(7.5, 0.5 * len(tasks) + 1.4), facecolor=SURFACE)
    ax = fig.subplots()
    _chrome(ax)
    ends = [r["lst_min"] for r in rows if r["lst_min"] is not None]
    right = max([p["end"]["est_min"] for p in tasks] + ends + [DAY]) / DAY
    ax.set_xlim(0, right * 1.25)
    for i, p in enumerate(tasks):
        s, e = p["start"], p["end"]
        ax.barh(i, (e["est_min"] - s["est_min"]) / DAY, left=s["est_min"] / DAY, height=0.5,
                color=BLUE, edgecolor=BLUE, linewidth=1)  # the edge keeps 1-hour tasks visible
        lst = s["lst_min"]
        if lst is not None:
            ax.plot([lst / DAY] * 2, [i - 0.36, i + 0.36], color=INK, linewidth=2)
        slack = "unbounded" if s["slack_min"] is None else format_minutes(s["slack_min"])
        x = max(e["est_min"], lst or 0) / DAY + 0.015 * ax.get_xlim()[1]
        ax.text(x, i, f"slack {slack}", va="center", fontsize=8, color=INK_2)
    if ends:
        ax.axvline(max(ends) / DAY, color=MUTED, linestyle="--", linewidth=1)
    ax.set_yticks(range(len(tasks)), [p["name"] for p in tasks], fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.set_xlabel("Days after case start", color=INK_2, fontsize=9)
    handles = [Patch(color=BLUE, label="worst-case run (earliest start)"),
               Line2D([], [], color=INK, linewidth=2, label="latest safe start"),
               Line2D([], [], color=MUTED, linestyle="--", label="latest end")]
    ax.legend(handles=handles[: 3 if ends else 2], loc="lower left", ncols=3, fontsize=8,
              frameon=False, bbox_to_anchor=(0, 1.0))
    fig.tight_layout()
    return fig


def frontier_figure(points: list[FrontierPoint], currency: str, caption: str) -> Figure:
    """Return the frontier: x = risk in %, y = minimum repair cost, one labeled point per alpha."""
    fig = Figure(figsize=(7, 4.5))
    ax = fig.subplots()
    ok = [p for p in points if not math.isnan(p.cost)]
    ax.plot([p.risk * 100 for p in ok], [p.cost for p in ok], "o-", color="#1f5f99")
    for p in ok:
        ax.annotate(f"alpha={p.alpha:.2f}", (p.risk * 100, p.cost), textcoords="offset points",
                    xytext=(6, 6), fontsize=8)
    for p in points:
        if math.isnan(p.cost):  # plotted as a marker so the axis range includes it
            ax.plot(p.risk * 100, 0, "x", color="#b03030", markersize=9)
            ax.annotate(f"alpha={p.alpha:.2f}: no repair", (p.risk * 100, 0),
                        textcoords="offset points", xytext=(6, 6), fontsize=8, color="#b03030")
    ax.set_xlabel("Risk: test cases outside a calibrated range (%)")
    ax.set_ylabel(f"Minimum repair cost ({currency}, assumption)")
    ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))  # as in the cost table
    ax.set_title("TimeLens risk vs cost frontier")
    ax.grid(alpha=0.3)
    fig.text(0.01, 0.01, textwrap.fill(caption, 120), fontsize=7)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    return fig


def frontier_png(points: list[FrontierPoint], path: Path, currency: str, caption: str) -> None:
    """Write the frontier figure as a PNG."""
    frontier_figure(points, currency, caption).savefig(path, dpi=130)


def bench_figure(table: pd.DataFrame) -> Figure:
    """Return median wall time per model size and check, log scale."""
    fig = Figure(figsize=(7, 4.5))
    ax = fig.subplots()
    med = table.groupby("tasks").median(numeric_only=True)
    for color, (col, label) in zip(SERIES, (
            ("consistency_s", "consistency"), ("sc_s", "strong controllability"),
            ("dc_s", "DC (fast engine)"), ("dc_prov_s", "DC with provenance")), strict=True):
        if col in med and med[col].notna().any():
            ax.plot(med.index, med[col], "o-", label=label, color=color)
    ax.set_yscale("log")
    ax.set_xlabel("tasks per model")
    ax.set_ylabel("median wall time (s)")
    ax.set_title("TimeLens benchmark")
    ax.grid(alpha=0.3, which="both")
    ax.legend()
    fig.tight_layout()
    return fig


def bench_png(table: pd.DataFrame, path: Path) -> None:
    """Write the benchmark figure as a PNG."""
    bench_figure(table).savefig(path, dpi=130)
