"""Plot style of the paper's figures: helpers on top of eimer_style.mplstyle (colour, marker and label per approach,
figure sizes, hatched bars, saving as .png and .pdf). ,,apply()'' loads the style; ,,apply(usetex=False)'' is the
fallback without a LaTeX install.
"""
from __future__ import annotations
import warnings
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.lines import Line2D

_HERE = Path(__file__).resolve().parent
MPLSTYLE = _HERE / "eimer_style.mplstyle"

C = {"blue": "#0072B2", "vermillion": "#D55E00", "green": "#009E73", "purple": "#CC79A7",
     "orange": "#E69F00", "sky": "#56B4E9", "yellow": "#F0E442", "black": "#000000"}
CYCLE   = [C["blue"], C["vermillion"], C["green"], C["purple"],
           C["orange"], C["sky"], C["yellow"], C["black"]]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]
PARAM_CMAP = plt.get_cmap("cividis")

ABBR = {"edge_cover": "EC", "clique_grow": "CG", "eimer": "EIMER",
        "mr": "MR", "match_recognize": "MR", "random": "Rand", "bushy": "Bushy",
        "left_deep": "LD", "baseline": "Base",
        "stitch": "STITCH", "rescan": "RESCAN", "full_rescan": "RESCAN"}
APPROACH_MARKER = {"EC": "o", "CG": "s", "EIMER": "D",
                   "MR": "v", "Rand": "P", "Bushy": "X", "LD": "*", "Base": "h",
                   "STITCH": "D", "RESCAN": "v"}
APPROACH_COLOR  = {"EC": C["blue"], "CG": C["vermillion"],
                   "EIMER": C["blue"], "MR": C["vermillion"],
                   "Rand": C["purple"], "Bushy": C["orange"], "LD": C["sky"],
                   "Base": C["black"], "STITCH": C["blue"], "RESCAN": C["vermillion"]}

WIDTHS = {"col": 3.33, "text": 7.0, "wide": 13.2}

_PAPER_RC = {"font.size": 9, "axes.titlesize": 9.5, "axes.labelsize": 10,
             "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8}

PAPER_FONTS = {"font.size": 19, "axes.labelsize": 23, "xtick.labelsize": 19,
               "ytick.labelsize": 19, "legend.fontsize": 15, "legend.title_fontsize": 16}
PAPER_ROW_FONTS = {"font.size": 16, "axes.labelsize": 19, "xtick.labelsize": 16,
                   "ytick.labelsize": 16, "legend.fontsize": 12.5, "legend.title_fontsize": 13.5}

_UNICODE_TEX = {"σ": r"$\sigma$", "≥": r"$\geq$", "≤": r"$\leq$", "→": r"$\rightarrow$",
                "×": r"$\times$", "≈": r"$\approx$", "—": "---", "–": "--",
                "↑": r"$\uparrow$", "↓": r"$\downarrow$", "·": r"$\cdot$",
                "#": r"\#", "%": r"\%", "&": r"\&", "_": r"\_"}
def tex(s: str) -> str:
    for k, v in _UNICODE_TEX.items():
        s = s.replace(k, v)
    return s


def apply(target: str = "report", usetex: bool = True) -> None:
    plt.style.use(str(MPLSTYLE))
    if target == "paper":
        plt.rcParams.update(_PAPER_RC)
    if not usetex:
        plt.rcParams.update({
            "text.usetex": False, "mathtext.fontset": "cm",
            "font.family": "serif",
            "font.serif": ["CMU Serif", "Latin Modern Roman", "STIXGeneral", "DejaVu Serif"],
            "axes.labelweight": "bold", "axes.titleweight": "bold",
        })


def figure(width="text", ratio: float = 0.62, nrows: int = 1, ncols: int = 1,
           h: float | None = None, **subplots_kw):
    w = WIDTHS.get(width, width)
    height = h if h is not None else w * ratio
    return plt.subplots(nrows, ncols, figsize=(w, height), **subplots_kw)


def short_name(name: str) -> str:
    out = name
    for k, v in ABBR.items():
        out = out.replace(k, v)
    return out


def approach_style(name: str):
    s = short_name(name)
    return APPROACH_COLOR.get(s, C["blue"]), APPROACH_MARKER.get(s, "o"), s


def parse_series(label: str):
    appr, param = label, None
    for sep in (", p=", ", σ=", ", sigma=", ", N=", ", n=", ", B="):
        if sep in label:
            appr, pv = label.split(sep, 1)
            try:
                param = float(pv.split(",")[0])
            except ValueError:
                param = None
            break
    return short_name(appr), param


def param_colors(values):
    vs = sorted(set(values))
    norm = mcolors.Normalize(min(vs), max(vs)) if len(vs) > 1 else mcolors.Normalize(0, 1)
    return norm, {v: PARAM_CMAP(norm(v)) for v in vs}


def rotate_xticks(ax, deg: int = 30):
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(deg); lbl.set_ha("right"); lbl.set_rotation_mode("anchor")


def grid_both(ax):
    ax.grid(True, which="major", ls=":", lw=0.6, alpha=0.55, color="#b0b0b0")
    ax.grid(True, which="minor", ls=":", lw=0.5, alpha=0.25, color="#c8c8c8")


def breakeven(ax, y: float = 1.0, color: str = "0.45", lw: float = 1.1, **kw):
    ax.axhline(y, ls="--", color=color, lw=lw, zorder=2, **kw)


def panel_tag(ax, tag: str, title: str = ""):
    ax.set_title(f"({tag})~~{title}" if title else f"({tag})", loc="left")


def check_labels(labels, max_len: int = 22):
    longest = max((len(str(x)) for x in labels), default=0)
    if longest > max_len:
        warnings.warn(f"[plot_style] legend label exceeds {max_len} chars (got {longest}); "
                      f"abbreviate via the registry or drop redundant text.", stacklevel=2)
    return longest


def proxy(color="0.3", marker="o", ls="none", label="", **kw):
    return Line2D([], [], color=color, marker=marker, linestyle=ls, label=label, **kw)


def save(fig, path, formats=("png", "pdf")):
    p = Path(path)
    p = p.with_suffix("")
    p.parent.mkdir(parents=True, exist_ok=True)
    for ext in formats:
        fig.savefig(p.with_suffix(f".{ext}"))
    print(f"-> {p}.{{{','.join(formats)}}}")


from matplotlib.artist import Artist as _Artist
from matplotlib.collections import LineCollection as _LineCollection
from matplotlib.legend_handler import HandlerBase as _HandlerBase, HandlerTuple as _HandlerTuple
from matplotlib.patches import Rectangle as _Rectangle
from matplotlib.transforms import IdentityTransform as _Identity
import math as _math

HATCH_SPACING_IN = 0.052
HATCH_LW = 0.7
HATCH_FILL_ALPHA = 0.22
HATCH_KINDS = ("diag", "rdiag", "cross", "horiz", "vert", "grid", "dots")
_HATCH_BY_ARM = (("edge", "rdiag"), ("prefix", "cross"), ("full", "dots"), ("singleton", "horiz"), ("dyn", "grid"),
                 ("unbounded", "vert"),
                 ("transpiled", "dots"), ("native", "dots"), ("_mr", "dots"))


def hatch_kind(arm: str) -> str:
    a = arm.lower()
    for key, kind in _HATCH_BY_ARM:
        if key in a:
            return kind
    return "diag"


def _clip_segment(p, q, x0, y0, x1, y1):
    (px, py), (qx, qy) = p, q; dx, dy = qx - px, qy - py; t0, t1 = 0.0, 1.0
    for num, den in ((px - x0, -dx), (x1 - px, dx), (py - y0, -dy), (y1 - py, dy)):
        if den == 0:
            if num < 0: return None
            continue
        t = num / den
        if den < 0:
            if t > t1: return None
            t0 = max(t0, t)
        else:
            if t < t0: return None
            t1 = min(t1, t)
    return ((px + t0 * dx, py + t0 * dy), (px + t1 * dx, py + t1 * dy))


def hatch_geometry(x0, y0, x1, y1, kind: str, spacing: float):
    segs, dots = [], []
    w, h = x1 - x0, y1 - y0
    if kind in ("diag", "cross"):
        c = (x0 - y1) - spacing
        while c <= x1 - y0 + spacing:
            s = _clip_segment((c + y0 - spacing * 0, y0), (c + y1, y1), x0, y0, x1, y1)
            if s: segs.append(s)
            c += spacing * _math.sqrt(2)
    if kind in ("rdiag", "cross"):
        c = (x0 + y0) - spacing
        while c <= x1 + y1 + spacing:
            s = _clip_segment((c - y0, y0), (c - y1, y1), x0, y0, x1, y1)
            if s: segs.append(s)
            c += spacing * _math.sqrt(2)
    if kind in ("horiz", "grid"):
        y = y0 + spacing / 2
        while y < y1:
            segs.append(((x0, y), (x1, y))); y += spacing
    if kind in ("vert", "grid"):
        x = x0 + spacing / 2
        while x < x1:
            segs.append(((x, y0), (x, y1))); x += spacing
    if kind == "dots":
        y = y0 + spacing / 2; row = 0
        while y < y1:
            x = x0 + spacing / 2 + (spacing / 2 if row % 2 else 0)
            while x < x1:
                dots.append((x, y)); x += spacing
            y += spacing * 0.87; row += 1
    return segs, dots


class _BarHatch(_Artist):
    def __init__(self, patch, color, kind, lw=HATCH_LW, zorder=1):
        super().__init__(); self._patch, self._color, self._kind, self._lw = patch, color, kind, lw
        self.set_zorder(zorder + 0.01)

    def draw(self, renderer):
        if not self.get_visible(): return
        bb = self._patch.get_window_extent(renderer)
        if bb.width <= 0 or bb.height <= 0: return
        spacing = HATCH_SPACING_IN * self.figure.dpi
        segs, dots = hatch_geometry(bb.x0, bb.y0, bb.x1, bb.y1, self._kind, spacing)
        if segs:
            lc = _LineCollection(segs, colors=[self._color], linewidths=self._lw, transform=_Identity(), zorder=self.get_zorder())
            lc.set_figure(self.figure); lc.set_clip_path(self._patch); lc.draw(renderer)
        if dots:
            ln = Line2D([d[0] for d in dots], [d[1] for d in dots], ls="none", marker="o", ms=self._lw * 2.3, mfc=self._color, mec=self._color,
                        mew=0, transform=_Identity(), zorder=self.get_zorder())
            ln.set_figure(self.figure); ln.set_clip_path(self._patch); ln.draw(renderer)


def hatched_bar(ax, x, height, width, bottom, color, arm_or_kind: str, zorder=1, lw=HATCH_LW, edgecolor=None, hatch_color=None, fill_alpha=None):
    kind = arm_or_kind if arm_or_kind in HATCH_KINDS else hatch_kind(arm_or_kind)
    bar = ax.bar(x, height, width=width, bottom=bottom, facecolor=mcolors.to_rgba(color, HATCH_FILL_ALPHA if fill_alpha is None else fill_alpha),
                 edgecolor=edgecolor or color, linewidth=0.8, zorder=zorder)[0]
    ax.add_artist(_BarHatch(bar, hatch_color or color, kind, lw=lw, zorder=zorder))
    return bar


class HatchSwatch:
    def __init__(self, color, arm_or_kind: str, label: str = "", edgecolor=None, hatch_color=None, fill_alpha=None):
        self.color = color; self.kind = arm_or_kind if arm_or_kind in HATCH_KINDS else hatch_kind(arm_or_kind); self._label = label
        self.edgecolor = edgecolor or color; self.hatch_color = hatch_color or color; self.fill_alpha = HATCH_FILL_ALPHA if fill_alpha is None else fill_alpha
    def get_label(self): return self._label
    def set_label(self, s): self._label = s
    def get_visible(self): return True


class _HandlerHatchSwatch(_HandlerBase):
    def create_artists(self, legend, h, xdescent, ydescent, width, height, fontsize, trans):
        x0, y0 = -xdescent, -ydescent; x1, y1 = x0 + width, y0 + height
        rect = _Rectangle((x0, y0), width, height, facecolor=mcolors.to_rgba(h.color, h.fill_alpha), edgecolor=h.edgecolor, linewidth=0.8, transform=trans)
        segs, dots = hatch_geometry(x0, y0, x1, y1, h.kind, HATCH_SPACING_IN * 72.0)
        arts = [rect]
        if segs:
            arts.append(_LineCollection(segs, colors=[h.hatch_color], linewidths=HATCH_LW, transform=trans))
        if dots:
            arts.append(Line2D([d[0] for d in dots], [d[1] for d in dots], ls="none", marker="o", ms=HATCH_LW * 2.3, mfc=h.hatch_color, mec=h.hatch_color, mew=0, transform=trans))
        return arts


HATCH_HANDLER_MAP = {HatchSwatch: _HandlerHatchSwatch(), tuple: _HandlerTuple(ndivide=None, pad=0.0)}
