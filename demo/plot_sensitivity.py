#!/usr/bin/env python3
"""Plot the small-scale sensitivity results (demo/run_sensitivity.py).

One 2x2 figure, one panel per OAT axis (k, N, B, sigma): total runtime (log) of
native MATCH_RECOGNIZE vs the clique-grow pick. A DNF arm draws as an open marker
at the wall (a measured lower bound, not a runtime); censored cells are left blank.

  python3 demo/plot_sensitivity.py --results out/sensitivity/results.json --out-dir out/sensitivity
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Okabe-Ito colorblind-safe pair; identity also carried by marker shape.
C_MR = "#E69F00"       # orange, circle
C_CG = "#0072B2"       # blue, square
PANELS = [  # (axis key, x param, x label template, log-x?)
    ("pattern_length", "k", "pattern length $k$", False),
    ("table_size", "n", "table size $N$ (rows)", False),
    ("batches", "batches", "number of batches $B$  ($N{{=}}{n}$)", False),
    ("selectivity", "sigma", r"dependent selectivity $\sigma$", True),
]


def series_for(records, axis, param):
    """[(x, mr_rec, cg_rec)] for the cells on this axis, ascending x."""
    rows = [r for r in records if axis in r["axes"]]
    return sorted(((r[param], r["mr"], r["clique_grow"]) for r in rows),
                  key=lambda t: t[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    data = json.loads(Path(a.results).read_text())
    records, wall_ms = data["records"], data["wall_s"] * 1000.0

    fig, axes = plt.subplots(2, 2, figsize=(9.6, 7.2))
    for (axis, param, xlabel, logx), ax in zip(PANELS, axes.flat):
        cells = series_for(records, axis, param)
        for label, key, color, marker in (
                ("MATCH_RECOGNIZE (native)", "mr", C_MR, "o"),
                ("clique-grow (EIMER)", "cg", C_CG, "s")):
            xs, ys = [], []
            for x, mr, cg in cells:
                rec = mr if key == "mr" else cg
                if rec["status"] == "ok":
                    xs.append(x)
                    ys.append(rec["total_ms"])
                elif rec["status"] == "dnf":
                    # measured lower bound at the wall: open marker + annotation
                    ax.plot([x], [wall_ms], marker=marker, ms=8, mfc="none",
                            mec=color, mew=1.8, ls="none", zorder=4)
                    ax.annotate("DNF", (x, wall_ms), textcoords="offset points",
                                xytext=(0, 7), ha="center", fontsize=8, color="#444444")
            ax.plot(xs, ys, marker=marker, ms=7, color=color, lw=2,
                    mec="white", mew=0.8, label=label, zorder=3)
        # per-cell speedup where both arms finished
        for x, mr, cg in cells:
            if (mr["status"] == "ok" and cg["status"] == "ok" and cg["total_ms"]
                    and mr["total_ms"] / cg["total_ms"] >= 3):
                ax.annotate(f"{mr['total_ms'] / cg['total_ms']:.1f}×",
                            (x, (mr["total_ms"] * cg["total_ms"]) ** 0.5),
                            ha="center", fontsize=8, color="#666666", zorder=2)
        ax.set_yscale("log")
        if logx:
            ax.set_xscale("log")
            ax.set_xticks([c[0] for c in cells])
            ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
        else:
            ax.set_xticks(sorted({c[0] for c in cells}))
        ax.axhline(wall_ms, color="#bbbbbb", lw=1, ls=":", zorder=1)
        ax.annotate(f"per-query wall {data['wall_s']}s", (0.02, wall_ms),
                    xycoords=("axes fraction", "data"),
                    ha="left", va="bottom", fontsize=7, color="#999999")
        ax.set_xlabel(xlabel.format(n=data["baseline"]["n"]))
        ax.set_ylabel("total runtime (ms, log)")
        ax.grid(True, which="major", axis="y", color="#e6e6e6", lw=0.7, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.005))
    b = data["baseline"]
    fig.suptitle("Small-scale sensitivity: clique-grow vs native MATCH_RECOGNIZE "
                 f"(chain; baseline $k{{=}}{b['k']}$, $N{{=}}{b['n']}$, "
                 f"$B{{=}}{b['batches']}$, $\\sigma{{=}}{b['sigma']}$)", fontsize=11)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(out / f"sensitivity.{ext}", dpi=170, bbox_inches="tight")
    print(f"plots -> {out / 'sensitivity.png'} (+ .pdf)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
