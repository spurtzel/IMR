#!/usr/bin/env python3
"""The DuckDB and Esper figures of the paper: runtime lines and peak-memory bars per approach over the table size N and
the pattern length k, from the records of the DuckDB and the Esper experiment (../duckdb, ../esper).

The record directories and the figure variant are set through environment variables; the two commands that produce
the paper's figures are in reproducibility/docker/in_container/render_figures.sh. Writes
<PLOT_OUT_DIR>/duckdb_esper_paper*.{png,pdf}.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import sys
from pathlib import Path


STATE_ARMS: dict = {}
def legend_kw(handles) -> dict:
    out = []
    for h in handles:
        cs = STATE_ARMS.get(h.get_label())
        out.append((ps.HatchSwatch(cs[0], cs[1], **BAR_EDGE(cs[0])), h) if cs else h)
    return dict(handles=out, labels=[h.get_label() for h in handles], handler_map=ps.HATCH_HANDLER_MAP)

HERE = Path(__file__).resolve().parent
EXP = HERE.parent
sys.path.insert(0, str(HERE))
from matplotlib.patches import Patch
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import LogLocator, NullFormatter
import plot_style as ps


def _cells(exp: str):
    spec = importlib.util.spec_from_file_location(f"cells_{exp}", EXP / exp / "cells.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


DUCK = _cells("duckdb")
os.environ["ESPER_CENTER"] = "fig8"
ESP = _cells("esper")
ESP_CENTER, ESP_AXES = ESP.active_center("fig8s005" if "s005" in os.environ.get("ESPER_RESULTS", "") else "fig8")

DUCKDB_RESULTS = EXP / "duckdb" / os.environ.get("DUCKDB_RESULTS", "results")
ESPER_RESULTS = EXP / "esper" / os.environ.get("ESPER_RESULTS", "results")
ESPER_K_FROM = EXP / "esper" / os.environ["ESPER_K_FROM"] if os.environ.get("ESPER_K_FROM") else None
ESPER_BOUNDED = EXP / "esper" / os.environ["ESPER_BOUNDED"] if os.environ.get("ESPER_BOUNDED") else None
ESPER_UNBOUNDED = EXP / "esper" / os.environ["ESPER_UNBOUNDED"] if os.environ.get("ESPER_UNBOUNDED") else None
ESPER_STRATEGIES = bool(os.environ.get("ESPER_STRATEGIES"))
MEM = os.environ.get("MEM", "1") == "1"
FIGS = Path(os.environ["PLOT_OUT_DIR"]) if os.environ.get("PLOT_OUT_DIR") else HERE / "figs"
ESPER_GROUPED_LEGEND = os.environ.get("ESPER_GROUPED_LEGEND", "0") == "1"
PANELS = [a for a in os.environ.get("PANELS", "").split(",") if a]
NAME = "duckdb_esper_paper" + ("_bounded" if ESPER_BOUNDED else "") + ("_unb" if ESPER_UNBOUNDED else "") + ("_n200" if os.environ.get("ESPER_N_AXIS") == "N200" else "") + ("_n1000" if os.environ.get("DUCKDB_N_AXIS") == "N1000" else "") + ("_bars3" if os.environ.get("BAR_ARMS") else "") + ("_strategies" if ESPER_STRATEGIES else "") + ("_mem" if MEM else "") + ("_k005" if ESPER_K_FROM else "") + ("_s005" if ESPER_RESULTS.name.endswith("s005") else "") + ("_mb" if os.environ.get("STATE_UNIT") == "mb" else "")
NAME += ("_ytight" if os.environ.get("Y_TIGHT") == "1" else "") + ("_unsharey" if os.environ.get("UNSHARE_Y") == "1" else "") + os.environ.get("NAME_SUFFIX", "") + ("_grouped" if ESPER_GROUPED_LEGEND else "")

XLABEL = {"N": r"Table Size $N$", "W": r"Window $W$ [ms]", "B": r"Batches $B$", "k": r"Pattern Length $k$"}
STATE_UNIT = os.environ.get("STATE_UNIT", "rows")
BYTES_PER_VAR = 32.0
PEAKMEM_DIR = os.environ.get("PEAKMEM_DIR", "")
ESPER_PEAKMEM_DIR = os.environ.get("ESPER_PEAKMEM_DIR", ""); ESPER_UNB_PEAKMEM_DIR = os.environ.get("ESPER_UNBOUNDED_PEAKMEM_DIR", "")
ESPER_PEAK = {}
ESPER_JVMBASE_DIR = os.environ.get("ESPER_JVMBASE_DIR", ""); ESPER_BASE = {}
if ESPER_JVMBASE_DIR:
    for _q in (EXP / "esper" / ESPER_JVMBASE_DIR / "records").glob("*.json"):
        _r = json.loads(_q.read_text()); ESPER_BASE[_r["cell"]] = _r.get("peak_live_heap_mb") or 0.0
for _d in (ESPER_PEAKMEM_DIR, ESPER_UNB_PEAKMEM_DIR):
    if _d:
        for _q in (EXP / "esper" / _d / "records").glob("*.json"):
            _r = json.loads(_q.read_text())
            if _d == ESPER_UNB_PEAKMEM_DIR:
                ESPER_PEAK.setdefault(_r["cell"], {})["esper_native_unbounded"] = _r.get("esper_native") or {}
            else:
                ESPER_PEAK.setdefault(_r["cell"], {}).update({k: v for k, v in _r.items() if k.startswith("esper_native") or k.startswith("eimer_esper")})
PEAK = {}
NAME += "_peak" if (PEAKMEM_DIR or ESPER_PEAKMEM_DIR) else ""
if PEAKMEM_DIR:
    for _q in (EXP / "duckdb" / PEAKMEM_DIR / "records").glob("*.json"):
        _r = json.loads(_q.read_text()); PEAK[_r["cell"]] = _r


def _view_vars(name: str) -> int:
    parts = name.split("_")
    return max(1, len([x for x in parts[1:] if len(x) == 1 and x.isalpha()]))


def _size(rows_by_view: dict) -> float:
    if STATE_UNIT == "mb":
        return sum(rows * BYTES_PER_VAR * _view_vars(v) for v, rows in rows_by_view.items()) / 1048576.0
    return float(sum(rows_by_view.values()))


def state_rows(r: dict, arm: str):
    d = r.get(arm) or {}
    if PEAK and arm in ("transpiled", "eimer") and r.get("cell") in PEAK:
        pk = PEAK[r["cell"]].get(arm) or {}
        return pk.get("transient_mb") if pk.get("status") == "ok" else None
    if (ESPER_PEAK or ESPER_PEAK_WBK) and (arm.startswith("esper_native") or arm.startswith("eimer_esper")):
        peak, base = (ESPER_PEAK_WBK, ESPER_BASE_WBK) if r.get("_wbk") else (ESPER_PEAK, ESPER_BASE)
        pk = (peak.get(r.get("cell")) or {}).get(arm) or {}
        live = max(pk.get("peak_live_heap_mb") or 0.0, pk.get("final_live_heap_mb") or 0.0)
        if not (pk.get("status") == "ok" and live):
            return None
        return max(live - base.get(r.get("cell"), 0.0), 1.0) if base else live
    if d.get("status") != "ok":
        return None
    if "max_resident_rows" in d:
        return _size(d.get("per_cache_rows") or {}) or (None if STATE_UNIT == "mb" else (d["max_resident_rows"] or None))
    if d.get("per_batch") and any("resident_rows" in b for b in d["per_batch"]):
        return max(_size(b.get("resident_rows") or {}) for b in d["per_batch"]) or None
    return None


def cell_of(cell_name, center: dict, param: str, v):
    return cell_name({**center, **v}) if isinstance(v, dict) else cell_name({**center, param: v})


def xval(param: str, v):
    return v[param] if isinstance(v, dict) else v


def present_axes(results: Path, center: dict, axes: dict, cell_name) -> dict:
    have = {q.stem for q in (results / "records").glob("*.json")}
    return {ax: (p, vs) for ax, (p, vs) in axes.items() if sum(cell_of(cell_name, center, p, v) in have for v in vs) >= 2}


DUCK_AXES = present_axes(DUCKDB_RESULTS, DUCK.CENTER, DUCK.AXES, DUCK.cell_name)
if os.environ.get("DUCKDB_N_AXIS") == "N1000" and "N1000" in DUCK_AXES:
    DUCK_AXES = {"N": DUCK_AXES["N1000"], **{ax: v for ax, v in DUCK_AXES.items() if ax not in ("N", "N1000")}}
else:
    DUCK_AXES = {ax: v for ax, v in DUCK_AXES.items() if ax != "N1000"}
ESP_AXES = present_axes(ESPER_RESULTS, ESP_CENTER, ESP_AXES, ESP.cell_name)
if os.environ.get("ESPER_N_AXIS") == "N200" and "N200" in ESP_AXES:
    ESP_AXES = {"N": ESP_AXES["N200"], **{ax: v for ax, v in ESP_AXES.items() if ax not in ("N", "N200")}}
else:
    ESP_AXES = {ax: v for ax, v in ESP_AXES.items() if ax != "N200"}
ESPER_WBK_FROM = EXP / "esper" / os.environ["ESPER_WBK_FROM"] if os.environ.get("ESPER_WBK_FROM") else None
ESPER_WBK_CENTER = None
if ESPER_WBK_FROM:
    ESPER_WBK_CENTER, _wbk_axes = ESP.active_center(os.environ["ESPER_WBK_CENTER"])
    _have_w = {q.stem for q in (ESPER_WBK_FROM / "records").glob("*.json")}
    ESP_AXES = {"N": ESP_AXES["N"], **{ax: _wbk_axes[ax] for ax in ("W", "B", "k")
                                       if sum(cell_of(ESP.cell_name, ESPER_WBK_CENTER, _wbk_axes[ax][0], v) in _have_w for v in _wbk_axes[ax][1]) >= 2}}
    NAME += "_wbk" + os.environ["ESPER_WBK_CENTER"]
ESPER_PEAK_WBK, ESPER_BASE_WBK = {}, {}
for _d, _tgt in ((os.environ.get("ESPER_WBK_PEAKMEM_DIR", ""), "peak"), (os.environ.get("ESPER_WBK_UNBOUNDED_PEAKMEM_DIR", ""), "unb"),
                 (os.environ.get("ESPER_WBK_JVMBASE_DIR", ""), "base")):
    for _q in ((EXP / "esper" / _d / "records").glob("*.json") if _d else []):
        _r = json.loads(_q.read_text())
        if _tgt == "unb":
            ESPER_PEAK_WBK.setdefault(_r["cell"], {})["esper_native_unbounded"] = _r.get("esper_native") or {}
        elif _tgt == "peak":
            ESPER_PEAK_WBK.setdefault(_r["cell"], {}).update({k: v for k, v in _r.items() if k.startswith("esper_native") or k.startswith("eimer_esper")})
        else:
            ESPER_BASE_WBK[_r["cell"]] = _r.get("peak_live_heap_mb") or 0.0
DUCKDB_MR_COLOR = ps.C["yellow"] if os.environ.get("DUCKDB_MR_COLOR") == "yellow" else ps.C["black"]
DUCK_SPLIT = os.environ.get("DUCK_SPLIT", "")
STRIP_STYLE = os.environ.get("STRIP_STYLE", "solid")
DUCK_MR_LABEL = os.environ.get("DUCK_MR_LABEL", r"DuckDB MR"); DUCK_EIMER_LABEL = os.environ.get("DUCK_EIMER_LABEL", r"EIMER")
if DUCK_SPLIT:
    NAME += "_split" + DUCK_SPLIT
GROUP_EDGE_BASE = os.environ.get("GROUP_EDGE_BASE", ps.C["green"]); GROUP_FACE_BASE = os.environ.get("GROUP_FACE_BASE", "#E2F4EB")
GROUP_EDGE_EIMER = os.environ.get("GROUP_EDGE_EIMER", ps.C["blue"]); GROUP_FACE_EIMER = os.environ.get("GROUP_FACE_EIMER", "#E3F0FA")
GROUP_LW = float(os.environ.get("GROUP_LW", "1.8"))
GROUP_TITLES = (os.environ.get("GROUP_TITLE_BASE", r"\textbf{Baselines:}"), os.environ.get("GROUP_TITLE_EIMER", r"\textbf{EIMER strategy space:}"))
ESPER_UNB_COLOR = os.environ.get("ESPER_UNB_COLOR", "#66CC00")
EDGE = lambda c: dict(mec="black", mew=1.3) if c == ps.C["yellow"] else {}
BAR_EDGE = lambda c: dict(edgecolor="black", hatch_color="black", fill_alpha=0.45) if c == ps.C["yellow"] else {}
ROWS = [
    dict(results=DUCKDB_RESULTS, center=DUCK.CENTER, axes=DUCK_AXES, cell_name=DUCK.cell_name, log_axes=("N", "B"),
         style={"transpiled": (DUCKDB_MR_COLOR, "X", DUCK_MR_LABEL, "--"), "eimer": (ps.C["blue"], "D", DUCK_EIMER_LABEL, "-")},
         legend=(1, "upper left")),
    dict(results=ESPER_RESULTS, center=ESP_CENTER, axes=ESP_AXES, cell_name=ESP.cell_name, log_axes=("N", "B"),
         style={"esper_native": (ps.C["green"], "P", os.environ.get("ESPER_NATIVE_LABEL", r"Esper MR (bounded)") if ESPER_STRATEGIES else r"Esper MR", "--"),
                **({"esper_native_unbounded": (ESPER_UNB_COLOR, "p", r"Esper MR (unbounded)", "-.")} if ESPER_UNBOUNDED else {}),
                "eimer_esper": (ps.C["blue"], "D", r"Clique-Grow" if ESPER_GROUPED_LEGEND else r"EIMER", "-"),
                **({"esper_native_bounded": (ps.C["green"], "p", r"Esper MR (bounded)", "-.")} if ESPER_BOUNDED else {}),
                **({"eimer_esper_edge": (ps.C["sky"], "o", r"Edge Cover", "-"),
                    "eimer_esper_prefix": (ps.C["orange"], "v", os.environ.get("ESPER_PREFIX_LABEL", r"CEP Prefixes"), "-"),
                    "eimer_esper_singletons": ("0.45", "^", r"Singletons", "-")} if ESPER_STRATEGIES else {})},
         legend=(1 if "W" in ESP_AXES else 0, "upper left"), thin=1.9 if len(ESP_AXES) == 2 else 2.5,
         w_in_s=os.environ.get("ESPER_W_UNIT", "ms" if ESPER_WBK_FROM else "s") == "s",
         k_from=ESPER_K_FROM, wbk_from=ESPER_WBK_FROM, wbk_center=ESPER_WBK_CENTER),
]
ROW_ONLY = os.environ.get("ROW_ONLY", "")
LEGEND_STRIP = os.environ.get("LEGEND_STRIP", "0") == "1"; STRIP_TOP = float(os.environ.get("STRIP_TOP", "1.6")); STRIP_FONTSIZE = float(os.environ.get("STRIP_FONTSIZE", "13"))
Y_TIGHT = os.environ.get("Y_TIGHT") == "1"; UNSHARE_Y = os.environ.get("UNSHARE_Y") == "1"
INLINE_HEADROOM = float(os.environ.get("INLINE_HEADROOM", "150")); INLINE_FONTSIZE = float(os.environ.get("INLINE_FONTSIZE", "13")); INLINE_NCOL = int(os.environ.get("INLINE_NCOL", "1"))
ROW4_FONTS = {**ps.PAPER_ROW_FONTS, "font.size": 18, "axes.labelsize": 26, "xtick.labelsize": 22,
              "ytick.labelsize": 22, "legend.fontsize": 21}


def load(d: Path) -> dict:
    p = d / "records"
    return {q.stem: json.loads(q.read_text()) for q in p.glob("*.json")} if p.exists() else {}


def ktick(x: int) -> str:
    if x >= 1_000_000 and x % 1_000_000 == 0:
        return r"$%d\mathrm{M}$" % (x // 1_000_000)
    return (r"$%d\mathrm{k}$" % (x // 1000)) if x >= 1000 and x % 1000 == 0 else str(x)


def thin(xs: list, factor: float = 2.5) -> list:
    out = []
    for x in xs:
        if not out or x / out[-1] >= factor:
            out.append(x)
    return out


def draw_row(axes, row: dict) -> None:
    recs = load(row["results"]); style, center, cell_name = row["style"], row["center"], row["cell_name"]
    if "esper_native_unbounded" in style:
        for name, ru in load(ESPER_UNBOUNDED).items():
            if name in recs and ru.get("esper_native"):
                recs[name]["esper_native_unbounded"] = ru["esper_native"]
    if "esper_native_bounded" in style:
        for name, rb in load(ESPER_BOUNDED).items():
            if name in recs and rb.get("esper_native"):
                recs[name]["esper_native_bounded"] = rb["esper_native"]
    if not recs:
        raise SystemExit(f"no records under {row['results']}")
    recs_k = load(row["k_from"]) if row.get("k_from") else None
    src = lambda axis: recs_k if (recs_k is not None and axis == "k") else recs
    all_recs = list(recs.values()) + (list(recs_k.values()) if recs_k else [])
    center_of = lambda axis: center
    if row.get("wbk_from"):
        recs_w = load(row["wbk_from"])
        for r in recs_w.values():
            r["_wbk"] = True
        if "esper_native_unbounded" in style and os.environ.get("ESPER_WBK_UNBOUNDED"):
            for name, ru in load(EXP / "esper" / os.environ["ESPER_WBK_UNBOUNDED"]).items():
                if name in recs_w and ru.get("esper_native"):
                    recs_w[name]["esper_native_unbounded"] = ru["esper_native"]
        src = lambda axis: recs_w if axis in ("W", "B", "k") else recs
        center_of = lambda axis: row["wbk_center"] if axis in ("W", "B", "k") else center
        n_param, n_values = row["axes"]["N"]
        all_recs = [recs[c] for c in (cell_of(cell_name, center, n_param, v) for v in n_values) if c in recs] + list(recs_w.values())
    feasible = [r[a]["total_ms"] / 1000.0 for r in all_recs for a in style if r.get(a, {}).get("status") == "ok"]
    any_dnf = any(r.get(a, {}).get("status") == "dnf" for r in all_recs for a in style)
    ceiling = max(feasible) * 5
    top = ceiling * 2 if any_dnf else max(feasible) * 4
    if ROW_ONLY and len(style) > 2:
        top = max(top, max(feasible) * INLINE_HEADROOM)
        if LEGEND_STRIP and not any_dnf:
            top = max(feasible) * STRIP_TOP
        elif LEGEND_STRIP and Y_TIGHT:
            top = ceiling * float(os.environ.get("DNF_TOP", "1.8"))
    bottom = min(feasible) / 1.6
    if os.environ.get("ROW_LEGEND_FRACTION"):
        _f = float(os.environ["ROW_LEGEND_FRACTION"]); top = top * (top / bottom) ** (_f / (1.0 - _f))
    rows_all = [m for r in all_recs for a in style for m in [state_rows(r, a)] if m]
    mem_range = (min(rows_all) / 1.6, max(rows_all) * float(os.environ.get("MEM_TOP", "4"))) if rows_all else (1, 10)
    if os.environ.get("ROW_LEGEND_FRACTION") and rows_all:
        _f = float(os.environ["ROW_LEGEND_FRACTION"]); mem_range = (mem_range[0], mem_range[1] * (mem_range[1] / mem_range[0]) ** (_f / (1.0 - _f)))
    if os.environ.get("SHARED_PRINT") == "1":
        print(f"ranges {ROW_ONLY}: runtime {bottom:.3g}..{top:.3g} ceiling {ceiling:.3g} memory {mem_range[0]:.3g}..{mem_range[1]:.3g} (bars {min(rows_all):.3g}..{max(rows_all):.3g})")
    if os.environ.get("SHARED_YLIM"): bottom, top = (float(x) for x in os.environ["SHARED_YLIM"].split(","))
    if os.environ.get("SHARED_CEILING"): ceiling = float(os.environ["SHARED_CEILING"])
    if os.environ.get("SHARED_MEM"): mem_range = tuple(float(x) for x in os.environ["SHARED_MEM"].split(","))
    for i, (ax, (axis, (param, values))) in enumerate(zip(axes, row["axes"].items())):
        rk = src(axis)
        ctr = center_of(axis)
        vs_present = [v for v in values if cell_of(cell_name, ctr, param, v) in rk]
        rec_of = {xval(param, v): rk[cell_of(cell_name, ctr, param, v)] for v in vs_present}
        xs_present = [xval(param, v) for v in vs_present]
        if MEM and xs_present:
            ax2 = ax.twinx(); mem_lo, mem_hi = mem_range; is_log = axis in row["log_axes"]
            gap = min((math.log10(b) - math.log10(a_)) if is_log else (b - a_) for a_, b in zip(xs_present, xs_present[1:])) if len(xs_present) > 1 else 1.0
            bar_arms = [a for a in style if any(state_rows(r, a) for r in all_recs)]
            if os.environ.get("BAR_ARMS"):
                keep = set(os.environ["BAR_ARMS"].split(",")); bar_arms = [a for a in bar_arms if a in keep]
            group = float(os.environ.get("BAR_GROUP", "0.55")) * gap; wbar = group / max(len(bar_arms), 1)
            for j, a in enumerate(bar_arms):
                color = style[a][0]; off = (j - (len(bar_arms) - 1) / 2) * wbar
                for v in xs_present:
                    m = state_rows(rec_of[v], a)
                    if not m:
                        continue
                    fill = float(os.environ.get("BAR_FILL", "1.0"))
                    if is_log:
                        c = v * 10 ** off; w = c * (10 ** (wbar * fill / 2) - 10 ** (-wbar * fill / 2))
                    else:
                        c = v + off; w = wbar * fill
                    ps.hatched_bar(ax2, c, m - mem_lo, w, mem_lo, color, a, zorder=1, **BAR_EDGE(color))
            ax2.set_yscale("log"); ax2.set_ylim(mem_lo, mem_hi)
            ax2.yaxis.set_major_locator(LogLocator(base=10.0, numticks=15)); ax2.yaxis.set_minor_formatter(NullFormatter())
            if os.environ.get("MEM_MINOR") == "1":
                ax2.yaxis.set_minor_locator(LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100))
            ax2.tick_params(axis="y", colors="0.35", labelsize=float(os.environ.get("MEM_TICK_SIZE", ROW4_FONTS["ytick.labelsize"] - 3)))
            if i == len(axes) - 1 and os.environ.get("HIDE_RIGHT") != "1":
                ax2.set_ylabel((r"Memory [MB]" if (PEAK or ESPER_PEAK) else r"State [MB]") if (STATE_UNIT == "mb" or PEAK or ESPER_PEAK) else r"State [rows]", color="0.35",
                               y=float(os.environ.get("MEM_LABEL_Y", "0.42")))
            else:
                ax2.tick_params(axis="y", labelright=False)
            ax.set_zorder(ax2.get_zorder() + 1); ax.patch.set_visible(False)
        for arm, (color, marker, label, ls) in style.items():
            fx, fy, cx = [], [], []
            for v in xs_present:
                a = rec_of[v].get(arm, {})
                if a.get("status") == "ok" and a.get("total_ms"):
                    fx.append(v); fy.append(a["total_ms"] / 1000.0)
                elif a.get("status") == "dnf":
                    cx.append(v)
            if fx:
                heavy = arm in ("eimer", "transpiled", "eimer_esper", "esper_native")
                lms, llw = float(os.environ.get("LIGHT_MS", "7")), float(os.environ.get("LIGHT_LW", "1.6"))
                ax.plot(fx, fy, color=color, marker=marker, ls=ls, lw=2.2 if heavy else llw, ms=9 if marker in "XPp" else (8 if heavy else lms),
                        label=label, zorder=4 if heavy else 3, alpha=1.0 if heavy else float(os.environ.get("LIGHT_ALPHA", "1.0")), **EDGE(color))
            if cx:
                if fx:
                    ax.plot([fx[-1], cx[0]], [fy[-1], ceiling], color=color, ls=":", lw=1.2, alpha=0.7, zorder=2)
                ax.scatter(cx, [ceiling] * len(cx), facecolors="white", edgecolors=color, marker=marker, s=90, linewidths=1.8, zorder=5)
            if arm == "eimer" and DUCK_SPLIT in ("lines", "band", "pct", "pctonly", "strip") and fx:
                mx = [sum(b[1] for b in rec_of[v]["eimer"]["per_batch_ms"]) / 1000.0 for v in fx]
                qx = [sum(b[2] for b in rec_of[v]["eimer"]["per_batch_ms"]) / 1000.0 for v in fx]
                ax._eimer_split = (fx, mx, qx, color)
                if DUCK_SPLIT == "strip":
                    continue
                if DUCK_SPLIT in ("pct", "pctonly"):
                    for x_, y_, m_, q_ in zip(fx, fy, mx, qx):
                        ax.annotate(r"%d\%%" % round(100 * q_ / (m_ + q_)), (x_, y_), xytext=(0, float(os.environ.get("PCT_DY", "11"))),
                                    textcoords="offset points", ha="center", va="bottom", color=color, zorder=6,
                                    fontsize=float(os.environ.get("PCT_FONTSIZE", "11")),
                                    bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.85))
                if DUCK_SPLIT == "pctonly":
                    continue
                if DUCK_SPLIT == "lines":
                    ax.plot(fx, mx, color=color, marker="s", ls="--", lw=1.4, ms=5, mfc="white", zorder=3, label=r"EIMER maintenance")
                    ax.plot(fx, qx, color=color, marker="o", ls=":", lw=1.4, ms=5, mfc="white", zorder=3, label=r"EIMER query")
                else:
                    ax.plot(fx, mx, color=color, ls="--", lw=1.4, zorder=3, label=r"EIMER maintenance")
                    ax.fill_between(fx, mx, fy, color=color, alpha=0.18, lw=0, zorder=2, label=r"EIMER query")
        if axis in row["log_axes"]:
            ax.set_xscale("log")
        ax.set_yscale("log"); ax.set_ylim(bottom=bottom, top=top)
        ax.yaxis.set_major_locator(LogLocator(base=10.0, numticks=15))
        ax.yaxis.set_minor_locator(LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_xticks(xs_present)
        lab = thin(xs_present, row.get("thin", 2.5)) if axis in row["log_axes"] else xs_present
        if axis in row["log_axes"] and len(xs_present) >= 7:
            lab = xs_present[len(xs_present) % 2 - 0 + 1::2] if len(xs_present) % 2 == 0 else xs_present[::2]
        w_s = axis == "W" and row.get("w_in_s")
        fmt = (lambda x: f"{x / 1000:g}") if w_s else (ktick if axis in ("N", "B") else str)
        ax.set_xticklabels([fmt(x) if x in lab else "" for x in xs_present])
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel(r"Window $W$ [s]" if w_s else XLABEL[axis])
        if i == 0 and os.environ.get("HIDE_LEFT") != "1":
            ax.set_ylabel(r"Runtime [s]")
        ps.rotate_xticks(ax); ps.grid_both(ax)
    handles = [ps.proxy(color=c, marker=m, ls=ls, label=l, ms=9 if m in "XPp" else 8, lw=2.0, **EDGE(c)) for c, m, l, ls in style.values()]
    if "eimer" in style and DUCK_SPLIT == "lines":
        handles += [ps.proxy(color=style["eimer"][0], marker="s", ls="--", label=r"EIMER maintenance", ms=5, lw=1.4, mfc="white"),
                    ps.proxy(color=style["eimer"][0], marker="o", ls=":", label=r"EIMER query", ms=5, lw=1.4, mfc="white")]
    elif "eimer" in style and DUCK_SPLIT in ("band", "pct"):
        handles += [ps.proxy(color=style["eimer"][0], marker="", ls="--", label=r"EIMER maintenance", lw=1.4),
                    Patch(facecolor=style["eimer"][0], alpha=0.18, label=r"EIMER query")]
    elif "eimer" in style and DUCK_SPLIT == "pctonly":
        h = ps.proxy(color=style["eimer"][0], marker=r"$\%$", ls="", label=os.environ.get("PCT_LABEL", r"EIMER query share"), ms=11, lw=0)
        handles.append(h)
    elif "eimer" in style and DUCK_SPLIT == "strip" and STRIP_STYLE == "hatch":
        pass
    elif "eimer" in style and DUCK_SPLIT == "strip":
        handles += [Patch(facecolor=style["eimer"][0], edgecolor=style["eimer"][0], label=r"EIMER maintenance"),
                    Patch(facecolor=style["eimer"][0], alpha=0.25, edgecolor=style["eimer"][0], label=r"EIMER query")]
    STATE_ARMS.update({style[a][2]: (style[a][0], a) for a in style if any(state_rows(r, a) for r in all_recs)
                       and (not os.environ.get("BAR_ARMS") or a in os.environ["BAR_ARMS"].split(","))})
    if (len(handles) <= 2 or (DUCK_SPLIT and "eimer" in style)) and not (LEGEND_STRIP and DUCK_SPLIT):
        li, loc = row["legend"]
        _e = lambda k, d: float(os.environ.get(k, d))
        axes[li].legend(**legend_kw(handles), loc=loc, handlelength=_e("SPLIT_LEG_HL", "1.6"), labelspacing=0.3, handletextpad=_e("SPLIT_LEG_TP", "0.4"),
                        borderpad=_e("SPLIT_LEG_BP", "0.32"), borderaxespad=_e("SPLIT_LEG_AP", "0.3"), framealpha=0.92,
                        **(dict(fontsize=float(os.environ.get("SPLIT_LEGEND_FONTSIZE", "14"))) if (len(handles) > 2 or DUCK_SPLIT) else {}))
        return None
    if ROW_ONLY and not LEGEND_STRIP:
        axes[0].legend(**legend_kw(handles), loc="upper left", ncol=INLINE_NCOL, fontsize=INLINE_FONTSIZE, handlelength=1.4, labelspacing=0.25,
                       handletextpad=0.35, columnspacing=0.7, borderpad=0.3, borderaxespad=0.25, framealpha=0.92)
        return None
    return handles


def strip_groups(fig, handles, y) -> None:
    from matplotlib.offsetbox import HPacker
    base = [h for h in handles if h.get_label().startswith("Esper MR")]
    rest = [h for h in handles if not h.get_label().startswith("Esper MR")]
    gap = float(os.environ.get("GROUP_GAP", "0.012"))
    legs = []
    for gi, (title, hs) in enumerate(zip(GROUP_TITLES, (base, rest))):
        ncs = [int(x) for x in os.environ.get("GROUP_NCOL", "").split(",") if x]
        nc = (ncs[gi] if gi < len(ncs) else ncs[-1]) if ncs else len(hs)
        leg = fig.legend(**legend_kw(hs), title=title, loc="lower left", bbox_to_anchor=(0.0, y), ncol=min(nc, len(hs)), columnspacing=1.0,
                         **(dict(borderaxespad=0.0) if os.environ.get("STRIP_GAP_IN") else {}),
                         handlelength=float(os.environ.get("GROUP_STRIP_HL", "1.6")), handletextpad=0.35, borderpad=0.3, framealpha=0.92, fontsize=STRIP_FONTSIZE - 1,
                         title_fontsize=STRIP_FONTSIZE - 1)
        tb, hb = leg._legend_box.get_children()
        leg._legend_box = HPacker(pad=float(os.environ.get("GROUP_STRIP_PAD", "0")), sep=6, align="center", children=[tb, hb])
        leg._legend_box.set_offset(leg._findoffset)
        legs.append(leg)
    for leg, (edge, face) in zip(legs, ((GROUP_EDGE_BASE, GROUP_FACE_BASE), (GROUP_EDGE_EIMER, GROUP_FACE_EIMER))):
        fr = leg.get_frame(); fr.set_edgecolor(edge); fr.set_facecolor(face); fr.set_linewidth(GROUP_LW); fr.set_alpha(1.0)
    fig.canvas.draw(); r = fig.canvas.get_renderer()
    from matplotlib.offsetbox import DrawingArea
    inner = [l._legend_box.get_window_extent(r).height for l in legs]
    for leg, h in zip(legs, inner):
        if max(inner) - h > 0.25:
            leg._legend_box.get_children().append(DrawingArea(0, max(inner) * 72.0 / fig.dpi - 2 * float(os.environ.get("GROUP_STRIP_PAD", "0")), 0, 0))
    fig.canvas.draw(); r = fig.canvas.get_renderer()
    widths = [l.get_window_extent(r).width / fig.bbox.width for l in legs]
    x = 0.5 - (sum(widths) + gap) / 2
    if os.environ.get("GROUP_STRIP_ALIGN") == "left":
        x = fig.axes[0].get_position().x0
    for leg, w in zip(legs, widths):
        leg.set_bbox_to_anchor((x, y), transform=fig.transFigure); x += w + gap


def inside_groups(ax, handles) -> None:
    from matplotlib.legend import Legend
    from matplotlib.offsetbox import HPacker
    base = [h for h in handles if h.get_label().startswith("Esper MR")]
    rest = [h for h in handles if not h.get_label().startswith("Esper MR")]
    fs = float(os.environ.get("GROUP_IN_SIZE", str(STRIP_FONTSIZE - 1)))
    loc = os.environ.get("GROUP_IN_LOC", "upper left"); x0 = 0.0 if "left" in loc else 1.0
    legs = []
    for gi, (title, hs) in enumerate(zip(GROUP_TITLES, (base, rest))):
        kw = legend_kw(hs)
        leg = Legend(ax, kw["handles"], kw["labels"], handler_map=kw["handler_map"], title=title, loc=loc, bbox_to_anchor=(x0, 1.0),
                     ncol=1 if gi == 0 else int(os.environ.get("GROUP_IN_NCOL", "2")), columnspacing=0.8, handlelength=1.6, handletextpad=0.35,
                     borderpad=0.3, borderaxespad=float(os.environ.get("GROUP_IN_PAD", "0.3")), framealpha=1.0, fontsize=fs, title_fontsize=fs,
                     alignment="left")
        if gi == 0:
            tb, hb = leg._legend_box.get_children()
            leg._legend_box = HPacker(pad=0.3 * fs * ax.figure.dpi / 72, sep=6, align="center", children=[tb, hb]); leg._legend_box.set_figure(ax.figure)
            leg._legend_box.set_offset(leg._findoffset)
        edge, face = ((GROUP_EDGE_BASE, GROUP_FACE_BASE), (GROUP_EDGE_EIMER, GROUP_FACE_EIMER))[gi]
        fr = leg.get_frame(); fr.set_edgecolor(edge); fr.set_facecolor(face); fr.set_linewidth(GROUP_LW); fr.set_alpha(1.0)
        leg.set_zorder(10); ax.add_artist(leg); legs.append(leg)
    ax.figure.canvas.draw(); r = ax.figure.canvas.get_renderer()
    bb = legs[0].get_window_extent(r).transformed(ax.transAxes.inverted())
    legs[1].set_bbox_to_anchor((x0, bb.y0 - float(os.environ.get("GROUP_IN_GAP", "0.02"))), transform=ax.transAxes)


def draw_split_strip(axs, saxs) -> None:
    for ax, sx in zip(axs, saxs):
        fx, mx, qx, color = getattr(ax, "_eimer_split", ([], [], [], ps.C["blue"]))
        is_log = ax.get_xscale() == "log"
        xt = list(ax.get_xticks()); xl = [t.get_text() for t in ax.get_xticklabels()]; lab = ax.get_xlabel()
        if len(fx) > 1:
            gap = min((math.log10(b) - math.log10(a)) if is_log else (b - a) for a, b in zip(fx, fx[1:]))
        else:
            gap = 1.0
        hw = float(os.environ.get("STRIP_BAR", "0.6")) * gap / 2
        for x_, m_, q_ in zip(fx, mx, qx):
            ms, qs = 100 * m_ / (m_ + q_), 100 * q_ / (m_ + q_)
            left, w = (x_ * 10 ** -hw, x_ * (10 ** hw - 10 ** -hw)) if is_log else (x_ - hw, 2 * hw)
            if STRIP_STYLE == "hatch":
                cx_ = left + w / 2
                ps.hatched_bar(sx, cx_, ms, w, 0, color, "eimer", zorder=2, fill_alpha=0.5)
                sx.bar(cx_, qs, w, bottom=ms, facecolor="white", edgecolor=color, lw=0.8, zorder=2)
                ps.hatched_bar(sx, cx_, qs, w, ms, color, "eimer", zorder=3, fill_alpha=0.0)
            else:
                sx.bar(left, ms, w, bottom=0, align="edge", color=color, edgecolor=color, lw=0.8, zorder=3)
                sx.bar(left, qs, w, bottom=ms, align="edge", color=color, alpha=0.25, edgecolor=color, lw=0.8, zorder=3)
            if os.environ.get("STRIP_TEXT", "1") == "1":
                sx.text(x_, 100 - 4, r"%d" % round(qs), ha="center", va="top", fontsize=float(os.environ.get("STRIP_TEXT_SIZE", "11")),
                        color="black", zorder=4)
        if is_log:
            sx.set_xscale("log")
        sx.set_xlim(ax.get_xlim()); sx.set_ylim(0, 100); sx.set_yticks([0, 100]); sx.set_yticklabels([r"0", r"100"])
        sx.set_xticks(xt); sx.set_xticklabels(xl); sx.xaxis.set_minor_formatter(NullFormatter()); sx.xaxis.set_minor_locator(plt.NullLocator())
        sx.set_xlabel(lab); ax.set_xlabel(""); ax.tick_params(axis="x", labelbottom=False)
        sx.tick_params(axis="y", labelsize=ROW4_FONTS["ytick.labelsize"] - 4)
        ps.rotate_xticks(sx); sx.grid(axis="y", lw=0.5, alpha=0.5, zorder=0)
        if STRIP_STYLE == "hatch":
            sx.set_facecolor(os.environ.get("STRIP_FACE", "0.93"))
            sx.legend(handles=[Patch(facecolor=(0.35, 0.35, 0.35, 0.5), edgecolor="0.35", lw=0.8), Patch(facecolor="white", edgecolor="0.35", lw=0.8)],
                      labels=[r"Maintenance /", r"query split"], loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=2, frameon=False,
                      fontsize=float(os.environ.get("STRIP_TITLE_SIZE", "13")), handlelength=1.2, handleheight=0.8, handletextpad=0.3,
                      columnspacing=0.5, borderpad=0.0, borderaxespad=0.15)
        if sx is not saxs[0]:
            sx.tick_params(labelleft=False)
    saxs[0].set_ylabel(os.environ.get("STRIP_YLABEL", r"[\%]"), fontsize=ROW4_FONTS["axes.labelsize"] - 8, labelpad=2)


def main_row_only() -> None:
    row = ROWS[0] if ROW_ONLY == "duckdb" else ROWS[1]
    if PANELS:
        missing = [a for a in PANELS if a not in row["axes"]]
        assert not missing, f"PANELS {missing} not among the row's axes {list(row['axes'])}"
        row = dict(row, axes={a: row["axes"][a] for a in PANELS}, legend=(len(PANELS) - 1, row["legend"][1]))
    with plt.rc_context(ROW4_FONTS):
        p = len(row["axes"]); ncols = p if PANELS else 4
        fig = plt.figure(figsize=(float(os.environ.get("ROW_W", 15.0 * ncols / 4)), float(os.environ.get("ROW_H", "2.73")))); fig.set_layout_engine("none")
        assert ncols % p == 0; span = ncols // p
        strip = DUCK_SPLIT == "strip" and ROW_ONLY == "duckdb"
        gs = fig.add_gridspec(2 if strip else 1, ncols, wspace=float(os.environ.get("ROW_WSPACE", "0.18")),
                              **(dict(height_ratios=[1.0, float(os.environ.get("STRIP_RATIO", "0.32"))], hspace=float(os.environ.get("STRIP_HSPACE", "0.16"))) if strip else {}))
        axs = [fig.add_subplot(gs[0, 0:span])]
        axs += [fig.add_subplot(gs[0, j * span:(j + 1) * span], **({} if UNSHARE_Y else dict(sharey=axs[0]))) for j in range(1, p)]
        for ax in axs[1:]:
            ax.tick_params(labelleft=UNSHARE_Y)
        if os.environ.get("HIDE_LEFT") == "1": axs[0].tick_params(labelleft=False)
        handles = draw_row(axs, row)
        if UNSHARE_Y:
            for ax in axs:
                ys = [y for ln in ax.lines for y in ln.get_ydata() if y and y > 0]
                ys += [y for c in ax.collections for _, y in c.get_offsets() if y > 0]
                if ys:
                    ax.set_ylim(min(ys) / float(os.environ.get("UNSHARE_LO", "1.6")), max(ys) * float(os.environ.get("UNSHARE_HI", "1.8")))
        if strip:
            saxs = [fig.add_subplot(gs[1, j * span:(j + 1) * span]) for j in range(p)]
            draw_split_strip(axs, saxs)
        if handles:
            fig.canvas.draw()
            y = max(ax.get_position().y1 for ax in axs) + 0.02
            if os.environ.get("STRIP_GAP_IN"):
                y = max(ax.get_position().y1 for ax in axs) + float(os.environ["STRIP_GAP_IN"]) / fig.get_figheight()
            small = [ps.proxy(color=h.get_color(), marker=h.get_marker(), ls=h.get_linestyle(), label=h.get_label(), ms=6, lw=1.6) for h in handles]
            if DUCK_SPLIT:
                small = handles
            if ESPER_GROUPED_LEGEND and ROW_ONLY == "esper" and os.environ.get("GROUP_INSIDE") == "1":
                inside_groups(axs[-1], small)
            elif ESPER_GROUPED_LEGEND and ROW_ONLY == "esper":
                strip_groups(fig, small, y)
            else:
                fig.legend(**legend_kw(small),
                       loc="lower center", bbox_to_anchor=(0.5, y), ncol=int(os.environ.get("STRIP_NCOL", len(handles))), columnspacing=float(os.environ.get("STRIP_LEG_CS", "1.0")),
                       handlelength=float(os.environ.get("STRIP_LEG_HL", "1.6")), handletextpad=0.35, borderpad=0.25, framealpha=0.92, fontsize=STRIP_FONTSIZE - (1 if len(handles) >= 6 else 0),
                       **(dict(borderaxespad=0.0) if os.environ.get("STRIP_GAP_IN") else {}))
        FIGS.mkdir(parents=True, exist_ok=True)
        out = NAME + ("_duckdb" if ROW_ONLY == "duckdb" else "_esper") + ("_panels" + "".join(PANELS) if PANELS else "") + ("_strip" if (handles and LEGEND_STRIP and os.environ.get("GROUP_INSIDE") != "1") else "") + ("_legin" if os.environ.get("GROUP_INSIDE") == "1" else "") + ("_yellow" if DUCKDB_MR_COLOR == ps.C["yellow"] else "")
        fig.canvas.draw(); pad = float(os.environ.get("ROW_PAD", "0.05"))
        bb = fig.get_tightbbox(fig.canvas.get_renderer()).padded(pad); bb.x1 += float(os.environ.get("ROW_RIGHT_EXTRA", "0.08"))
        if os.environ.get("ROW_VBOX"):
            p_ = axs[0].get_position(); h_ = fig.get_figheight(); below, above = (float(x) for x in os.environ["ROW_VBOX"].split(","))
            print(f"vbox {ROW_ONLY}: tight needs below {p_.y0 * h_ - bb.y0:.3f} above {bb.y1 - p_.y1 * h_:.3f} in; panel {p_.width * fig.get_figwidth():.3f} x {p_.height * h_:.3f} in")
            bb.y0 = p_.y0 * h_ - below; bb.y1 = p_.y1 * h_ + above
            for l_ in fig.legends: print(f"legend gap {ROW_ONLY}: {l_.get_frame().get_window_extent().y0 / fig.dpi - p_.y1 * h_:.4f} in above the panels")
        for ext in ("png", "pdf"):
            fig.savefig(FIGS / f"{out}.{ext}", bbox_inches=bb, dpi=200)
        plt.close(fig)
    print(f"wrote {FIGS / out}.{{png,pdf}} from {row['results'].name}")


def main() -> None:
    ps.apply(usetex=bool(shutil.which("latex") and shutil.which("dvipng")))
    if ROW_ONLY:
        return main_row_only()
    with plt.rc_context(ROW4_FONTS):
        fig = plt.figure(figsize=(15.0, 7.6)); fig.set_layout_engine("none")
        needs_strip = any(len(r["style"]) > 2 for r in ROWS)
        gs = fig.add_gridspec(2, 4, hspace=1.0 if needs_strip else 0.62, wspace=0.18)
        row1 = [fig.add_subplot(gs[0, 0])]
        row1 += [fig.add_subplot(gs[0, j], sharey=row1[0]) for j in range(1, 4)]
        p2 = len(ESP_AXES); assert 4 % p2 == 0, "the Esper row's panels must tile four columns"; span = 4 // p2
        row2 = [fig.add_subplot(gs[1, 0:span])]
        row2 += [fig.add_subplot(gs[1, j * span:(j + 1) * span], sharey=row2[0]) for j in range(1, p2)]
        for axs in (row1, row2):
            for ax in axs[1:]:
                ax.tick_params(labelleft=False)
        strips = [(axs, draw_row(axs, row)) for axs, row in ((row1, ROWS[0]), (row2, ROWS[1]))]
        fig.canvas.draw()
        for axs, handles in strips:
            if handles:
                y = max(ax.get_position().y1 for ax in axs) + 0.015
                fig.legend(**legend_kw(handles), loc="lower center", bbox_to_anchor=(0.5, y), ncol=len(handles), columnspacing=1.6,
                           handlelength=1.8, handletextpad=0.4, borderpad=0.3, framealpha=0.92)
        FIGS.mkdir(parents=True, exist_ok=True)
        for ext in ("png", "pdf"):
            fig.savefig(FIGS / f"{NAME}.{ext}", bbox_inches="tight", pad_inches=0.25, dpi=200)
        plt.close(fig)
    print(f"wrote {FIGS / NAME}.{{png,pdf}} from {DUCKDB_RESULTS.name} + {ESPER_RESULTS.name}")


if __name__ == "__main__":
    main()
