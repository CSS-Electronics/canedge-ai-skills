"""Shared chart style for canedge reports (matplotlib -> SVG embedded in the HTML report).

Import from any skill script or analysis.py:

    import sys; sys.path.insert(0, ".claude/skills/canedge-reports/scripts")
    from charts import new_figure, save, COLORS, SERIES

    from display_time import DisplayTime
    dt_ = DisplayTime.from_project(project_cfg)            # --tz > project display_tz > the PC's zone
    fig, ax = new_figure(10, 3.2)
    ax.plot([dt_.local(x) for x in t], v, color=COLORS["blue"], label="Pack current [A]")
    local_time_axis(ax, dt_, t[0], t[-1])
    save(fig, out_dir / "fig-current.svg")

Keep charts simple: One message per chart, local time axes (data stays UTC), units in labels, a short caption in
the report spec.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {
    "blue": "#3d85c6",
    "red": "#ff6666",
    "orange": "#ff9900",
    "green": "#10cba9",
    "grey": "#8a8f98",
    "light": "#e9edf2",
    "text": "#2b2f36",
}
# Default series order for multi-line / stacked charts
SERIES = [COLORS["blue"], COLORS["orange"], COLORS["green"], COLORS["red"], "#7b61c9", "#4f5b66", "#f2c14e", "#2fa4d6"]


def apply_style() -> None:
    plt.rcParams.update({
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#c9ced6",
        "axes.labelcolor": COLORS["text"],
        "axes.titlesize": 11,
        "axes.titleweight": "bold",
        "axes.titlecolor": COLORS["text"],
        "axes.labelsize": 9,
        "axes.grid": True,
        "grid.color": "#e6e9ee",
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.prop_cycle": matplotlib.cycler(color=SERIES),
        "xtick.color": "#5b616b",
        "ytick.color": "#5b616b",
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "legend.frameon": False,
        "font.family": "DejaVu Sans",
        "svg.fonttype": "none",  # keep text as text (smaller, searchable)
    })


def new_figure(width: float = 10, height: float = 3.2, nrows: int = 1, sharex: bool = True):
    """Return (fig, ax) or (fig, axes) with the report style applied. Width ~10 fits the report column."""
    apply_style()
    fig, ax = plt.subplots(nrows, 1, figsize=(width, height), sharex=sharex, squeeze=nrows == 1)
    return fig, ax


def local_time_axis(ax, dt_, *times, fmt: str = "%y/%m/%d %H:%M") -> None:
    """Format an x axis of local times: Plot dt_.local(t) values (display_time.DisplayTime, naive local datetimes),
    the label names the zone + its UTC offset(s) at the given times (e.g. the first and last sample)."""
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz="UTC"))  # the values are already local: no shift
    ax.set_xlabel(f"Time, {dt_.label(*times)}")


def mark_event(ax, x, label: str, color: str = COLORS["red"]) -> None:
    """Vertical event marker with a small label at the top - use the same label on every plot of the report."""
    ax.axvline(x, color=color, linewidth=1, linestyle="--", alpha=0.9)
    ax.annotate(label, xy=(x, 1), xycoords=("data", "axes fraction"), xytext=(3, -10),
                textcoords="offset points", fontsize=7, color=color)


def save(fig, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, format=path.suffix.lstrip(".") or "svg", bbox_inches="tight", dpi=150)
    plt.close(fig)
    return path
