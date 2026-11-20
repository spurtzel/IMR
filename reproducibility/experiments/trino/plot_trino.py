#!/usr/bin/env python3
"""The figures of the Trino experiments from the records of a run.

  python3 plot_trino.py fig8 <results dir> <figs dir>           -> trino_fig8_N_k.{pdf,png}
  python3 plot_trino.py fig9 <results dir> <figs dir>           -> trino_fig9_W_B.{pdf,png}
  python3 plot_trino.py fig10 <results dir> <figs dir>          -> trino_fig10a_N_k.{pdf,png}, trino_fig10b_schedule.{pdf,png}, trino_fig10s_skew.{pdf,png}
  python3 plot_trino.py kleene <results dir> <figs dir>         -> trino_fig11_kleene.{pdf,png}
  python3 plot_trino.py budget_shift <results dir> <figs dir>   -> trino_fig11_budget_shift.{pdf,png}
  python3 plot_trino.py topology_memory <results dir> <figs dir> -> trino_topology.{pdf,png}, trino_memory.{pdf,png}
  python3 plot_trino.py realworld <results dir> <figs dir>      -> trino_fig12_realworld.{pdf,png}
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "figures"))
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, NullFormatter
import plot_style as ps

USETEX = bool(shutil.which("latex") and shutil.which("dvipng"))
ps.apply(usetex=USETEX)
plt.rcParams.update(ps.PAPER_ROW_FONTS)


def bf(s: str) -> str:
    return rf"\textbf{{{s}}}" if USETEX else s


def records(root: Path) -> list:
    return [json.loads(f.read_text()) for f in sorted((root / "records").glob("*.json"))]


def secs(arm: dict | None):
    if not arm:
        return "missing"
    return arm["total_ms"] / 1000.0 if arm.get("status") == "ok" and arm.get("total_ms") is not None else None


def klabel(n: int) -> str:
    return f"{n // 1000}k" if n % 1000 == 0 else f"{n / 1000:g}k"


def draw(ax, xs, ys, color, marker, ls, ceiling, dnf_marker, **kw):
    fx = [x for x, y in zip(xs, ys) if isinstance(y, float)]
    fy = [y for y in ys if isinstance(y, float)]
    cx = [x for x, y in zip(xs, ys) if y is None]
    if fx:
        ax.plot(fx, fy, color=color, marker=marker, ls=ls, ms=6.5, lw=1.8, zorder=3, **kw)
    if cx:
        if fx and cx[0] > fx[-1]:
            ax.plot([fx[-1], cx[0]], [fy[-1], ceiling], color=color, ls=":", lw=1.1, alpha=0.7, marker="none", zorder=2)
        if dnf_marker == "hollow":
            ax.scatter(cx, [ceiling] * len(cx), facecolors="white", edgecolors=color, marker=marker, s=60, linewidths=1.6, zorder=5)
        else:
            ax.scatter(cx, [ceiling] * len(cx), marker="x", color=color, s=95, lw=2.4, zorder=5)


def y_range(values, any_dnf):
    vals = [v for v in values if isinstance(v, float)]
    lo, hi = (min(vals), max(vals)) if vals else (0.1, 10.0)
    ceiling = hi * 4.0
    return lo / 2.0, ceiling, (ceiling * 2.0 if any_dnf else hi * 2.5)


def finish(fig, axes, handles, ncol, out: Path, name: str, extra_legends=()):
    for ax in axes:
        ps.grid_both(ax)
    fig.canvas.draw()
    p0, p1 = axes[0].get_position(), axes[-1].get_position()
    leg = fig.legend(handles=handles, loc="lower center", bbox_to_anchor=((p0.x0 + p1.x1) / 2, p0.y1 + 0.015), ncol=ncol, frameon=True, fontsize=14,
                     borderpad=0.25, handlelength=2.2, handletextpad=0.35, columnspacing=1.0)
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"{name}.{ext}", bbox_inches="tight", bbox_extra_artists=[leg, *extra_legends], pad_inches=0.03, dpi=200)
    print(f"wrote {out / name}.{{pdf,png}}")


def fig8(root: Path, out: Path):
    recs = records(root)
    style = {"mr": (ps.C["orange"], "^", "--", "T: MR"), "gdyn": (ps.C["purple"], "P", "--", "T: GDYN"), "eimer": (ps.C["sky"], "s", "--", "T: EIMER")}
    panels = []
    for axis, key, xlabel in (("N", "n", r"\textbf{Table Size} $\boldsymbol{N}$" if USETEX else "Table Size $N$"),
                              ("k", "k", r"\textbf{Pattern Length} $\boldsymbol{k}$" if USETEX else "Pattern Length $k$")):
        rs = sorted((r for r in recs if axis in r["params"]["axes"]), key=lambda r: r["params"][key])
        panels.append((axis, key, xlabel, rs))
    allv = [secs(r.get(a)) for _, _, _, rs in panels for r in rs for a in style]
    lo, ceiling, top = y_range(allv, any(v is None for v in allv))
    fig, axes = ps.figure("text", nrows=1, ncols=2, h=3.4, sharey=True)
    for ax, (axis, key, xlabel, rs) in zip(axes, panels):
        xs = [r["params"][key] for r in rs]
        pos = list(range(len(xs)))
        for arm, (color, marker, ls, _) in style.items():
            ys = [secs(r.get(arm)) for r in rs]
            draw(ax, pos, ys, color, marker, ls, ceiling, "hollow")
        ax.set_yscale("log")
        ax.set_ylim(lo, top)
        ax.set_xticks(pos)
        ax.set_xticklabels([klabel(x) if axis == "N" else str(x) for x in xs], rotation=(45 if axis == "N" else 0))
        ax.set_xlim(-0.4, max(len(xs) - 0.6, 0.6))
        ax.set_xlabel(xlabel)
    axes[0].set_ylabel(bf("Runtime [s]"))
    handles = [Line2D([], [], color=c, marker=m, ls=ls, lw=1.8, ms=6.5, label=bf(lab)) for c, m, ls, lab in style.values()]
    finish(fig, axes, handles, 3, out, "trino_fig8_N_k")


def fig9(root: Path, out: Path):
    recs = records(root)
    arms = {"mr": (ps.C["vermillion"], "v", "MR"), "prefilter": (ps.C["green"], "s", "prefilter"), "eimer": (ps.C["blue"], "D", "EIMER")}
    wrecs = [r for r in recs if "W" in r["params"]["axes"]]
    brecs = [r for r in recs if "B" in r["params"]["axes"]]

    def last_rate(r):
        return (r.get("type_rates") or {}).get("last")

    sigmas = sorted({r["params"]["sigma"] for r in wrecs}, reverse=True)
    lasts = sorted({last_rate(r) for r in wrecs}, key=lambda v: -(v or 0.0))
    by_rate = len(lasts) > 1 and len(sigmas) == 1
    groups = lasts if by_rate else sigmas
    ns = sorted({r["params"]["n"] for r in brecs})
    variants = (dict(ls="-"), dict(ls=(0, (1, 1.3)), mfc="none", mew=1.6), dict(ls="-.", mfc="none", mew=1.6))
    allv = [secs(r.get(a)) for r in recs for a in arms]
    lo, ceiling, top = y_range(allv, any(v is None for v in allv))
    both = bool(wrecs) == bool(brecs)
    fig, axs = ps.figure(ps.WIDTHS["text"] * (1.0 if both else 0.6), nrows=1, ncols=2 if both else 1, h=3.4, sharey=True)
    axl, axr = axs if both else ((axs, None) if wrecs else (None, axs))
    axes = [ax for ax in (axl, axr) if ax is not None]
    inner = []
    if axl is not None:
        ws = sorted({r["params"]["w_ms"] for r in wrecs})
        for g, st in zip(groups, variants):
            by = {r["params"]["w_ms"]: r for r in wrecs if (last_rate(r) if by_rate else r["params"]["sigma"]) == g}
            xs = [w for w in ws if w in by]
            for arm, (color, marker, _) in arms.items():
                draw(axl, xs, [secs(by[w].get(arm)) for w in xs], color, marker, st["ls"], ceiling, "cross",
                     **{k: v for k, v in st.items() if k != "ls"})
        axl.set_xscale("log")
        axl.set_xticks(ws)
        axl.set_xticklabels([str(w) for w in ws], rotation=30)
        axl.xaxis.set_minor_formatter(NullFormatter())
        axl.set_xlim(ws[0] / 1.3, ws[-1] * 1.3)
        axl.set_xlabel(r"\textbf{Window Size} $\boldsymbol{W}$ \textbf{[ms]}" if USETEX else "Window Size $W$ [ms]")
        if by_rate:
            pct = (lambda v: rf"{v * 100:g}\,\%") if USETEX else (lambda v: f"{v * 100:g} %")
            h = [Line2D([], [], color="0.25", marker="o", lw=1.7, label=("last type " if i == 0 else "") + pct(v), **st) for i, (v, st) in enumerate(zip(lasts, variants))]
            inner.append(axl.legend(handles=h, loc="upper left", ncol=len(h), fontsize=12, borderpad=0.2, handlelength=2.0, handletextpad=0.3, columnspacing=0.8))
        elif len(sigmas) > 1:
            h = [Line2D([], [], color="0.25", marker="o", lw=1.7, label=rf"$\sigma{{=}}{s:g}$", **st) for s, st in zip(sigmas, variants)]
            inner.append(axl.legend(handles=h, loc="upper left", ncol=len(h), fontsize=12, borderpad=0.2, handlelength=2.0, handletextpad=0.3, columnspacing=0.8))
        elif sigmas:
            axl.text(0.03, 0.97, rf"$\sigma{{=}}{sigmas[0]:g}$", transform=axl.transAxes, ha="left", va="top", fontsize=13)
        b_w = sorted({r["params"]["batches"] for r in wrecs})
        fixed = ([rf"$\sigma{{=}}{sigmas[0]:g}$"] if by_rate else []) + ([rf"$B{{=}}{b_w[0]}$"] if len(b_w) == 1 else [])
        if fixed:
            x, y, ha = (0.045, 0.85, "left") if inner else (0.97, 0.97, "right")
            axl.text(x, y, ", ".join(fixed), transform=axl.transAxes, ha=ha, va="top", fontsize=12, color="0.25")
    if axr is not None:
        bs = sorted({r["params"]["batches"] for r in brecs})
        for n, st in zip(ns, variants):
            by = {r["params"]["batches"]: r for r in brecs if r["params"]["n"] == n}
            xs = [b for b in bs if b in by]
            for arm, (color, marker, _) in arms.items():
                draw(axr, xs, [secs(by[b].get(arm)) for b in xs], color, marker, st["ls"], ceiling, "cross",
                     **{k: v for k, v in st.items() if k != "ls"})
        axr.set_xscale("log")
        axr.set_xticks(bs)
        axr.set_xticklabels([str(b) for b in bs], rotation=30)
        axr.xaxis.set_minor_formatter(NullFormatter())
        axr.set_xlim(bs[0] / 1.3, bs[-1] * 1.3)
        axr.set_xlabel(r"\textbf{\# Batches} $\boldsymbol{B}$" if USETEX else "# Batches $B$")
        if len(ns) > 1:
            h = [Line2D([], [], color="0.25", marker="o", lw=1.7, label=rf"$N{{=}}${klabel(n)}", **st) for n, st in zip(ns, variants)]
            inner.append(axr.legend(handles=h, loc="upper left", ncol=len(h), fontsize=12, borderpad=0.2, handlelength=2.0, handletextpad=0.3, columnspacing=0.8))
        elif ns:
            axr.text(0.03, 0.97, rf"$N{{=}}${klabel(ns[0])}", transform=axr.transAxes, ha="left", va="top", fontsize=13)
    for ax in axes:
        ax.set_yscale("log")
        ax.set_ylim(lo if both else lo / 4.0, top * 6)
        ax.yaxis.set_major_locator(LogLocator(base=10.0, numticks=15))
        ax.yaxis.set_minor_locator(LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100))
        ax.yaxis.set_minor_formatter(NullFormatter())
    axes[0].set_ylabel(bf("Runtime [s]"))
    if any(v is None for v in allv):
        axes[-1].text(0.97, 0.03, (r"\texttimes\ did not finish" if USETEX else "x did not finish"), transform=axes[-1].transAxes, ha="right", va="bottom", fontsize=12, color="0.35",
                      bbox=dict(facecolor="white", edgecolor="none", alpha=0.8, pad=1.5), zorder=6)
    handles = [Line2D([], [], color=c, marker=m, ls="-", lw=1.8, ms=6.5, label=bf(lab)) for c, m, lab in arms.values()]
    finish(fig, axes, handles, 3, out, "trino_fig9_W_B")


F10 = {"eimer": dict(color=ps.C["blue"], marker="D", ls="-", hatch="diag", label="EIMER", maint="update_ms", query="compose_ms"),
       "gdyn": dict(color="#A0287A", marker="o", ls="-.", hatch="grid", label="GDYN", maint="update_ms", query="query_ms")}


def _f10_legend_handles():
    return [(ps.HatchSwatch(st["color"], st["hatch"]), Line2D([], [], color=st["color"], marker=st["marker"], ls=st["ls"], lw=1.8, ms=6.5)) for st in F10.values()], \
           [bf(st["label"]) for st in F10.values()]


def _share(arm: dict):
    if not arm or arm.get("status") != "ok":
        return None
    st = F10["eimer"] if "compose_ms" in arm else F10["gdyn"]
    return float(arm.get(st["maint"]) or 0.0), float(arm.get(st["query"]) or 0.0)


def fig10a(recs: list, out: Path):
    recs = [r for r in recs if r["params"]["part"] == "a"]
    if not recs:
        return
    classes = [c for c in ("acyclic", "cyclic") if any(r["params"]["cls"] == c for r in recs)]
    panels = []
    for cls in classes:
        for axis, key in (("N", "n"), ("k", "k")):
            rs = sorted((r for r in recs if r["params"]["cls"] == cls and axis in r["params"]["axes"]), key=lambda r: r["params"][key])
            if rs:
                panels.append((cls, axis, key, rs))
    times = [secs(r.get(a)) for *_, rs in panels for r in rs for a in F10]
    lo, ceiling, top = y_range(times, any(v is None for v in times))
    def state_mb(arm):
        st = (arm or {}).get("state") or {}
        v = st.get("bytes") if st.get("bytes") is not None else st.get("uncompressed_bytes")
        return None if v is None else v / 1e6

    states = [v for *_, rs in panels for r in rs for a in F10 if (v := state_mb(r.get(a))) is not None]
    s_lo, s_hi = (min(states) / 3.0, max(states) * 30.0) if states else (0.01, 1.0)
    n = len(panels)
    fig = plt.figure(figsize=(3.3 * n + 0.6, 4.7))
    fig.set_layout_engine("none")
    gap = [1.0 if (i == 0 or panels[i][0] == panels[i - 1][0]) else 1.25 for i in range(n)]
    left, right, wsp = 0.085, 0.925, 0.035
    unit = (right - left - wsp * sum(g for g in gap[1:])) / n
    x0 = left
    top_y0, top_h, strip_y0, strip_h = 0.40, 0.47, 0.17, 0.14
    width = 0.36
    for i, (cls, axis, key, rs) in enumerate(panels):
        if i:
            x0 += unit + wsp * gap[i]
        ax = fig.add_axes([x0, top_y0, unit, top_h])
        axs = ax.twinx()
        ax.set_zorder(axs.get_zorder() + 1)
        ax.patch.set_visible(False)
        st_ax = fig.add_axes([x0, strip_y0, unit, strip_h])
        pos = list(range(len(rs)))
        for j, (arm, st) in enumerate(F10.items()):
            off = (j - 0.5) * width
            ys = [secs(r.get(arm)) for r in rs]
            fx = [x for x, y in zip(pos, ys) if isinstance(y, float)]
            fy = [y for y in ys if isinstance(y, float)]
            cx = [x for x, y in zip(pos, ys) if y is None]
            if fx:
                ax.plot(fx, fy, color=st["color"], marker=st["marker"], ls=st["ls"], ms=7, lw=1.9, zorder=4)
            if cx:
                if fx and cx[0] > fx[-1]:
                    ax.plot([fx[-1], cx[0]], [fy[-1], ceiling], color=st["color"], ls=":", lw=1.1, alpha=0.7, marker="none", zorder=3)
                ax.scatter(cx, [ceiling] * len(cx), facecolors="white", edgecolors=st["color"], marker="o", s=80, linewidths=1.6, zorder=5)
                ax.scatter(cx, [ceiling] * len(cx), color=st["color"], marker="x", s=30, linewidths=1.4, zorder=6)
            for x, r in zip(pos, rs):
                a = r.get(arm) or {}
                if state_mb(a) is not None:
                    v = max(state_mb(a), s_lo * 1.05)
                    ps.hatched_bar(axs, x + off, v - s_lo, width, s_lo, st["color"], st["hatch"], zorder=1)
                sh = _share(a)
                if sh and sum(sh) > 0:
                    m = 100.0 * sh[0] / sum(sh)
                    ps.hatched_bar(st_ax, x + off, m, width, 0, st["color"], st["hatch"], zorder=2, fill_alpha=0.45)
                    st_ax.bar(x + off, 100.0 - m, width=width, bottom=m, facecolor="white", edgecolor=st["color"], linewidth=0.8, zorder=2)
                elif a.get("status") == "dnf":
                    st_ax.scatter([x + off], [50], facecolors="white", edgecolors=st["color"], marker="o", s=45, linewidths=1.3, zorder=3)
                    st_ax.scatter([x + off], [50], color=st["color"], marker="x", s=16, linewidths=1.1, zorder=4)
        ax.set_yscale("log"); ax.set_ylim(lo, top * 3)
        axs.set_yscale("log"); axs.set_ylim(s_lo, s_hi)
        pad = max(0.0, (3 - len(rs)) / 2.0)
        for a_ in (ax, axs, st_ax):
            a_.set_xlim(-0.6 - pad, len(rs) - 0.4 + pad)
        ax.set_xticks(pos); ax.set_xticklabels([])
        ps.grid_both(ax)
        axs.grid(False)
        st_ax.set_ylim(0, 100); st_ax.set_yticks([0, 100]); st_ax.set_facecolor("0.93")
        st_ax.set_xticks(pos)
        st_ax.set_xticklabels([klabel(r["params"][key]) if axis == "N" else str(r["params"][key]) for r in rs])
        st_ax.set_xlabel(r"\textbf{Table Size} $\boldsymbol{N}$" if (USETEX and axis == "N") else r"\textbf{Pattern Length} $\boldsymbol{k}$" if USETEX
                         else ("Table Size $N$" if axis == "N" else "Pattern Length $k$"))
        ax.text(0.045, 0.95, bf(cls.capitalize()), transform=ax.transAxes, ha="left", va="top", fontsize=15,
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="0.6", lw=0.8), zorder=10)
        if i == 0:
            ax.set_ylabel(bf("Runtime [s]")); st_ax.set_ylabel(bf(r"[\%]" if USETEX else "[%]"), fontsize=13)
        else:
            ax.set_yticklabels([]); st_ax.set_yticklabels([])
        if not states:
            axs.set_yticks([]); axs.set_yticklabels([]); axs.minorticks_off()
        elif i == n - 1:
            axs.set_ylabel(bf("State [MB]"), color="0.35"); axs.tick_params(axis="y", colors="0.35")
        else:
            axs.set_yticklabels([])
        if axis == "N" or i == 0 or panels[i - 1][0] != cls:
            from matplotlib.patches import Patch
            st_ax.legend(handles=[Patch(fc="0.6", ec="0.3"), Patch(fc="white", ec="0.3")], labels=[bf("Maintenance /"), bf("query")], loc="lower left",
                         bbox_to_anchor=(0.35, 0.98), ncol=2, frameon=False, fontsize=14, handlelength=1.4, handletextpad=0.4, columnspacing=0.6, borderpad=0.1)
    handles, labels = _f10_legend_handles()
    leg = fig.legend(handles=handles, labels=labels, handler_map=ps.HATCH_HANDLER_MAP, loc="lower center", bbox_to_anchor=((left + right) / 2, top_y0 + top_h + 0.012),
                     ncol=2, frameon=True, fontsize=15, borderpad=0.3, handlelength=3.0, handletextpad=0.5, columnspacing=1.4)
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_fig10a_N_k.{ext}", bbox_inches="tight", bbox_extra_artists=[leg], pad_inches=0.03, dpi=200)
    print(f"wrote {out / 'trino_fig10a_N_k'}.{{pdf,png}}")


def fig10b(recs: list, out: Path):
    recs = sorted((r for r in recs if r["params"]["part"] == "b"), key=lambda r: -float(r["params"]["pct"]))
    if not recs:
        return
    fig = plt.figure(figsize=(12.6, 2.9))
    fig.set_layout_engine("none")
    ax = fig.add_axes([0.085, 0.27, 0.84, 0.66])
    width = 0.36
    pos = list(range(len(recs)))
    tops = []
    for j, (arm, st) in enumerate(F10.items()):
        off = (j - 0.5) * width
        for x, r in zip(pos, recs):
            sh = _share(r.get(arm))
            if sh:
                ps.hatched_bar(ax, x + off, sh[0] / 1000.0, width, 0, st["color"], st["hatch"], zorder=2, fill_alpha=0.45)
                ax.bar(x + off, sh[1] / 1000.0, width=width, bottom=sh[0] / 1000.0, facecolor="white", edgecolor=st["color"], linewidth=1.0, zorder=2)
                tops.append(sum(sh) / 1000.0)
    ymax = (max(tops) if tops else 1.0) * 1.22
    for j, (arm, st) in enumerate(F10.items()):
        for x, r in zip(pos, recs):
            if (r.get(arm) or {}).get("status") == "dnf":
                xx = x + (j - 0.5) * width
                ax.scatter([xx], [ymax * 0.93], facecolors="white", edgecolors=st["color"], marker="o", s=80, linewidths=1.6, zorder=5)
                ax.scatter([xx], [ymax * 0.93], color=st["color"], marker="x", s=30, linewidths=1.4, zorder=6)
    ax.set_ylim(0, ymax)
    pad = max(0.0, (4 - len(recs)) / 2.0)
    ax.set_xlim(-0.6 - pad, len(recs) - 0.4 + pad)
    ax.set_xticks(pos)
    ax.set_xticklabels([(rf"{r['params']['pct']:g}\%" if USETEX else f"{r['params']['pct']:g}%") for r in recs], rotation=25)
    ax.set_ylabel(bf("Time [s]"))
    ax.grid(axis="y", ls=":", lw=0.6, color="0.8"); ax.grid(axis="x", visible=False)
    from matplotlib.patches import Patch
    handles = [ps.HatchSwatch(st["color"], st["hatch"], fill_alpha=0.45) for st in F10.values()] + [Patch(fc="0.6", ec="0.3"), Patch(fc="white", ec="0.3")]
    labels = [bf(st["label"]) for st in F10.values()] + [bf("maintenance"), bf("query")]
    p0 = recs[0]["params"]
    leg = ax.legend(handles=handles, labels=labels, handler_map=ps.HATCH_HANDLER_MAP, loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=4,
                    fontsize=13.5, borderpad=0.3, handlelength=1.8, handletextpad=0.4, columnspacing=1.0)
    ax.set_xlabel(bf(f"Queried Batches ({p0['cls']} query)"))
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_fig10b_schedule.{ext}", bbox_inches="tight", bbox_extra_artists=[leg], pad_inches=0.03, dpi=200)
    print(f"wrote {out / 'trino_fig10b_schedule'}.{{pdf,png}}")


def fig10s(recs: list, out: Path):
    recs = [r for r in recs if "skew" in r["params"].get("axes", [])]
    if not recs:
        return
    names = (("uniform", "uniform"), ("rare_ends", "first/last rare"), ("rare_interior", "interior rare"))
    classes = [c for c in ("acyclic", "cyclic") if any(r["params"]["cls"] == c for r in recs)]
    fig, axes = plt.subplots(1, len(classes), figsize=(4.6 * len(classes), 3.2), sharey=True, squeeze=False)
    width, any_dnf = 0.36, False
    vals = [secs(r.get(a)) for r in recs for a in F10]
    top = max([v for v in vals if isinstance(v, float)] or [1.0]) * 1.25
    for ax, cls in zip(axes[0], classes):
        for x, (key, _) in enumerate(names):
            r = next((r for r in recs if r["params"]["cls"] == cls and r["params"].get("skew", "uniform") == key), None)
            for j, (arm, st) in enumerate(F10.items()):
                v, xx = secs((r or {}).get(arm)), x + (j - 0.5) * width
                if isinstance(v, float):
                    ps.hatched_bar(ax, xx, v, width, 0, st["color"], st["hatch"], zorder=2, fill_alpha=0.45)
                elif v is None:
                    any_dnf = True
                    ax.scatter([xx], [top * 0.93], facecolors="white", edgecolors=st["color"], marker="o", s=80, linewidths=1.6, zorder=5)
                    ax.scatter([xx], [top * 0.93], color=st["color"], marker="x", s=30, linewidths=1.4, zorder=6)
        ax.set_ylim(0, top)
        ax.set_xlim(-0.6, len(names) - 0.4)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels([bf(lab) for _, lab in names], rotation=20, fontsize=13)
        ax.set_title(bf(cls.capitalize()), fontsize=15)
        ax.grid(axis="y", ls=":", lw=0.6, color="0.8"); ax.grid(axis="x", visible=False)
    axes[0][0].set_ylabel(bf("Runtime [s]"))
    handles = [ps.HatchSwatch(st["color"], st["hatch"], fill_alpha=0.45) for st in F10.values()]
    leg = fig.legend(handles=handles, labels=[bf(st["label"]) for st in F10.values()], handler_map=ps.HATCH_HANDLER_MAP, loc="lower center",
                     bbox_to_anchor=(0.5, 0.98), ncol=2, fontsize=13.5, borderpad=0.3, handlelength=1.8, handletextpad=0.4, columnspacing=1.0)
    fig.tight_layout(w_pad=0.6)
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_fig10s_skew.{ext}", bbox_inches="tight", bbox_extra_artists=[leg], pad_inches=0.05, dpi=200)
    plt.close(fig)
    print(f"wrote {out / 'trino_fig10s_skew'}.{{pdf,png}}" + (" (x: did not finish)" if any_dnf else ""))


def fig10(root: Path, out: Path):
    recs = records(root)
    fig10a(recs, out)
    fig10b(recs, out)
    fig10s(recs, out)


def kleene(root: Path, out: Path):
    import numpy as np
    recs = records(root)
    pay = [r for r in recs if r["params"]["part"] == "payoff"]
    swp = sorted((r for r in recs if r["params"]["part"] == "sweep"), key=lambda r: r["params"]["l_run"])
    c_mr, c_ns, c_st = ps.C["vermillion"], ps.C["orange"], ps.C["blue"]
    fig, (axa, axb) = ps.figure("text", nrows=1, ncols=2, h=3.4, sharey=False)
    if pay:
        nb = min(len(r["per_batch"]) for r in pay)
        mean = lambda k: np.mean([[r["per_batch"][i][k] for i in range(nb)] for r in pay], axis=0)
        batch = np.arange(1, nb + 1)
        every = max(1, nb // 8)
        axa.plot(batch, np.cumsum(mean("rescan_maint_ms")) / 1000.0, color=c_ns, marker="s", ls="--", ms=6.5, lw=1.8, markevery=every, label=bf("EIMER (no stitch)"))
        axa.plot(batch, np.cumsum(mean("stitch_maint_ms")) / 1000.0, color=c_st, marker="D", ls="-", ms=6.5, lw=1.8, markevery=every, label=bf("EIMER (stitch)"))
        axa.legend(loc="upper left", framealpha=1.0, fontsize=11.5, labelspacing=0.25, borderpad=0.3, handlelength=1.6)
    axa.set_xlabel(r"\textbf{\# Batches} $\boldsymbol{B}$" if USETEX else "# Batches $B$")
    axa.set_ylabel(bf("Cum. Maint. [s]"))
    ps.grid_both(axa)
    if swp:
        ls_ = [r["params"]["l_run"] for r in swp]
        pos = list(range(len(ls_)))
        series = (("mr", c_mr, "v", "-", "MR"), ("eimer_nostitch", c_ns, "s", "--", "EIMER (no stitch)"), ("eimer_stitch", c_st, "D", "-", "EIMER (stitch)"))
        vals = [secs(r.get(a)) for r in swp for a, *_ in series]
        lo, ceiling, top = y_range(vals, any(v is None for v in vals))
        for arm, color, marker, lst, label in series:
            draw(axb, pos, [secs(r.get(arm)) for r in swp], color, marker, lst, ceiling, "cross")
        axb.set_yscale("log")
        axb.set_ylim(lo, top * 8)
        axb.set_xticks(pos)
        axb.set_xticklabels([str(l) for l in ls_], rotation=30)
        axb.set_xlim(-0.4, max(len(ls_) - 0.6, 0.6))
        axb.legend(handles=[Line2D([], [], color=c, marker=m, ls=l, lw=1.8, ms=6.5, label=bf(lab)) for _, c, m, l, lab in series], loc="upper left",
                   framealpha=1.0, fontsize=11.5, labelspacing=0.25, borderpad=0.3, handlelength=1.6)
        if any(v is None for v in vals):
            axb.text(0.97, 0.03, (r"\texttimes\ did not finish" if USETEX else "x did not finish"), transform=axb.transAxes, ha="right", va="bottom", fontsize=12, color="0.35")
    axb.set_xlabel(r"\textbf{Run Length} $\boldsymbol{L}$" if USETEX else "Run Length $L$")
    axb.set_ylabel(bf("Runtime [s]"))
    ps.grid_both(axb)
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_fig11_kleene.{ext}", bbox_inches="tight", pad_inches=0.05, dpi=200)
    print(f"wrote {out / 'trino_fig11_kleene'}.{{pdf,png}}")


def _runtime_at(rec: dict, m: float, with_anchor: bool):
    steps = [s for s in rec["steps"] if s["plan"] != "(empty)"]
    floor = next((s for s in rec["steps"] if s["plan"] == "(empty)"), None)
    pure = next((s for s in steps if s["predicted_peak_bytes"] <= m), None)
    cand = []
    if pure is not None and pure.get("measured_ms") is not None:
        cand.append(pure["measured_ms"])
    floor_bytes = None if floor is None else (floor["measured_peak_bytes"] if floor.get("measured_peak_bytes") is not None else floor.get("modelled_state_bytes") or 0)
    if with_anchor and floor is not None and floor.get("measured_ms") is not None and floor_bytes <= m:
        cand.append(floor["measured_ms"])
    return min(cand) if cand else None


def _budget_panel(ax, recs: list):
    import statistics
    finished = [r for r in recs if r.get("mr_total_ms")]
    cells = [r for r in finished if any(s["plan"] != "(empty)" for s in r["steps"])]
    if not cells:
        ax.text(0.5, 0.5, "the picks maintain no view" if finished else "native MR did not finish\non any workload",
                transform=ax.transAxes, ha="center", va="center", fontsize=13, color="0.35")
        return
    grid = [10 ** (-i / 30) for i in range(0, 91)]
    smax = {r["cell"]: next(s["predicted_peak_bytes"] for s in r["steps"] if s["plan"] != "(empty)") for r in cells}
    per_cell = {r["cell"]: [(r["mr_total_ms"] / ms if (ms := _runtime_at(r, smax[r["cell"]] * f * 1.0001, True)) else None) for f in grid] for r in cells}
    n_feas = [sum(1 for v in per_cell.values() if v[i]) for i in range(len(grid))]
    full_until = next((i for i, n in enumerate(n_feas) if n < len(cells)), len(grid))
    if full_until < 2:
        full_until = min(2, len(grid))
    med = [statistics.median([v[i] for v in per_cell.values() if v[i]]) if n_feas[i] else None for i in range(len(grid))]
    lo = [min(v[i] for v in per_cell.values() if v[i]) for i in range(full_until)]
    hi = [max(v[i] for v in per_cell.values() if v[i]) for i in range(full_until)]
    green = ps.C["green"]
    ax.fill_between(grid[:full_until], lo, hi, color=green, alpha=0.16, lw=0, zorder=1, label=bf("min--max" if USETEX else "min-max"))
    ax.plot(grid[:full_until], med[:full_until], color=green, lw=3.5, marker="None", label=bf("median"), zorder=5)
    idx, prev = [], None
    for i in range(full_until):
        v = med[i]
        if prev is not None and v is not None and abs(v - prev) > 0.03 * prev:
            idx.append(i)
        prev = v
    ax.scatter([grid[i] for i in idx], [med[i] for i in idx], s=46, color=green, edgecolors="white", linewidths=1.0, marker="o", zorder=6)
    worst = []
    for i, f in enumerate(grid[:full_until]):
        pv = [r["mr_total_ms"] / ms for r in cells if (ms := _runtime_at(r, smax[r["cell"]] * f * 1.0001, False))]
        worst.append(min(pv) if pv else None)
    wx = [g for g, w in zip(grid, worst) if w is not None]
    ax.plot(wx, [w for w in worst if w is not None], color=ps.C["blue"], lw=2.4, ls=":", marker="None", label=bf("worst (drop-only)"), zorder=4)
    ax.axhline(1.0, color=ps.C["vermillion"], lw=2.6, zorder=3)
    ax.annotate(bf("MR runtime"), (grid[full_until - 1], 1.0), xytext=(-4, -6), textcoords="offset points", fontsize=13, ha="right", va="top", color=ps.C["vermillion"])
    ax.set_xscale("log"); ax.set_yscale("log")
    right = grid[full_until - 1]
    ax.set_xlim(1.05, right)
    ticks = [d for d in (1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.001) if right <= d <= 1.05]
    if len(ticks) < 2:
        ticks = [d for d in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3) if right <= d <= 1.05]
    ax.set_xticks(ticks); ax.set_xticklabels([f"{d:g}" for d in ticks], rotation=30)
    ax.get_xaxis().set_minor_formatter(plt.NullFormatter())
    vals = [x for x in lo + hi + [w for w in worst if w] if x]
    ax.set_ylim(min(min(vals), 1.0) / 3.0, max(vals) * 300.0)
    handles, labels = ax.get_legend_handles_labels()
    handles.append(Line2D([], [], color="0.25", marker="o", ls="none", ms=6, mec="white", mew=0.7, label=bf("plan switch")))
    ax.legend(handles=handles, loc="upper right", ncol=1, framealpha=0.7, fontsize=10.5, labelspacing=0.2, handletextpad=0.4, borderpad=0.3, handlelength=1.5)
    ax.text(0.03, 0.03, f"{len(cells)} workload{'s' if len(cells) != 1 else ''}", transform=ax.transAxes, ha="left", va="bottom", fontsize=12, color="0.35")


def _shift_panel(ax, rec: dict):
    p = rec["params"]
    series = [("stale", "#CC79A7", "o", "-", "Stale plan"), ("instant", "0.45", "*", "--", "Instant switch"), ("delayed", ps.C["green"], "s", "-", "Delayed switch")]
    mr = rec["mr"].get("latency_ms") or []
    allv = []
    if mr:
        ax.plot([b for b, _ in mr], [ms / 1000.0 for _, ms in mr], color=ps.C["vermillion"], marker="v", ls=":", ms=6, lw=1.8, markevery=3, zorder=3)
        ax.annotate(bf("MR"), (mr[(2 * len(mr)) // 3][0], mr[(2 * len(mr)) // 3][1] / 1000.0), xytext=(6, -8), textcoords="offset points", ha="left", va="top", color=ps.C["vermillion"], fontsize=14)
        allv += [ms / 1000.0 for _, ms in mr]
        if rec["mr"].get("status") != "ok" and len(mr) < p["batches"]:
            ax.scatter([mr[-1][0] + 1], [mr[-1][1] / 1000.0 * 2], marker="x", color=ps.C["vermillion"], s=80, lw=2.2, zorder=5)
    for arm, color, marker, ls, label in series:
        lat = (rec["arms"].get(arm) or {}).get("latency_ms") or []
        if lat:
            ax.plot([b for b, _ in lat], [ms / 1000.0 for _, ms in lat], color=color, marker=marker, ls=ls, ms=(8 if marker == "*" else 5.5), lw=1.8, markevery=3, label=bf(label), zorder=4)
            allv += [ms / 1000.0 for _, ms in lat]
    ax.axvline(p["shift_after_batch"], color="0.25", lw=1.2, zorder=2)
    ax.text(p["shift_after_batch"] - 0.3, 0.97, "shift", transform=ax.get_xaxis_transform(), ha="right", va="top", fontsize=12, color="0.3")
    ax.set_yscale("log")
    if allv:
        ax.set_ylim(min(allv) / 60.0, max(allv) * 3.0)
    ax.legend(loc="lower right", fontsize=11, framealpha=0.85, labelspacing=0.25, borderpad=0.3, handlelength=1.8)


def budget_shift(root: Path, out: Path):
    recs = records(root)
    budget = [r for r in recs if r["params"]["part"] == "budget"]
    shift = [r for r in recs if r["params"]["part"] == "shift"]
    fig, (axa, axb) = ps.figure("text", nrows=1, ncols=2, h=3.5, sharey=False)
    _budget_panel(axa, budget)
    axa.set_xlabel(r"\textbf{Mem.\ Budget} $\boldsymbol{M / S_{\max}}$" if USETEX else "Mem. Budget $M / S_{max}$")
    axa.set_ylabel(bf("Speed-Up Ratio"))
    ps.grid_both(axa)
    if shift:
        _shift_panel(axb, shift[0])
    axb.set_xlabel(bf("Batch"))
    axb.set_ylabel(bf("Latency [s]"))
    ps.grid_both(axb)
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_fig11_budget_shift.{ext}", bbox_inches="tight", pad_inches=0.05, dpi=200)
    print(f"wrote {out / 'trino_fig11_budget_shift'}.{{pdf,png}}")


def _topology_figure(topo: list, out: Path):
    names = [t for t in ("chain", "star", "triangle", "cycle", "diamond", "complete") if any(r["params"]["topo"] == t for r in topo)]
    sigmas = sorted({r["params"]["sigma"] for r in topo})
    style = {"chain": (ps.C["blue"], "o", "-"), "star": (ps.C["vermillion"], "s", "--"), "triangle": (ps.C["green"], "^", "-."),
             "cycle": (ps.C["purple"], "D", ":"), "diamond": (ps.C["orange"], "v", (0, (3, 1, 1, 1))), "complete": (ps.C["sky"], "P", (0, (5, 2)))}
    fig, ax = plt.subplots(1, 1, figsize=(5.4, 3.5))
    lower = False
    for t in names:
        color, marker, ls = style[t]
        xs, ys, bound = [], [], []
        for s in sigmas:
            r = next((r for r in topo if r["params"]["topo"] == t and r["params"]["sigma"] == s), None)
            e, m = secs((r or {}).get("eimer")), (r or {}).get("mr") or {}
            dnf = m.get("status") != "ok"
            ms = m.get("total_ms") if not dnf else m.get("spent_ms")
            if isinstance(e, float) and ms:
                xs.append(s); ys.append(ms / 1000.0 / e); bound.append(dnf)
        ax.plot(xs, ys, color=color, marker=marker, ls=ls, ms=6.5, lw=1.8, zorder=3, label=bf(t))
        if any(bound):
            lower = True
            ax.scatter([x for x, d in zip(xs, bound) if d], [y for y, d in zip(ys, bound) if d], facecolors="white", edgecolors=color, marker=marker,
                       s=60, linewidths=1.6, zorder=5)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xticks(sigmas)
    ax.set_xticklabels([f"{s:g}" for s in sigmas], rotation=35, ha="right", rotation_mode="anchor")
    ax.get_xaxis().set_minor_formatter(plt.NullFormatter())
    ax.set_xlabel(r"\textbf{Dependent Sel.} $\boldsymbol{\sigma}$" if USETEX else r"Dependent Sel. $\sigma$")
    ax.set_ylabel(bf("Speed-Up Ratio"))
    ps.breakeven(ax); ps.grid_both(ax)
    lo, hi = ax.get_ylim()
    ax.set_ylim(min(lo, 0.7), hi * 1.3)
    leg = ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=3, framealpha=1.0, fontsize=13, columnspacing=0.8, handletextpad=0.35,
                    handlelength=1.6, labelspacing=0.25, borderpad=0.3)
    if lower:
        ax.text(0.97, 0.10, "hollow: lower bound", transform=ax.transAxes, ha="right", va="bottom", fontsize=11, color="0.35",
                bbox=dict(facecolor="white", edgecolor="none", alpha=0.8, pad=1.5))
    fig.tight_layout()
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_topology.{ext}", bbox_inches="tight", bbox_extra_artists=[leg], pad_inches=0.05, dpi=200)
    plt.close(fig)
    print(f"wrote {out / 'trino_topology'}.{{pdf,png}}")


def _memory_figure(mem: list, out: Path):
    ks = [r["params"]["k"] for r in mem]
    series = (("singletons", ps.C["orange"], "o", ":", "Singletons"), ("pick", ps.C["blue"], "D", "-", "EIMER"),
              ("full_pattern", ps.C["green"], "s", "--", "Full Pattern"), ("prefixes", ps.C["purple"], "^", "-.", "Prefixes"))
    in_rows = any((s.get("state") or {}).get("bytes") is None for r in mem for s in r["strategies"] if s.get("status") == "ok")
    state = lambda s: ((s.get("state") or {}).get("rows") if in_rows else ((s.get("state") or {}).get("bytes") or 0) / 1e6)
    peak = lambda s: (s.get("peak_query_memory_bytes") or 0) / 1e6
    fig, (axa, axb) = plt.subplots(1, 2, figsize=(11.0, 3.3))
    for ax, value, label in ((axa, state, "State [rows]" if in_rows else "State [MB]"), (axb, peak, "Peak Mem. [MB]")):
        for name, color, marker, lst, lab in series:
            xs, ys = [], []
            for x, r in enumerate(mem):
                s = next((s for s in r["strategies"] if s["strategy"] == name), None)
                if s and s.get("status") == "ok" and value(s):
                    xs.append(x); ys.append(value(s))
            ax.plot(xs, ys, color=color, marker=marker, ls=lst, ms=6.5, lw=1.8, zorder=3, label=bf(lab))
        ax.set_yscale("log")
        ax.set_xticks(range(len(ks)))
        ax.set_xticklabels([str(k) for k in ks])
        ax.set_xlim(-0.3, max(len(ks) - 0.7, 0.7))
        ax.set_ylabel(bf(label))
        ax.set_xlabel(r"\textbf{Pattern Length} $\boldsymbol{k}$" if USETEX else "Pattern Length $k$")
        ps.grid_both(ax)
    handles, labels = axa.get_legend_handles_labels()
    fig.tight_layout(w_pad=2.0)
    leg = fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 0.97), framealpha=1.0, fontsize=13, borderpad=0.3, handlelength=1.8, ncol=4,
                     columnspacing=0.9, handletextpad=0.4)
    for ext in ("pdf", "png"):
        fig.savefig(out / f"trino_memory.{ext}", bbox_inches="tight", bbox_extra_artists=[leg], pad_inches=0.05, dpi=200)
    plt.close(fig)
    print(f"wrote {out / 'trino_memory'}.{{pdf,png}}")


def topology_memory(root: Path, out: Path):
    recs = records(root)
    topo = [r for r in recs if r["params"]["part"] == "topology"]
    mem = sorted((r for r in recs if r["params"]["part"] == "memory"), key=lambda r: r["params"]["k"])
    out.mkdir(parents=True, exist_ok=True)
    if topo:
        _topology_figure(topo, out)
    if mem:
        _memory_figure(mem, out)


def realworld(root: Path, out: Path):
    from matplotlib.ticker import LogLocator
    from matplotlib.transforms import Bbox
    names = {"taxi": "NYC Taxi", "chicago": "Chicago Crimes", "ecommerce": "E-Commerce"}
    shade = [ps.C["orange"], ps.C["green"], ps.C["blue"]]
    c_sp, c_mem = ps.C["blue"], ps.C["orange"]
    mb = 1048576.0
    by = {(r["dataset"], r["query"]): r for r in records(root)}

    def state_mb(e: dict) -> float:
        st = e.get("state") or {}
        parts = [st] if "uncompressed_bytes" in st else list(st.values())
        return sum((v or {}).get("uncompressed_bytes") or 0 for v in parts) / mb

    laid, sections, x, gap = [], [], 0.0, 0.7
    for di, d in enumerate([n for n in names if any(k[0] == n for k in by)]):
        start = x
        for q in sorted(qq for (dd, qq) in by if dd == d):
            e, m = by[(d, q)].get("eimer") or {}, by[(d, q)].get("mr") or {}
            et = e["total_ms"] if e.get("status") == "ok" and e.get("total_ms") else None
            done = m.get("status") == "ok" and bool(m.get("total_ms"))
            mt = m["total_ms"] if done else m.get("spent_ms")
            laid.append(dict(x=x, q=q, ok=et is not None, sp=(mt / et if et and mt else None), bound=not done, state=state_mb(e),
                             peak=(e.get("peak_query_memory_bytes") or 0) / mb))
            x += 1.0
        sections.append((di, names[d], start, x - 1.0))
        x += gap
    sp_all = [c["sp"] for c in laid if c["sp"]] or [1.0]
    mem_all = [c["state"] + c["peak"] for c in laid if c["ok"] and c["state"] + c["peak"] > 0] or [1.0]
    w_in, left, right, bottom, h_mem, h_gap, h_sp, band, bar_w = 13.32, 1.5, 0.08, 0.5, 2.25, 0.16, 1.9, 0.42, 0.74
    h_in = bottom + h_mem + h_gap + h_sp + band
    sp_floor, mem_floor = min(0.3, min(sp_all) / 3.0), min(0.7, min(mem_all) / 3.0)
    with plt.rc_context({"font.size": 15, "axes.labelsize": 24, "xtick.labelsize": 22, "ytick.labelsize": 22}):
        fig = plt.figure(figsize=(w_in, h_in))
        fig.set_layout_engine("none")
        am = fig.add_axes([left / w_in, bottom / h_in, (w_in - left - right) / w_in, h_mem / h_in])
        asp = fig.add_axes([left / w_in, (bottom + h_mem + h_gap) / h_in, (w_in - left - right) / w_in, h_sp / h_in], sharex=am)
        asp.set_yscale("log"); asp.set_ylim(sp_floor, max(sp_all) * 1.6)
        am.set_yscale("log"); am.set_ylim(mem_floor, max(mem_all) * 1.6)
        for a in (asp, am):
            for di, _, a0, b0 in sections:
                a.axvspan(a0 - 0.55, b0 + 0.55, color=shade[di % 3], alpha=0.06, zorder=0)
                if di > 0:
                    a.axvline(a0 - 0.55 - (gap - 0.1) / 2, color="0.75", lw=1.0, zorder=1)
            a.grid(axis="y", ls=":", color="0.7", lw=0.7, zorder=0)
            a.grid(False, axis="x")
            a.set_axisbelow(True)
            a.yaxis.set_major_locator(LogLocator(base=10.0, numticks=15))
            a.yaxis.set_minor_locator(LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100))
            a.yaxis.set_minor_formatter(plt.NullFormatter())
            a.tick_params(axis="x", which="minor", bottom=False)
        x_hi = laid[-1]["x"] if laid else 0.0
        for di, title, a0, b0 in sections:
            fig.text((left + ((a0 + b0) / 2 + 1.0) / (x_hi + 2.0) * (w_in - left - right)) / w_in, (h_in - band + 0.07) / h_in, bf(title), ha="center", va="bottom", fontsize=19)
        asp.axhline(1.0, color="0.4", ls="--", lw=1.3, zorder=2)
        asp.set_xlim(-1.0, x_hi + 1.0)
        am.set_xticks([c["x"] for c in laid])
        am.set_xticklabels([(rf"$\boldsymbol{{Q_{{{c['q'][1:]}}}}}$" if USETEX else c["q"]) for c in laid])
        asp.tick_params(axis="x", labelbottom=False)
        asp.set_ylabel(bf("Speed-Up"), color=c_sp)
        am.set_ylabel(bf(r"Mem.\ [MB]") if USETEX else "Mem. [MB]", color=c_mem)
        asp.tick_params(axis="y", colors=c_sp); am.tick_params(axis="y", colors=c_mem)
        asp.spines["left"].set_color(c_sp); am.spines["left"].set_color(c_mem)
        fig.align_ylabels([asp, am])

        def num(v: float) -> str:
            return f"{v:.0f}" if v >= 100 else f"{v:.1f}"

        todo = []
        for c in laid:
            if not c["ok"] or c["sp"] is None:
                asp.text(c["x"], sp_floor * 1.5, bf("EIMER DNF" if not c["ok"] else "n/a"), ha="center", va="bottom", rotation=90, fontsize=12.5, color="0.45", zorder=5)
                continue
            asp.bar(c["x"], c["sp"], width=bar_w, color=c_sp, edgecolor="white", linewidth=0.5, hatch=("//" if c["bound"] else ""), zorder=3)
            n = f"{c['sp']:.0f}" if c["sp"] >= 10 else f"{c['sp']:.1f}"
            lab = (r"$\boldsymbol{" + (r"\geq\!\!" if c["bound"] else "") + n + r"\times}$") if USETEX else (("≥" if c["bound"] else "") + n + "×")
            todo.append((asp, sp_floor, c["sp"], asp.text(c["x"], c["sp"], lab, rotation=90, ha="center", va="bottom", fontsize=20, color=c_sp, zorder=5), "sp"))
            mem = c["state"] + c["peak"]
            am.bar(c["x"], mem, width=bar_w, color=c_mem, edgecolor="white", linewidth=0.5, zorder=3)
            lab = (r"$\boldsymbol{" + num(c["state"]) + r"{+}" + num(c["peak"]) + r"}$") if USETEX else f"{num(c['state'])}+{num(c['peak'])}"
            todo.append((am, mem_floor, mem, am.text(c["x"], mem, lab, rotation=90, ha="center", va="bottom", fontsize=18.5, color="#6B3A00", zorder=5), "mem"))
        import math
        fig.canvas.draw()
        rend = fig.canvas.get_renderer()
        for a, floor in ((asp, sp_floor), (am, mem_floor)):
            mine = [(top, t.get_window_extent(rend).height) for ax_, _, top, t, _ in todo if ax_ is a]
            axes_px = a.transAxes.transform((0, 1))[1] - a.transAxes.transform((0, 0))[1]
            hi = a.get_ylim()[1]
            for _ in range(20):
                need = hi
                for top, hpx in mine:
                    y1 = (math.log(top) - math.log(floor)) / (math.log(hi) - math.log(floor)) * axes_px
                    if y1 < hpx + 7 and y1 + hpx + 7 > axes_px:
                        need = max(need, math.exp(math.log(floor) + (math.log(top) - math.log(floor)) / (1.0 - (hpx + 7) / axes_px)) * 1.02)
                if need <= hi:
                    break
                hi = need
            a.set_ylim(floor, hi)
        fig.canvas.draw()
        for a, floor, top, t, kind in todo:
            hpx = t.get_window_extent(rend).height
            y0, y1 = a.transData.transform((0, floor))[1], a.transData.transform((0, top))[1]
            if y1 + 5 + hpx <= a.transAxes.transform((0, 1))[1] - 2:
                ybot = y1 + 5
            else:
                ybot = max(y0 + 2, y1 - 5 - hpx)
                if kind == "sp":
                    t.set_color("white"); t.set_bbox(dict(boxstyle="square,pad=0.08", fc=c_sp, ec="none"))
                else:
                    t.set_color("#2B1600")
            t.set_position((t.get_position()[0], a.transData.inverted().transform((0, ybot))[1]))
        out.mkdir(parents=True, exist_ok=True)
        for ext in ("pdf", "png"):
            fig.savefig(out / f"trino_fig12_realworld.{ext}", dpi=200, bbox_inches=Bbox([[0, 0], [w_in, h_in]]))
        plt.close(fig)
    print(f"wrote {out / 'trino_fig12_realworld'}.{{pdf,png}}")


if __name__ == "__main__":
    which, root, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    {"fig8": fig8, "fig9": fig9, "fig10": fig10, "kleene": kleene, "budget_shift": budget_shift, "topology_memory": topology_memory,
     "realworld": realworld}[which](root, out)
