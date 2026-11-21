#!/usr/bin/env python3
"""The selection-time figure (Section 7.3): the selection time of clique-grow (solid) and of the edge cover (dashed) over
the pattern length k, one color per topology, from the records of selection_time.py.

  plot_selection_time.py <results dir> <figure dir>     writes <figure dir>/selection_time.{pdf,png}
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "figures"))
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, NullFormatter
import plot_style as ps

USETEX = bool(shutil.which("latex") and shutil.which("dvipng"))
ps.apply(usetex=USETEX)
STYLE = {"chain": (ps.C["blue"], "o", "Chain"), "star": (ps.C["green"], "s", "Star"),
         "cycle": (ps.C["orange"], "^", "Cycle"), "complete": (ps.C["vermillion"], "D", "Compl.")}


def bf(s: str) -> str:
    return r"\textbf{" + s + "}" if USETEX else s


def main() -> None:
    root, out = Path(sys.argv[1]), Path(sys.argv[2])
    recs = [json.loads(p.read_text()) for p in sorted((root / "records").glob("*.json"))]
    out.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(ps.PAPER_FONTS):
        fig, ax = plt.subplots(figsize=(5.6, 4.2))
        for topology, (color, marker, label) in STYLE.items():
            rows = sorted((r for r in recs if r["params"]["topology"] == topology), key=lambda r: r["params"]["k"])
            cg = [(r["params"]["k"], r["clique_grow"]["median_s"]) for r in rows if r["clique_grow"]]
            ec = [(r["params"]["k"], r["edge_cover"]["median_s"]) for r in rows]
            if cg:
                ax.plot([p[0] for p in cg], [p[1] for p in cg], color=color, marker=marker, ms=6, lw=1.8, label=bf(label))
            if ec:
                ax.plot([p[0] for p in ec], [p[1] for p in ec], color=color, lw=1.3, ls="--", alpha=0.7, marker="None")
        ax.set_yscale("log")
        ax.set_xlabel(bf("Pattern Length") + r" $k$")
        ax.set_ylabel(bf("Selection Time [s]"))
        ks = sorted({r["params"]["k"] for r in recs})
        ax.set_xticks(ks if len(ks) <= 7 else [k for k in ks if k % 2 == 1])
        ps.grid_both(ax)
        ax.yaxis.set_major_locator(LogLocator(base=10.0, numticks=15))
        ax.yaxis.set_minor_locator(LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100))
        ax.yaxis.set_minor_formatter(NullFormatter())
        handles, _ = ax.get_legend_handles_labels()
        handles.append(Line2D([], [], color="0.4", lw=1.3, ls="--", label=bf("Edge cover")))
        ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=3, framealpha=1.0, fontsize=13, labelspacing=0.25,
                  columnspacing=1.0, borderpad=0.3, handlelength=1.5)
        for ext in ("pdf", "png"):
            fig.savefig(out / f"selection_time.{ext}", bbox_inches="tight", pad_inches=0.06)
        plt.close(fig)
    print(f"wrote {out / 'selection_time'}.{{pdf,png}}")


if __name__ == "__main__":
    main()
