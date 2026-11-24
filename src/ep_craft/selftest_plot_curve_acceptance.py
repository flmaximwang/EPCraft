#!/usr/bin/env python3
"""selftest_plot_curve_acceptance — independent acceptance suite for ep-bqv.4 (`plot-curve`).

Ticket: ep-bqv.4 "plot-curve: render the standard curve and residuals to PNG".
Contract: specs/ep-craft-densitometry.md §2.6, §3.4, §4.5, §4.6, §4.7 and §7 (a)-(f), plus the
ticket's acceptance criteria.

This suite is deliberately SEPARATE from the implementation change's own selftests.  It drives
only the contract the ticket publishes — `dens_plot.plot_curve(curve, out, quant=None,
width=1600)`, `dens_plot.curve_layout(curve, quant=None, width=1600)` and the CLI
(`ep-craft densitometry plot-curve` / `dens plot-curve`) — with hand-written curve.json /
quant.json fixtures (spec §4.5 / §4.6 shapes) and one fixture built by the real upstream chain
(`selftest_densitometry._synthetic_report` → `measure` → `build_curve`) so the picture is also
tied to the numbers the earlier tickets produce.

Drawing expectations are derived HERE, from the fixture JSON and the axes frame: the fit is
evaluated from the slope/intercept (or scale/exponent) written in the fixture, and every
value→pixel step is recomputed by `_x` / `_y` / `_yr` from the boxes and ranges that
`curve_layout` publishes, never by calling the renderer's own `.x()` / `.y()` / `.yr()` (those are
only cross-checked against this arithmetic in `check_geometry_and_frames`).  `curve_layout` is
used for the facts the renderer must publish anyway: canvas size, the two pixel boxes, the three
value ranges, the fitted line's ends, the annotation text and the font.

Requires a real ImageMagick (like the other pixel suites): every pixel below is read back from
an actually rendered PNG.

One contract question is deliberately NOT settled here: `curve.points[].sd` is the spread of the
replicate AREAS (spec §4.5) while §4.7 asks for vertical error bars, i.e. a spread in MASS.  The
suite checks that the renderer is self-consistent about the axis it picks
(`check_error_bar_axis_conflict`) and prints the conflict as a separate `注意：…` line after the
count; the literal vertical form the ticket asks for is checked by `check_error_bars`, which
fails if the renderer switches axis until the spec/ticket decides.  See `ERROR_BAR_CONFLICT`.

Run:  PYTHONPATH=src python3 -m ep_craft.selftest_plot_curve_acceptance
`run()` returns the number of passed checks and prints `selftest_plot_curve_acceptance N/N 通过`.
"""
import contextlib
import importlib
import io
import json
import math
import os
import tempfile

from . import densitometry
from . import magick_io
from . import text_layout

KIND = densitometry.CURVE_KIND
QUANT_KIND = "ep-craft-densitometry-quant"
WHITE = (255, 255, 255)

# Known contract conflicts this suite must NOT settle on its own (reported to the lead/ticket).
ERROR_BAR_CONFLICT = (
    "sd-axis",
    "§4.7 / ep-bqv.4 的验收要的是竖直（质量轴）误差棒，而 curve.points[].sd 是重复点【面积】"
    "（gray*cm，§4.5）的离散度。渲染器把 sd 原样画在 %s 轴上，棒半长读作 %s；两种读法在像素上"
    "差 6 倍以上。要么改成面积轴上的水平棒，要么由 curve 提供质量单位的 sd——"
    "需要 spec/工单拍板，本套件只验证渲染器与自己的合同自洽，不替它挑。",
)


# ---------------------------------------------------------------- public API guard

def _plot():
    """The renderer module, with the names this ticket promises (clear failure before .4 lands).

    Imported through importlib on purpose: `selftest.py`'s import-direction guard refuses a
    module that statically imports a package module which does not exist yet, and this suite has
    to live on the base branch (where `dens_plot` is still ep-bqv.2/ep-bqv.4's work) without
    tripping it.  The lazy import also keeps `selftest` cheap when plot-curve is not used.
    """
    try:
        dens_plot = importlib.import_module(".dens_plot", __package__)
    except ImportError:
        raise AssertionError("ep_craft.dens_plot 不存在——ep-bqv.4（plot-curve）还没落到这个分支上")
    missing = [name for name in ("plot_curve", "curve_layout")
               if not callable(getattr(dens_plot, name, None))]
    if missing:
        raise AssertionError("dens_plot 缺少 %s——ep-bqv.4（plot-curve）还没落到这个分支上"
                             % "、".join(missing))
    return dens_plot


def _colours():
    """The marker colours the renderer publishes (interface pinned by the ticket lead)."""
    dens_plot = _plot()
    wanted = ("POINT_RGB", "DROP_RGB", "LINE_RGB", "SAMPLE_RGB", "OOR_RGB", "GUIDE_RGB")
    missing = [name for name in wanted if not hasattr(dens_plot, name)]
    if missing:
        raise AssertionError("dens_plot 缺少颜色常量 %s" % "、".join(missing))
    return {"point": tuple(dens_plot.POINT_RGB), "drop": tuple(dens_plot.DROP_RGB),
            "line": tuple(dens_plot.LINE_RGB), "sample": tuple(dens_plot.SAMPLE_RGB),
            "oor": tuple(dens_plot.OOR_RGB), "guide": tuple(dens_plot.GUIDE_RGB),
            "zero": tuple(getattr(dens_plot, "ZERO_RGB", (60, 60, 60))),
            "err": tuple(getattr(dens_plot, "ERR_RGB", (105, 105, 105)))}


# ---------------------------------------------------------------- CLI harness

def _cli(argv):
    """Run the public CLI in-process → (rc, stdout, stderr); argparse exits count as rc 2."""
    from . import gel_photo
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = gel_photo.main(list(argv), canvas=True)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _one_line(stderr, label):
    """The CLI's error boundary: exactly one `[!] …` line, no traceback (§3)."""
    lines = stderr.rstrip("\n").split("\n")
    assert len(lines) == 1, "%s: stderr 不是一行：%r" % (label, stderr)
    assert lines[0].startswith("[!] "), "%s: 不是 [!] 开头的错误行：%r" % (label, lines[0])
    assert "Traceback" not in stderr, "%s: 吐了 traceback：%r" % (label, stderr)


@contextlib.contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


# ---------------------------------------------------------------- pixels

class _Pic(object):
    """A rendered PNG as native-size (or 800 px thumbnail) RGB triples."""

    def __init__(self, path, scaled=False):
        self.w, self.h, self.rgb = magick_io.read_ppm(path, 800 if scaled else 0)

    def px(self, x, y):
        x, y = int(round(x)), int(round(y))
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return None
        i = (y * self.w + x) * 3
        return (self.rgb[i], self.rgb[i + 1], self.rgb[i + 2])


def _window(box, w, h):
    """A float pixel box → inclusive integer bounds clipped to the image."""
    x0, y0, x1, y1 = box
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return (max(0, int(math.floor(x0))), max(0, int(math.floor(y0))),
            min(w - 1, int(math.ceil(x1))), min(h - 1, int(math.ceil(y1))))


def _match(rgb, i, colour, tol):
    return (abs(rgb[i] - colour[0]) <= tol and abs(rgb[i + 1] - colour[1]) <= tol
            and abs(rgb[i + 2] - colour[2]) <= tol)


def _has(pic, colour, box, tol=8):
    """True when `box` holds at least one pixel within `tol` of `colour`."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    for y in range(y0, y1 + 1):
        base = y * pic.w
        for x in range(x0, x1 + 1):
            if _match(pic.rgb, (base + x) * 3, colour, tol):
                return True
    return False


def _count(pic, colour, box, tol=8, limit=None):
    """Number of pixels within `tol` of `colour` inside `box` (stops at `limit`)."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    n = 0
    for y in range(y0, y1 + 1):
        base = y * pic.w
        for x in range(x0, x1 + 1):
            if _match(pic.rgb, (base + x) * 3, colour, tol):
                n += 1
                if limit is not None and n >= limit:
                    return n
    return n


def _row_has(pic, colour, y, x0, x1, tol=8, least=4, samples=60, band=3):
    """True when one of the rows y±band carries `least` pixels of `colour` across x0..x1."""
    x0, _y0, x1, _y1 = _window((x0, y - band, x1, y + band), pic.w, pic.h)
    step = max(1.0, (x1 - x0) / float(samples))
    for dy in range(-band, band + 1):
        yy = int(round(y)) + dy
        if yy < 0 or yy >= pic.h:
            continue
        n = 0
        x = x0
        while x <= x1:
            xx = int(round(x))
            if _match(pic.rgb, (yy * pic.w + xx) * 3, colour, tol):
                n += 1
                if n >= least:
                    return True
            x += step
    return False


def _white(pic, x, y, tol=10):
    px = pic.px(x, y)
    return px is not None and all(abs(c - 255) <= tol for c in px)


def _ink(pic, box, dark=205, spread=14):
    """True when `box` holds a dark, neutral pixel (axis ink, line, ring — never a fill)."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    for y in range(y0, y1 + 1):
        base = y * pic.w
        for x in range(x0, x1 + 1):
            i = (base + x) * 3
            r, g, b = pic.rgb[i], pic.rgb[i + 1], pic.rgb[i + 2]
            if max(r, g, b) <= dark and (max(r, g, b) - min(r, g, b)) <= spread:
                return True
    return False


def _ink_rows(pic, box, dark=205, spread=14, least=6):
    """Rows of `box` carrying at least `least` dark neutral pixels (a line of text)."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    rows = 0
    for y in range(y0, y1 + 1):
        base = y * pic.w
        n = 0
        for x in range(x0, x1 + 1):
            i = (base + x) * 3
            r, g, b = pic.rgb[i], pic.rgb[i + 1], pic.rgb[i + 2]
            if max(r, g, b) <= dark and (max(r, g, b) - min(r, g, b)) <= spread:
                n += 1
                if n >= least:
                    rows += 1
                    break
    return rows


def _ink_total(pic, box, dark=205, spread=14):
    """Number of dark neutral pixels in `box` (text ink, rule ink)."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    n = 0
    for y in range(y0, y1 + 1):
        base = y * pic.w
        for x in range(x0, x1 + 1):
            i = (base + x) * 3
            r, g, b = pic.rgb[i], pic.rgb[i + 1], pic.rgb[i + 2]
            if max(r, g, b) <= dark and (max(r, g, b) - min(r, g, b)) <= spread:
                n += 1
    return n


def _non_white_fraction(pic):
    n = pic.w * pic.h
    hits = 0
    for i in range(0, n * 3, 3):
        if (abs(pic.rgb[i] - 255) > 6 or abs(pic.rgb[i + 1] - 255) > 6
                or abs(pic.rgb[i + 2] - 255) > 6):
            hits += 1
    return hits / float(n) if n else 0.0


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _diff_pixels(a, b):
    """Pixels that differ between two same-size PNGs (None when the sizes differ)."""
    pa, pb = _Pic(a), _Pic(b)
    if (pa.w, pa.h) != (pb.w, pb.h):
        return None
    return sum(1 for i in range(0, len(pa.rgb), 3) if pa.rgb[i:i + 3] != pb.rgb[i:i + 3])


def _no_pixels(path, label):
    """No leftover plot debris: *.mvg scripts or the renderer's temp-file names."""
    hits = []
    directory = os.path.dirname(os.path.abspath(path)) or "."
    for name in os.listdir(directory):
        if name.endswith(".mvg") or name.startswith(".dens-plot-"):
            hits.append(os.path.join(directory, name))
    assert not hits, "%s：scratch 目录留下了 %r" % (label, hits)


# ---------------------------------------------------------------- independent geometry

def _x(geo, volume):
    """volume → pixel x inside the main axes, recomputed from the published frame."""
    x0, _y0, x1, _y1 = geo["main_box"]
    lo, hi = geo["x_range"]
    return x0 + (float(volume) - lo) / (hi - lo) * (x1 - x0)


def _y(geo, mass):
    """mass → pixel y inside the main axes (0 at the bottom), recomputed."""
    _x0, y0, _x1, y1 = geo["main_box"]
    lo, hi = geo["y_range"]
    return y1 - (float(mass) - lo) / (hi - lo) * (y1 - y0)


def _yr(geo, rel_pct):
    """percent deviation → pixel y inside the residual strip, recomputed."""
    _x0, y0, _x1, y1 = geo["resid_box"]
    lo, hi = geo["rel_range"]
    return y1 - (float(rel_pct) - lo) / (hi - lo) * (y1 - y0)


def _fit_value(curve, volume):
    """The fitted model of the fixture JSON at one volume (no renderer code involved)."""
    fp = curve["fit_params"]
    volume = float(volume)
    if curve["fit"] == "linear":
        return float(fp["intercept"]) + float(fp["slope"]) * volume
    return float(fp["scale"]) * (volume ** float(fp["exponent"]))


def _three(value):
    """`40.0` → `40`, `13.5` → `13.5` (the 3 dp trimmed form used by the standard range)."""
    text = "%.3f" % float(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _two(value):
    """`3.7621` → `3.76`, `1.2885` → `1.29`, `-1.4968` → `-1.5` (the 2 dp equation form)."""
    text = "%.2f" % float(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# ---------------------------------------------------------------- fixtures (spec §4.5 / §4.6)

def _linear_curve():
    """Standard curve fixture: 4 points (one dropped, one replicate pair), linear fit, flank."""
    return {
        "version": 1,
        "kind": KIND,
        "unit": "ug",
        "baseline": "flank",
        "fit": "linear",
        "source": {"dens": "fig.dens.json",
                   "std": "2=5,3=10,4=20,5=20,6=40",
                   "drop": [2]},
        "points": [
            {"mass": 5.0, "volume": 1.7, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [2], "entries": [[2, 1]], "used": False},
            {"mass": 10.0, "volume": 3.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [3], "entries": [[3, 1]], "used": True},
            {"mass": 20.0, "volume": 5.8, "n": 2, "sd": 0.849, "cv_pct": 14.63,
             "lanes": [4, 5], "entries": [[4, 1], [5, 1]], "used": True},
            {"mass": 40.0, "volume": 11.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [6], "entries": [[6, 1]], "used": True},
        ],
        "fit_params": {"slope": 3.7621, "intercept": -1.4968, "r2": 0.9997, "n": 3,
                       "space": "linear"},
        "range": {"volume_min": 3.0, "volume_max": 11.0, "mass_min": 10.0, "mass_max": 40.0},
        "residuals": [
            {"mass": 10.0, "volume": 3.0, "predicted_mass": 9.790, "rel_pct": -2.1},
            {"mass": 20.0, "volume": 5.8, "predicted_mass": 20.324, "rel_pct": 1.62},
            {"mass": 40.0, "volume": 11.0, "predicted_mass": 39.887, "rel_pct": -0.28},
        ],
        "notes": [],
    }


def _power_curve():
    """Power-fit fixture: 4 points, unit ng, global baseline, nothing dropped."""
    return {
        "version": 1,
        "kind": KIND,
        "unit": "ng",
        "baseline": "global",
        "fit": "power",
        "source": {"dens": "pow.dens.json", "std": "2=4,3=9,4=20,5=42", "drop": []},
        "points": [
            {"mass": 4.0, "volume": 3.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [2], "entries": [[2, 1]], "used": True},
            {"mass": 9.0, "volume": 6.2, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [3], "entries": [[3, 1]], "used": True},
            {"mass": 20.0, "volume": 13.2, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [4], "entries": [[4, 1]], "used": True},
            {"mass": 42.0, "volume": 28.0, "n": 1, "sd": None, "cv_pct": None,
             "lanes": [5], "entries": [[5, 1]], "used": True},
        ],
        "fit_params": {"exponent": 1.0529, "scale": 1.2885, "r2": 0.9992, "n": 4,
                       "space": "log10"},
        "range": {"volume_min": 3.0, "volume_max": 28.0, "mass_min": 4.0, "mass_max": 42.0},
        "residuals": [
            {"mass": 4.0, "volume": 3.0, "predicted_mass": 4.097, "rel_pct": 2.42},
            {"mass": 9.0, "volume": 6.2, "predicted_mass": 8.798, "rel_pct": -2.24},
            {"mass": 20.0, "volume": 13.2, "predicted_mass": 19.496, "rel_pct": -2.52},
            {"mass": 42.0, "volume": 28.0, "predicted_mass": 43.033, "rel_pct": 2.46},
        ],
        "notes": [],
    }


def _quant():
    """quant.json fixture (§4.6): two sample lanes, one with two bands, one out of range."""
    def band(index, y_cm, mw, area, mass, oor, rel):
        return {"index": index, "y_cm": y_cm, "mw_kda": mw, "area": area, "mass": mass,
                "out_of_range": oor, "rel_pct": rel}

    return {
        "version": 1,
        "kind": QUANT_KIND,
        "unit": "ug",
        "baseline": "flank",
        "fit": "linear",
        "source": {"dens": "fig.dens.json", "curve": "fig.curve.json", "load": "9=2,11=2,12=2"},
        "lanes": [
            {"lane": 9, "label": "S1", "load_ul": 2.0,
             "bands": [band(1, 1.4, 50.0, 4.2, 14.3, False, 3.5),
                       band(2, 3.9, 30.0, 8.6, 30.6, False, -1.2)],
             "mass_total": 44.9, "conc": 22.45, "out_of_range": False,
             "out_of_range_bands": [], "frac_pct_among_lanes": 66.6},
            {"lane": 11, "label": "S3", "load_ul": 2.0,
             "bands": [band(1, 2.2, 40.0, 6.1, 21.4, False, 0.6)],
             "mass_total": 21.4, "conc": 10.7, "out_of_range": False,
             "out_of_range_bands": [], "frac_pct_among_lanes": 33.4},
            {"lane": 12, "label": "S4", "load_ul": 2.0,
             "bands": [band(1, 2.0, 40.0, 1.1, None, True, None)],
             "mass_total": None, "conc": None, "out_of_range": True,
             "out_of_range_bands": [1], "frac_pct_among_lanes": None},
        ],
        "overall_mass": 66.3,
        "notes": ["泳道 12 有 1 条带超出标曲范围"],
    }


def _wide_frame_curve():
    """A linear curve with a dropped point far to the right: the frame (1.08 x 40) is wider than
    the +10 % extension, so the dashed tail must stop at its extension end, not at the edge."""
    curve = _linear_curve()
    curve["points"] = curve["points"] + [
        {"mass": 4.0, "volume": 40.0, "n": 1, "sd": None, "cv_pct": None,
         "lanes": [7], "entries": [[7, 1]], "used": False}]
    curve["source"] = {"dens": "fig.dens.json", "std": "2=5,3=10,4=20,5=20,6=40,7=4",
                       "drop": [2, 7]}
    return curve


def _big_residual_curve():
    """A BSA-like bad fit: one residual at -174 % pushes the strip to ±208 %, so the ±10 % guides
    land a few pixels from the zero line and cannot carry their own labels."""
    curve = _linear_curve()
    curve["residuals"] = [
        {"mass": 10.0, "volume": 3.0, "predicted_mass": -13.5, "rel_pct": -174.0},
        {"mass": 20.0, "volume": 5.8, "predicted_mass": 20.324, "rel_pct": 1.62},
        {"mass": 40.0, "volume": 11.0, "predicted_mass": 39.887, "rel_pct": -0.28},
    ]
    return curve


def _single_point_curve():
    """One standard point only: `n: 1 point` (singular), an unknown r2 and a single-value range."""
    curve = _linear_curve()
    curve["points"] = [{"mass": 5.0, "volume": 2.0, "n": 1, "sd": None, "cv_pct": None,
                        "lanes": [2], "entries": [[2, 1]], "used": True}]
    curve["fit_params"] = {"slope": 2.0, "intercept": 0.0, "r2": None, "n": 1,
                           "space": "linear"}
    curve["range"] = {"volume_min": 2.0, "volume_max": 2.0,
                      "mass_min": 5.0, "mass_max": 5.0}
    curve["residuals"] = [{"mass": 5.0, "volume": 2.0, "predicted_mass": 4.0, "rel_pct": -20.0}]
    curve["source"] = {}
    return curve


def _nonascii_curve():
    """User-supplied `unit` / `baseline` with non-ASCII text: the block must stay ASCII-renderable."""
    curve = _linear_curve()
    curve["unit"] = "µg"
    curve["baseline"] = "a'b"
    return curve


def _write(obj, path):
    """Fixture JSON on disk, written by the chain's own writer."""
    densitometry.write_json(obj, path)
    return path


def _api_render(td, curve, name="fig.curve.png", quant=None, width=1600):
    """`plot_curve` into td/name → (path, w, h, geo); the size must match `identify`."""
    dens_plot = _plot()
    path = os.path.join(td, name)
    w, h = dens_plot.plot_curve(curve, path, quant=quant, width=width)
    assert os.path.isfile(path), "plot_curve 没有写出 %s" % path
    assert magick_io.identify(path) == (w, h), \
        "plot_curve 返回 %r，identify 读到 %r" % ((w, h), magick_io.identify(path))
    geo = dens_plot.curve_layout(curve, quant=quant, width=width)
    return path, w, h, geo


# ---------------------------------------------------------------- 1) parser, both names

def check_cli_parser():
    """`plot-curve` is registered under both group names with §3.4's defaults and --curve required."""
    from . import gel_photo
    ap = gel_photo.build_parser(canvas=True)
    for group in ("dens", "densitometry"):
        a = ap.parse_args([group, "plot-curve", "--curve", "fig.curve.json"])
        assert a.cmd == group and a.action == "plot-curve", (group, a.cmd, a.action)
        assert a.func.__name__ == "cmd_plot_curve", (group, a.func.__name__)
        assert a.out is None and a.quant is None and a.width == 1600, \
            (group, a.out, a.quant, a.width)
    a = ap.parse_args(["dens", "plot-curve", "--curve", "c.json", "--quant", "q.json",
                       "--out", "x.png", "--width", "1200"])
    assert (a.curve, a.quant, a.out, a.width) == ("c.json", "q.json", "x.png", 1200), a
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            ap.parse_args(["dens", "plot-curve"])
        except SystemExit as e:
            assert e.code == 2, e.code
        else:
            raise AssertionError("缺 --curve 时 argparse 该拒绝")
    assert "--curve" in err.getvalue(), err.getvalue()
    for name in ("dens", "densitometry"):
        assert name in gel_photo.CANVAS_COMMANDS, "%s 不在 CANVAS_COMMANDS" % name
    return True


# ---------------------------------------------------------------- 2) default path + --out + alias

def _success_line(stdout, out, w, h, label):
    """Exactly one `[i]` line naming the output path and the pixel size."""
    lines = stdout.strip().splitlines()
    assert len(lines) == 1, "%s: 成功路径该只有一行输出：%r" % (label, stdout)
    line = lines[0]
    assert line.startswith("[i] 写出 "), "%s: 不是 [i] 写出 开头的行：%r" % (label, line)
    rest = line[len("[i] 写出 "):]
    for mark in ("（", "("):
        if mark in rest:
            rest = rest.split(mark)[0]
    named = rest.strip()
    assert os.path.realpath(named) == os.path.realpath(out), \
        "%s: 成功行点名的路径是 %r，该写 %r（整行 %r）" % (label, named, out, line)
    assert ("%d" % w) in line and ("%d" % h) in line, \
        "%s: 成功行里没有像素尺寸 %dx%d：%r" % (label, w, h, line)


def check_default_path_and_alias():
    """`fig.curve.json` → `fig.curve.png`; fallback name; explicit --out; both CLI names."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        _write(curve, os.path.join(td, "fig.curve.json"))
        with _chdir(td):
            rc, stdout, stderr = _cli(["dens", "plot-curve", "--curve", "fig.curve.json"])
            assert rc == 0 and stderr == "", (rc, stderr)
            png = os.path.join(td, "fig.curve.png")
            assert os.path.isfile(png), "默认输出不是 fig.curve.png"
            assert not os.path.exists(os.path.join(td, "fig.curve.curve.png")), \
                "默认名叠出了 .curve.curve.png"
            _success_line(stdout, png, *_size(png), label="默认路径")
            _no_pixels(png, "默认渲染")

            # the long group name renders the very same picture (same renderer, same data)
            alias = os.path.join(td, "alias.png")
            rc, stdout, stderr = _cli(["densitometry", "plot-curve", "--curve",
                                       "fig.curve.json", "--out", "alias.png"])
            assert rc == 0 and stderr == "", (rc, stderr)
            assert _diff_pixels(png, alias) == 0, "两个组名渲染出不同的画面"

            # a renamed curve.json falls back to <stem>.curve.png (not <stem>.curve.curve.png)
            renamed = os.path.join(td, "mycurve.json")
            with open(os.path.join(td, "fig.curve.json"), encoding="utf-8") as src, \
                    open(renamed, "w", encoding="utf-8") as dst:
                dst.write(src.read())
            rc, stdout, stderr = _cli(["dens", "plot-curve", "--curve", "mycurve.json"])
            assert rc == 0 and stderr == "", (rc, stderr)
            fallback = os.path.join(td, "mycurve.curve.png")
            assert os.path.isfile(fallback), "退落名不是 mycurve.curve.png"
            assert not os.path.exists(os.path.join(td, "mycurve.curve.curve.png")), \
                "退落名叠重复了"
            _success_line(stdout, fallback, *_size(fallback), label="退落名")
    return True


def _size(path):
    return magick_io.identify(path)


# ---------------------------------------------------------------- 3) size, blank, rerun

def check_size_blank_and_rerun():
    """1600x1200 by default, 1200x900 at --width 1200, never blank, and a rerun is identical."""
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        png, w, h, _geo = _api_render(td, curve)
        assert (w, h) == (1600, 1200), "默认画布该是 1600x1200，实际 %r" % ((w, h),)
        assert h == int(round(0.75 * w)), (w, h)
        assert _bytes(png)[:8] == b"\x89PNG\r\n\x1a\n", "输出不是 PNG"
        thumb = _Pic(png, scaled=True)
        fraction = _non_white_fraction(thumb)
        assert fraction >= 0.02, "图几乎全白（非白比例 %.4f）" % fraction
        assert fraction <= 0.80, "图像异常地黑（非白比例 %.4f）" % fraction

        narrow, w2, h2, _g2 = _api_render(td, curve, name="narrow.png", width=1200)
        assert (w2, h2) == (1200, 900), "--width 1200 该是 1200x900，实际 %r" % ((w2, h2),)
        assert _non_white_fraction(_Pic(narrow, scaled=True)) >= 0.02, "窄图是空白"

        # the same width through the CLI, plus --width on a fresh canvas (ticket AC)
        curve_path = _write(curve, os.path.join(td, "fig.curve.json"))
        rc, stdout, stderr = _cli(["dens", "plot-curve", "--curve", curve_path,
                                   "--out", os.path.join(td, "cli-narrow.png"),
                                   "--width", "900"])
        assert rc == 0 and stderr == "", (rc, stderr)
        cli_png = os.path.join(td, "cli-narrow.png")
        assert _size(cli_png) == (900, 675), "CLI --width 900 该是 900x675，实际 %r" % (
            _size(cli_png),)
        _success_line(stdout, cli_png, 900, 675, label="CLI --width")

        again = os.path.join(td, "again.png")
        plot = _plot()
        w3, h3 = plot.plot_curve(curve, again)
        assert _diff_pixels(png, again) == 0, "同一输入两次渲染的画面不一致"
        assert (w3, h3) == (1600, 1200), (w3, h3)
    return True


# ---------------------------------------------------------------- 4) fitted line, linear

def _fit_line_body(curve, label, volumes):
    """The fitted line follows the fixture's own model, is solid across the range and its
    dashed ±10 % tails end exactly where the frame allows."""
    colours = _colours()
    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, geo = _api_render(td, curve, name="line.png")
        pic = _Pic(png)
        box = geo["main_box"]
        fitted = geo["fitted"]
        vmin, vmax = fitted["volume_min"], fitted["volume_max"]
        span = vmax - vmin
        assert span > 0, "%s: 标曲范围是空的" % label

        # (1) inside the range the line is SOLID: every sampled column of the model has ink at
        # the model's row — a dash phase that skips a column is a rendering bug, not art.
        lo, hi = geo["x_range"]
        px_per_unit = (box[2] - box[0]) / (hi - lo)
        checked = 0
        for frac in volumes:
            volume = vmin + frac * span
            x = _x(geo, volume)
            row = _y(geo, _fit_value(curve, volume))
            assert box[0] + 2 < x < box[2] - 2, "%s: 取样的列超出主轴：%.1f" % (label, x)
            assert _has(pic, colours["line"], (x - 2, row - 3, x + 2, row + 3), tol=8), \
                "%s: v=%.2f 的拟合线该在 y=%.1f 附近，那里没有线色" % (label, volume, row)
            # the renderer's accessor must agree with the published frame (no hidden flip)
            assert abs(geo["axes"].x(volume) - x) <= 2.0, \
                "%s: axes.x(%.2f)=%.1f，按公开的框算出 %.1f" % (label, volume,
                                                                geo["axes"].x(volume), x)
            assert abs(geo["axes"].y(_fit_value(curve, volume)) - row) <= 2.0, \
                "%s: axes.y 与主轴框不一致" % label
            checked += 1
        assert checked == len(volumes)

        # the model must be linearly mapped into pixels: two equal volume steps have equal
        # pixel steps (this is what a log axis would break)
        v_a, v_b = vmin + 0.15 * span, vmin + 0.55 * span
        steps = (geo["axes"].x(v_b) - geo["axes"].x(v_a)) / (0.40 * span)
        assert abs(steps - px_per_unit) <= 1e-6 * px_per_unit + 1e-6, \
            "%s: 体积到像素不是线性映射（%.3f vs %.3f）" % (label, steps, px_per_unit)

        # (2) the ±10 % tails: they are drawn from the range ends out to `drawn_min`/`drawn_max`,
        # and that end is either the frame edge (the frame is narrower than the extension, so the
        # line is clipped there) or exactly the extension end (the frame is wider).
        for value, sign, what in ((fitted["drawn_min"], -1, "左端"),
                                  (fitted["drawn_max"], +1, "右端")):
            x_end = geo["axes"].x(value)
            row = _y(geo, _fit_value(curve, value))
            assert box[0] <= x_end <= box[2], \
                "%s: %s的绘制末端 x=%.1f 超出主轴框 %r" % (label, what, x_end, box)
            # the renderer clamps strokes one pixel inside the frame, so the ink sits at
            # max(box[0]+1, x_end) … min(box[2]-1, x_end) — probe there, not past the frame.
            probe = min(max(x_end, box[0] + 1.0), box[2] - 1.0)
            assert _has(pic, colours["line"], (probe - 3, row - 4, probe + 1, row + 4), tol=8), \
                "%s: %s（v=%.2f）没有画到线的末端 x=%.1f y=%.1f" % (label, what, value, probe, row)
            # beyond the end: nothing (a tail overshooting its recorded end would show here)
            beyond_x0 = min(probe + 4.0, box[2])
            beyond_x1 = box[2]
            if sign < 0:
                beyond_x0, beyond_x1 = box[0], max(box[0], probe - 4.0)
            if beyond_x1 - beyond_x0 >= 4.0:
                assert not _has(pic, colours["line"],
                                (beyond_x0, row - 5, beyond_x1, row + 5), tol=8), \
                    "%s: %s越过了绘制末端（x=%.1f 之外还有线色）" % (label, what, probe)

        # (3) the dashed tail really spans its whole length: a dash pattern leaves gaps, so
        # sample many columns and require a dash-like fraction (a tail clipped short, or one
        # that stops a dash early, drops far below the ~50 % of an on/off pattern).
        for tail_lo, tail_hi in ((fitted["drawn_min"], vmin), (vmax, fitted["drawn_max"])):
            length = abs(geo["axes"].x(tail_hi) - geo["axes"].x(tail_lo))
            if length < 10.0:
                continue                      # a tail shorter than one dash: nothing to sample
            hits = total = 0
            for k in range(1, 25):
                volume = tail_lo + (tail_hi - tail_lo) * (k / 25.0)
                x = _x(geo, volume)
                row = _y(geo, _fit_value(curve, volume))
                inside = (box[0] + 2.0, row, box[2] - 2.0, row)
                total += 1
                if _has(pic, colours["line"],
                        (min(max(x - 1.5, inside[0]), inside[2] - 1.0), row - 3,
                         min(max(x + 1.5, inside[0] + 1.0), inside[2]), row + 3), tol=8):
                    hits += 1
            assert total and hits / float(total) >= 0.30, \
                "%s: 外延虚线只覆盖 %.1f%% 的取样列（虚线段被截短了？）" % (
                    label, 100.0 * hits / float(total))

        assert fitted["extension_min"] < vmin and fitted["extension_max"] > vmax, \
            "%s: fitted 没有记录 ±10 %% 外延：%r" % (label, fitted)
        assert abs((vmin - fitted["extension_min"]) - 0.10 * span) <= 0.02 * span, \
            "%s: 下端外延不是 10 %%：%r" % (label, fitted)
        assert abs((fitted["extension_max"] - vmax) - 0.10 * span) <= 0.02 * span, \
            "%s: 上端外延不是 10 %%：%r" % (label, fitted)
        # the drawn ends are the extension ends, clipped into the frame
        assert fitted["drawn_min"] <= vmin and fitted["drawn_max"] >= vmax, \
            "%s: drawn 端点没有覆盖标曲范围：%r" % (label, fitted)
        if geo["x_range"][1] >= fitted["extension_max"] + 1e-9:
            assert abs(geo["axes"].x(fitted["drawn_max"]) - box[2]) <= 2.0 or \
                abs(fitted["drawn_max"] - fitted["extension_max"]) <= 1e-9, \
                "%s: 画框装得下外延，但线的末端既不在框边也不在外延端：%r" % (label, fitted)
        else:
            assert fitted["drawn_max"] <= geo["x_range"][1] + 0.10 * span, \
                "%s: 记录的外延末端远在画框之外：%r > %r" % (label, fitted["drawn_max"],
                                                            geo["x_range"][1])
    return True


def check_fit_line_linear():
    """Linear fit: the drawn line is `intercept + slope*volume` inside and past the range."""
    return _fit_line_body(_linear_curve(), "linear", (0.18, 0.5, 0.82))


def check_fit_line_power():
    """Power fit: the drawn line is `scale*volume**exponent`, still linear in x/y pixels."""
    return _fit_line_body(_power_curve(), "power", (0.2, 0.5, 0.8))


def check_fit_line_wide_frame():
    """When the frame is wider than the +10 % extension (a far-right dropped point), the dashed
    tail stops at its extension end instead of running to the frame edge."""
    colours = _colours()
    curve = _wide_frame_curve()
    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, geo = _api_render(td, curve, name="wide.png")
        pic = _Pic(png)
        fitted = geo["fitted"]
        box = geo["main_box"]
        reached = geo["x_range"][1]
        assert reached > fitted["extension_max"], \
            "fixture 没有让画框超出外延：%r vs %r" % (reached, fitted["extension_max"])
        x_end = geo["axes"].x(fitted["drawn_max"])
        row = _y(geo, _fit_value(curve, fitted["drawn_max"]))
        assert x_end < box[2] - 20.0, \
            "画框更宽时线的末端该停在外延，实际 x=%.1f（框边 %.1f）" % (x_end, box[2])
        assert _has(pic, colours["line"], (x_end - 4, row - 4, x_end + 1, row + 4), tol=8), \
            "宽画框下线的末端（x=%.1f）没有墨" % x_end
        # nothing between the recorded end and the frame edge on that row
        assert not _has(pic, colours["line"],
                        (x_end + 6, row - 5, box[2] - 2, row + 5), tol=8), \
            "宽画框下虚线越过了 +10 %% 外延"
        # and the standard points are still where they belong
        for point in curve["points"]:
            if not point["used"]:
                continue
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            assert _has(pic, colours["point"], (x - 2, y - 2, x + 2, y + 2), tol=12), \
                "宽画框下标准点 %g 丢了" % point["mass"]
    return True


# ---------------------------------------------------------------- 5) points and dropped points

def check_points_and_dropped():
    """Used points are filled circles where the data says; a dropped point is a hollow ring
    labelled `dropped` and has no fill."""
    colours = _colours()
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, geo = _api_render(td, curve, name="points.png")
        pic = _Pic(png)
        for point in curve["points"]:
            if not point["used"]:
                continue
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            assert _has(pic, colours["point"], (x - 2, y - 2, x + 2, y + 2), tol=12), \
                "标准点 %g (v=%.2f) 的中心不是实心点颜色（x=%.1f y=%.1f）" % (
                    point["mass"], point["volume"], x, y)
            assert not _white(pic, x, y), "标准点 %g 的中心是白的" % point["mass"]

        dropped = [p for p in curve["points"] if not p["used"]]
        assert dropped, "fixture 该有一个弃用点"
        for point in dropped:
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            assert _white(pic, x, y), "弃用点 %g 不是空心（中心不是白）" % point["mass"]
            assert not _has(pic, colours["point"], (x - 3, y - 3, x + 3, y + 3), tol=12), \
                "弃用点 %g 的中心有实心颜色" % point["mass"]
            assert _has(pic, colours["drop"], (x - 9, y - 9, x + 9, y + 9), tol=14), \
                "弃用点 %g 没有空心环" % point["mass"]
            # the word `dropped` is drawn beside it, in the dropped colour (no OCR: count ink)
            assert _count(pic, colours["drop"], (x - 90, y - 26, x + 90, y + 26),
                          tol=14, limit=4) >= 4, "弃用点 %g 旁边没有 `dropped` 标签" % point["mass"]
            # and it must differ from the filled points' centres
            used = [p for p in curve["points"] if p["used"]][0]
            assert pic.px(x, y) != pic.px(_x(geo, used["volume"]), _y(geo, used["mass"]))
    return True


# ---------------------------------------------------------------- 6) replicate error bars

def check_error_bars():
    """A repeat point (n>1, sd) carries a vertical bar whose ends reach the sd in MASS units and
    whose caps straddle the stem; n=1 points carry no bar.

    NOTE (contract conflict, documented in the ticket): `curve.points[].sd` is the spread of the
    replicate AREAS (gray*cm), while §4.7 / this ticket ask for y-error bars, i.e. a spread in
    MASS.  The renderer takes the literal §4.7 reading (sd on the vertical mass axis); this check
    therefore verifies the literal vertical form ONLY and does not bless it as the correct
    science.  The conflict is reported separately to the lead.
    """
    dens_plot = _plot()
    axis = str(getattr(dens_plot, "ERROR_BAR_AXIS", "y"))
    assert axis == "y", \
        "§4.7 与工单验收要求竖直（质量轴）误差棒，渲染器的 ERROR_BAR_AXIS 却是 %r；" \
        "sd 的单位与轴对不上，见 check_error_bar_axis_conflict 与 ERROR_BAR_CONFLICT" % (axis,)
    colours = _colours()
    curve = _linear_curve()
    reps = [p for p in curve["points"] if p["used"] and p["n"] and p["n"] > 1]
    assert reps, "fixture 该有一个重复点（n=2）"
    single = [p for p in curve["points"] if p["used"] and not (p["n"] and p["n"] > 1)]
    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, geo = _api_render(td, curve, name="bars.png")
        pic = _Pic(png)
        box = geo["main_box"]
        radius = 6.0                        # POINT_R: the bar must be visible outside it
        for point in reps:
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            half = abs(point["sd"]) / geo["y_range"][1] * (box[3] - box[1])
            assert half >= radius + 3.0, \
                "fixture 的误差棒只有 %.1f px，看不出（半径 %.0f）" % (half, radius)
            for sign in (-1, +1):
                rows = (y + sign * (radius + 2), y + sign * max(radius + 2, half - 1))
                assert _has(pic, colours["err"], (x - 2, min(rows), x + 2, max(rows)), tol=8), \
                    "重复点 %g 在 %s 方向没有伸出点的误差棒" % (
                        point["mass"], "上" if sign < 0 else "下")
                # the bar ENDS at sd: a cap (a short horizontal stroke) sits on the sd row, and
                # nothing of the bar colour is found past it
                cap_y = y - sign * half
                # the cap is the short horizontal stroke at the sd row: look OUTSIDE the stem
                # (|dx| >= 2) so the stem itself cannot satisfy this check
                for side in (-1, +1):
                    assert _has(pic, colours["err"],
                                (x + side * 6.0, cap_y - 1, x + side * 2.0, cap_y + 1), tol=8), \
                        "重复点 %g 的误差棒在 sd 处没有端帽（y=%.1f, 侧 %d）" % (
                            point["mass"], cap_y, side)
                past = y - sign * (half + 4.0)
                assert not _has(pic, colours["err"], (x - 2, min(past, past), x + 2, past), tol=8), \
                    "重复点 %g 的误差棒伸过了 sd（y=%.1f 还有棒色）" % (point["mass"], past)
        for point in single:
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            half = 12.0                     # a bar would be obvious just outside the radius
            for sign in (-1, +1):
                assert not _has(pic, colours["err"],
                                (x - 1, y + sign * (radius + 2), x + 1,
                                 y + sign * (radius + half)), tol=8), \
                    "n=1 的点 %g 也画了误差棒" % point["mass"]
    return True


# ---------------------------------------------------------------- 6b) the sd-axis conflict

def _probe(x, y, distance, along, thickness=2):
    """A thin probe box `distance` px from (x, y), centred on both sides along `along`."""
    boxes = []
    for sign in (-1, +1):
        if along == "y":
            boxes.append((x - thickness, y + sign * (distance - 1), x + thickness,
                          y + sign * (distance + 1)))
        else:
            boxes.append((x + sign * (distance - 1), y - thickness,
                          x + sign * (distance + 1), y + thickness))
    return boxes


def _span(pic, colour, box):
    """How far `colour` reaches across `box` on its long side (a cap's width)."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    reached = 0
    if x1 - x0 >= y1 - y0:                       # measure the horizontal extent
        for xx in range(x0, x1 + 1):
            if any(_match(pic.rgb, (yy * pic.w + xx) * 3, colour, 8)
                   for yy in range(y0, y1 + 1)):
                reached += 1
    else:
        for yy in range(y0, y1 + 1):
            if any(_match(pic.rgb, (yy * pic.w + xx) * 3, colour, 8)
                   for xx in range(x0, x1 + 1)):
                reached += 1
    return reached


def check_error_bar_axis_conflict():
    """The sd bar is drawn on ONE axis, consistently with that axis' own scale.

    This is the separate, deliberately lenient record of the conflict described in
    `ERROR_BAR_CONFLICT`: whichever axis the renderer names in `ERROR_BAR_AXIS`, the bar must be
    a bar on THAT axis — ink from the point's rim out to the sd, a cap across the sd, and no ink
    at the other axis' reading of the same sd.  Passing does not mean the science is settled.
    """
    dens_plot = _plot()
    axis = str(getattr(dens_plot, "ERROR_BAR_AXIS", ""))
    assert axis in ("x", "y"), \
        "dens_plot.ERROR_BAR_AXIS 该是 'x' 或 'y'，实际 %r（套件不知道 sd 画在哪根轴上）" % (axis,)
    other_axis = "x" if axis == "y" else "y"
    colours = _colours()
    curve = _linear_curve()
    reps = [p for p in curve["points"] if p["used"] and p["n"] and p["n"] > 1]
    assert reps, "fixture 该有一个重复点（n=2）"
    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, geo = _api_render(td, curve, name="axis.png")
        pic = _Pic(png)
        box, radius = geo["main_box"], 6.0
        for point in reps:
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            sd = abs(float(point["sd"]))
            half = {"x": sd / geo["x_range"][1] * (box[2] - box[0]),
                    "y": sd / geo["y_range"][1] * (box[3] - box[1])}
            assert abs(half["x"] - half["y"]) > 20.0, \
                "两种读法的棒长只差 %.2f px（x %.2f / y %.2f），这个 fixture 分辨不出用了哪根轴" % (
                    abs(half["x"] - half["y"]), half["x"], half["y"])
            own, other = half[axis], half[other_axis]
            assert own >= radius + 4.0, "sd 在 %s 轴上只有 %.1f px，看不出" % (axis, own)

            # the bar runs from just outside the rim out to the sd, on the named axis
            for sign in (-1, +1):
                if axis == "y":
                    span = (x - 2, min(y + sign * (radius + 2), y + sign * (own - 1)),
                            x + 2, max(y + sign * (radius + 2), y + sign * (own - 1)))
                else:
                    span = (min(x + sign * (radius + 2), x + sign * (own - 1)), y - 2,
                            max(x + sign * (radius + 2), x + sign * (own - 1)), y + 2)
                assert _has(pic, colours["err"], span, tol=8), \
                    "ERROR_BAR_AXIS=%s，但 %s 方向半径外到 sd（%.1f px）之间没有棒色" % (
                        axis, "上下" if axis == "y" else "左右", own)

            # the cap: a stroke wider than the stem, sitting exactly on the sd
            for sign in (-1, +1):
                if axis == "y":
                    cap = (x - 9, y + sign * own - 1, x + 9, y + sign * own + 1)
                else:
                    cap = (x + sign * own - 1, y - 9, x + sign * own + 1, y + 9)
                assert _span(pic, colours["err"], cap) >= 7, \
                    "误差棒的端帽不压在 sd 上（%s=%.1f 处横跨只有 %d px）" % (
                        "y" if axis == "y" else "x", own, _span(pic, colours["err"], cap))

            # and NOTHING at the other axis' reading of the same sd: drawing both readings at
            # once is the one thing this check must refuse
            for probe in _probe(x, y, abs(other), other_axis):
                assert not _has(pic, colours["err"], probe, tol=8), \
                    "ERROR_BAR_AXIS=%s，但另一根轴读数 %.1f px 处也有棒色（两种读法混画）" % (
                        axis, other)
    return True


# ---------------------------------------------------------------- 7) residual strip

def check_residuals():
    """Residual strip: one marker per used point at (volume, rel_pct), a zero line, ±10 % guides,
    a range of at least ±15 % and 1.2× the largest residual, and no marker for a dropped point."""
    colours = _colours()
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, geo = _api_render(td, curve, name="resid.png")
        pic = _Pic(png)
        rbox = geo["resid_box"]
        main = geo["main_box"]
        assert rbox[1] > main[3], "残差带不在主轴下方（%r vs %r）" % (rbox, main)
        assert abs(rbox[0] - main[0]) < 0.5 and abs(rbox[2] - main[2]) < 0.5, \
            "残差带与主轴的 x 范围不共享：%r vs %r" % (rbox, main)

        rels = [r["rel_pct"] for r in curve["residuals"]]
        hi = geo["rel_range"][1]
        assert hi >= 15.0, "残差带的量程该至少 ±15 %%，实际 %.2f" % hi
        assert hi >= 1.2 * max(abs(r) for r in rels), \
            "残差带量程 %.2f 装不下 1.2 倍的最大残差 %.2f" % (hi, max(abs(r) for r in rels))
        assert abs(geo["rel_range"][0] + hi) <= 1e-6, "残差带量程不是对称的 ±: %r" % (
            geo["rel_range"],)

        for res in curve["residuals"]:
            x = _x(geo, res["volume"])
            row = _yr(geo, res["rel_pct"])
            assert _has(pic, colours["line"], (x - 3, row - 3, x + 3, row + 3), tol=12), \
                "体积 %.2f 的残差 %.2f%% 该画在 y=%.1f 附近" % (res["volume"], res["rel_pct"], row)
            assert abs(geo["axes"].yr(res["rel_pct"]) - row) <= 2.0, \
                "axes.yr(%.2f) 与残差带框不一致" % res["rel_pct"]

        x0, y0, x1, y1 = _window(rbox, pic.w, pic.h)
        hits = 0
        for y in range(y0, y1 + 1, 2):
            base = y * pic.w
            for x in range(x0, x1 + 1, 4):
                i = (base + x) * 3
                if (abs(pic.rgb[i] - 255) > 6 or abs(pic.rgb[i + 1] - 255) > 6
                        or abs(pic.rgb[i + 2] - 255) > 6):
                    hits += 1
        assert hits >= 40, "残差带几乎是空的（%d 个非白采样点）" % hits
        assert _ink(pic, (rbox[0] - 1, rbox[1] - 1, rbox[0] + 1, rbox[3] + 1)), \
            "残差带的左边框没画出来"

        zero = _yr(geo, 0.0)
        assert _row_has(pic, colours["zero"], zero, rbox[0] + 2, rbox[2] - 2, tol=8, least=8), \
            "残差带没有贯穿的零线（y≈%.1f）" % zero
        for value in (-10.0, 10.0):
            row = _yr(geo, value)
            assert _row_has(pic, colours["guide"], row, rbox[0] + 2, rbox[2] - 2, tol=8,
                            least=8, samples=80), \
                "残差带没有 %+g%% 的虚线导引线（y≈%.1f）" % (value, row)

        dropped = [p for p in curve["points"] if not p["used"]][0]
        x = _x(geo, dropped["volume"])
        assert not _has(pic, colours["line"], (x - 2, rbox[1] + 2, x + 2, rbox[3] - 2), tol=6), \
            "弃用点在残差带里也画了标记"

        # with a normal strip each guide carries its own `+10%` / `-10%` label to the left of the
        # strip (text is muted grey, so count ink in the label band rather than a colour)
        assert geo["guide_note"] is None, \
            "正常量程不该用合并的导引说明标签：%r" % (geo["guide_note"],)
        for value in (-10.0, 10.0):
            row = _yr(geo, value)
            band = (rbox[0] - 46, row - 7, rbox[0] - 20, row + 7)
            assert _ink_total(pic, band, dark=205, spread=14) >= 40, \
                "残差带 %+g%% 的导引线旁没有自己的标签（墨迹太少）" % value

        # the same curve with a BSA-like residual (-174 %): the strip scales to ±208 %, the guides
        # sit ~3 px from the zero line, so the labels collapse into ONE note in the gap above the
        # strip (`guide_note` / `guide_note_at`), which must actually be drawn.
        big = _big_residual_curve()
        big_png, _bw, _bh, big_geo = _api_render(td, big, name="resid-big.png")
        big_pic = _Pic(big_png)
        big_rel = big_geo["rel_range"]
        assert big_rel[1] >= 1.2 * 174.0, "大病残差的量程没放大：%r" % (big_rel,)
        assert big_geo["guide_note"], "导引线挤在一起时该给出合并说明：%r" % (big_geo["guide_note"],)
        note = big_geo["guide_note"]
        assert "10%" in note and "dashed" in note, \
            "合并说明该点名 10%% 与虚线：%r" % (note,)
        # it must be ONE note, not the two per-guide labels pasted together
        assert note.strip() not in ("+10%", "-10%", "+10% -10%", "-10%+10%"), \
            "合并说明退化成了逐条标签：%r" % (note,)
        assert all(32 <= ord(ch) <= 126 for ch in note), "合并说明不是 ASCII：%r" % (note,)
        note_at = big_geo["guide_note_at"]
        assert note_at is not None and len(note_at) == 3, "guide_note_at 形状不对：%r" % (note_at,)
        nx, ny, align = note_at
        assert big_geo["resid_box"][0] <= nx <= big_geo["resid_box"][2], \
            "说明文字的锚点不在残差带宽度内：%r" % (note_at,)
        assert big_geo["main_box"][3] <= ny <= big_geo["resid_box"][1], \
            "说明文字没有画在主轴与残差带之间：%r" % (note_at,)
        note_box = (nx - 170, ny - 24, nx + 2, ny + 2)
        assert _ink_rows(big_pic, note_box, dark=205, spread=20, least=3) >= 5, \
            "合并说明没画出来"
        assert _ink_total(big_pic, note_box, dark=205, spread=20) >= 200, \
            "合并说明的墨迹太少（%d）" % _ink_total(big_pic, note_box, dark=205, spread=20)
        # and the per-guide labels are gone
        for value in (-10.0, 10.0):
            row = big_geo["axes"].yr(value)
            band = (big_geo["resid_box"][0] - 46, row - 7, big_geo["resid_box"][0] - 20,
                    row + 7)
            assert _ink_total(big_pic, band, dark=205, spread=14) == 0, \
                "挤在一起时不该再画逐条 %+g%% 标签" % value
        # the rules themselves stay exactly where they belong
        for value in (-10.0, 10.0, 0.0):
            row = big_geo["axes"].yr(value)
            colour = colours["zero"] if value == 0.0 else colours["guide"]
            assert _row_has(big_pic, colour, row, big_geo["resid_box"][0] + 2,
                            big_geo["resid_box"][2] - 2, tol=8, least=8, samples=80), \
                "大病残差下 %+g%% 的规则线不见了" % value
    return True


# ---------------------------------------------------------------- 8) annotation block

def _annotation_body(curve, label):
    """One curve's annotation block: the fields, the ASCII rule, the drawn text and the equation."""
    dens_plot = _plot()
    geo = dens_plot.curve_layout(curve, width=1600)
    text = geo["annotation"]
    assert isinstance(text, str) and text, "curve_layout 没有公布 annotation 文本"
    fp = curve["fit_params"]
    used = sum(1 for p in curve["points"] if p["used"])
    dropped = sum(1 for p in curve["points"] if not p["used"])
    assert "fit: %s" % curve["fit"] in text, "%s: 注释块没有 fit 种类：%r" % (label, text)
    if fp.get("r2") is None:
        assert "r2: -" in text, "%s: r2 缺失时该写 -：%r" % (label, text)
    else:
        assert "r2: %.4f" % float(fp["r2"]) in text, \
            "%s: 注释块的 r2 该是 4 dp 的 %.4f：%r" % (label, float(fp["r2"]), text)
    assert "n: %d point" % used in text, \
        "%s: 注释块没有点数 n=%d：%r" % (label, used, text)
    if used == 1:
        assert "n: 1 points" not in text, "%s: 单点数该用单数 point：%r" % (label, text)
    if dropped:
        assert "%d dropped" % dropped in text, "%s: 注释块没写弃用数：%r" % (label, text)
    else:
        assert " dropped" not in text, "%s: 没有弃用点却写了 dropped：%r" % (label, text)
    unit = curve["unit"]
    baseline = curve["baseline"]
    if all(32 <= ord(ch) <= 126 for ch in unit):
        assert "unit: %s" % unit in text, "%s: 注释块没有 unit：%r" % (label, text)
    if all(32 <= ord(ch) <= 126 for ch in baseline) and '"' not in baseline \
            and "'" not in baseline:
        assert "baseline: %s" % baseline in text, \
            "%s: 注释块没有 baseline：%r" % (label, text)
    assert "None" not in text, "%s: 注释块里出现了 None：%r" % (label, text)
    assert all(ch == "\n" or 32 <= ord(ch) <= 126 for ch in text), \
        "%s: 注释块不是 ASCII：%r" % (label, text)
    assert "mass = " in text, "%s: 注释块没有方程：%r" % (label, text)
    rng = curve.get("range") or {}
    if "mass_min" in rng and "mass_max" in rng:
        lo, hi = float(rng["mass_min"]), float(rng["mass_max"])
        if lo == hi:
            assert "standard range: %s" % _three(lo) in text, \
                "%s: 单值标曲范围该只写一个数：%r" % (label, text)
        else:
            assert "standard range: %s - %s" % (_three(lo), _three(hi)) in text, \
                "%s: 注释块没写标曲范围 %s - %s：%r" % (label, _three(lo), _three(hi), text)
    # the equation is the fit's own formula, in the 2-dp trimmed form
    if curve["fit"] == "linear":
        assert "*area" in text, "%s: 线性方程不用 *area：%r" % (label, text)
        assert _two(fp["slope"]) in text, "%s: 方程里没有斜率 %s：%r" % (
            label, _two(fp["slope"]), text)
        assert _two(abs(fp["intercept"])) in text, "%s: 方程里没有截距 %s：%r" % (
            label, _two(abs(fp["intercept"])), text)
        assert "+" in text or "-" in text.split("mass = ", 1)[1], \
            "%s: 线性方程没有加减号：%r" % (label, text)
    else:
        assert "*area^" in text, "%s: 幂律方程不是 c*area^b：%r" % (label, text)
        assert _two(fp["scale"]) in text and _two(fp["exponent"]) in text, \
            "%s: 方程里没有 scale/exponent：%r" % (label, text)

    assert geo["font"] == (text_layout.pick_font() or "Arial"), \
        "%s: 字体不是 text_layout.pick_font()：%r" % (label, geo["font"])
    for name in ("title", "legend", "x_label", "y_label", "resid_label"):
        assert geo[name], "%s: %s 是空的" % (label, name)
        assert all(32 <= ord(ch) <= 126 for ch in geo[name]), \
            "%s: %s 不是 ASCII：%r" % (label, name, geo[name])
    assert "baseline" not in geo["title"], "标题里泄露了 baseline 字段名"

    # the block sits ABOVE the main axes at the top right — it must never cover a point
    assert geo["annotation_box"][3] <= geo["main_box"][1], \
        "%s: 注释块压在主轴里（%r vs %r）" % (label, geo["annotation_box"], geo["main_box"])
    assert geo["annotation_box"][2] >= geo["main_box"][2] - 1.0, \
        "%s: 注释块不在右上角（%r vs 主轴 %r）" % (label, geo["annotation_box"],
                                                   geo["main_box"])
    assert geo["annotation_box"][0] >= 0 and geo["annotation_box"][1] >= 0, \
        "%s: 注释块跑到画布外（%r）" % (label, geo["annotation_box"])

    with tempfile.TemporaryDirectory() as td:
        png, _w, _h, rendered = _api_render(td, curve, name="ann-%s.png" % label)
        pic = _Pic(png)
        box = rendered["annotation_box"]
        # the block is FRAMED with grey, so count dark NEUTRAL text ink inside the frame:
        # a blank block keeps only the frame, a drawn block has several text rows.
        inner = (box[0] + 8, box[1] + 6, box[2] - 8, box[3] - 6)
        rows = _ink_rows(pic, inner, dark=205, spread=14, least=6)
        assert rows >= 3, "%s: 注释块的框里没有画出文字（只有 %d 行墨迹）" % (label, rows)
        score = _ink_total(pic, inner, dark=205, spread=14)
        assert score >= 150, "%s: 注释块里的文字太少（%d 个墨点）" % (label, score)

        # title / legend / axis labels: each band must carry text on its own
        main, resid = rendered["main_box"], rendered["resid_box"]
        regions = {
            "标题": ((4, 4, rendered["annotation_box"][0] - 8, 58), 6, 400),
            "图例": ((4, 60, main[0] - 8, main[1] - 8), 5, 300),
            "x 轴标签": ((resid[0] + 8, resid[3] + 38, resid[2] - 8, pic.h - 2), 6, 0),
            "y 轴标签": ((2, main[1] + 40, main[0] - 66, main[3] - 40), 6, 0),
            "残差标签": ((2, resid[1] + 4, resid[0] - 70, resid[3] - 4), 5, 0),
        }
        for what, (region, least_rows, least_ink) in regions.items():
            rows = _ink_rows(pic, region, dark=205, spread=14, least=3)
            assert rows >= least_rows, "%s: %s 没有画出来（只有 %d 行墨迹）" % (
                label, what, rows)
            if least_ink:
                ink = _ink_total(pic, region, dark=205, spread=14)
                assert ink >= least_ink, "%s: %s 的墨迹太少（%d 个墨点）" % (label, what, ink)
    return True


def check_annotation():
    """Annotation block: the fields, ASCII, the equation, the font, and it is really drawn."""
    for curve, label in ((_linear_curve(), "linear"), (_power_curve(), "power")):
        _annotation_body(curve, label)
    # a single point: singular "1 point", unknown r2 and a one-number range
    _annotation_body(_single_point_curve(), "单点")
    # user-supplied non-ASCII text must be sanitised, never break the MVG text primitive
    _annotation_body(_nonascii_curve(), "非 ASCII")
    return True


# ---------------------------------------------------------------- 9) geometry frame

def check_geometry_and_frames():
    """The published frame is a 3:4 canvas, the strip sits under shared axes, and every
    value→pixel mapping agrees with the arithmetic re-derived here."""
    dens_plot = _plot()
    for curve, label in ((_linear_curve(), "linear"), (_power_curve(), "power")):
        for width in (600, 700, 900, 1000, 1200, 1600, 2400):
            geo = dens_plot.curve_layout(curve, width=width)
            assert geo["width"] == width and geo["height"] == int(round(0.75 * width)), \
                "%s: 画布尺寸 %r 在 width=%d 下不对" % (label, (geo["width"], geo["height"]), width)
            main, resid = geo["main_box"], geo["resid_box"]
            assert resid[1] > main[3] and abs(resid[0] - main[0]) < 0.5 \
                and abs(resid[2] - main[2]) < 0.5, "%s: 残差带/主轴框 %r %r" % (label, main, resid)
            assert geo["x_range"][0] == 0.0 and geo["rel_range"][0] < 0.0, \
                "%s: 范围该从 0 开始、残差范围含负：%r %r" % (label, geo["x_range"],
                                                              geo["rel_range"])
            vmin, vmax = geo["fitted"]["volume_min"], geo["fitted"]["volume_max"]
            xhi = geo["x_range"][1]
            # §4.7's x range (1.08 x the largest volume) can end before the +10 % extension when
            # the standard volumes span a wide ratio: the line is then clipped to the frame.  What
            # must hold is that the frame covers the data and starts at 0.
            assert xhi >= max(p["volume"] for p in curve["points"]), \
                "%s: x 轴装不下标准点：%r" % (label, xhi)
            assert xhi >= geo["fitted"]["volume_max"], \
                "%s: x 轴装不下标曲上限：%r > %r" % (label, geo["fitted"]["volume_max"], xhi)
            for volume in (0.0, 0.5 * vmax, vmax, xhi):
                assert abs(geo["axes"].x(volume) - _x(geo, volume)) <= 2.0, \
                    "%s: axes.x(%.2f) 与框算值不一致" % (label, volume)
            for mass in (0.0, geo["y_range"][1] / 2.0, geo["y_range"][1]):
                assert abs(geo["axes"].y(mass) - _y(geo, mass)) <= 2.0, \
                    "%s: axes.y(%.2f) 与框算值不一致" % (label, mass)
            for rel in (geo["rel_range"][0], 0.0, geo["rel_range"][1]):
                assert abs(geo["axes"].yr(rel) - _yr(geo, rel)) <= 2.0, \
                    "%s: axes.yr(%.2f) 与框算值不一致" % (label, rel)
            assert main[3] >= main[1] and main[2] >= main[0], "%s: 主轴框反了 %r" % (label, main)
            # the annotation block stays above the axes at EVERY width, and on a narrow canvas
            # it moves below the title/legend rather than over the data
            ann = geo["annotation_box"]
            assert ann[3] <= main[1], "%s@%d: 注释块压在主轴里（%r vs %r）" % (
                label, width, ann, main)
            assert ann[0] >= 0 and ann[2] <= width, "%s@%d: 注释块出了画布（%r）" % (
                label, width, ann)
            if width <= 700:
                assert ann[1] >= 58.0, "%s@%d: 窄画布上注释块该在标题/图例下面（%r）" % (
                    label, width, ann)
    return True


# ---------------------------------------------------------------- 10) quant markers

def _label_ink(pic, colour, x, y, reach=80, dy=28, exclude=12, tol=14):
    """Count label-coloured pixels around a marker, skipping the marker's own box."""
    x0, y0, x1, y1 = _window((x - reach, y - dy, x + reach, y + dy), pic.w, pic.h)
    n = 0
    for yy in range(y0, y1 + 1):
        for xx in range(x0, x1 + 1):
            if abs(xx - x) <= exclude and abs(yy - y) <= exclude:
                continue
            if _match(pic.rgb, (yy * pic.w + xx) * 3, colour, tol):
                n += 1
                if n >= 4:
                    return n
    return n


def _ring_centre(pic, colour, box, tol=14):
    """Mean position of a hollow marker's stroke (≈ its centre) inside `box`."""
    x0, y0, x1, y1 = _window(box, pic.w, pic.h)
    xs = ys = n = 0
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            if _match(pic.rgb, (y * pic.w + x) * 3, colour, tol):
                xs += x
                ys += y
                n += 1
    return (xs / float(n), ys / float(n)) if n else None


def _x_values(geo):
    """The two ends of the main axes' x range (the frame is published as the pair itself)."""
    return float(geo["x_range"][0]), float(geo["x_range"][1])


def check_quant_markers():
    """`--quant`: one square per band at (area, mass), hollow on the x axis when out of range,
    each labelled; the axes do not move when quant is added."""
    colours = _colours()
    curve = _linear_curve()
    quant = _quant()
    with tempfile.TemporaryDirectory() as td:
        plain_png, _w, _h, plain = _api_render(td, curve, name="plain.png")
        png, _w2, _h2, geo = _api_render(td, curve, name="quant.png", quant=quant)
        pic = _Pic(png)
        assert geo["x_range"] == plain["x_range"] and geo["y_range"] == plain["y_range"], \
            "加了 quant 之后坐标轴范围变了：%r vs %r" % (geo["x_range"], plain["x_range"])
        assert _diff_pixels(plain_png, png) not in (0, None), \
            "--quant 没有改变画面（样品方块没画出来）"
        box = geo["main_box"]
        seen = 0
        for lane in quant["lanes"]:
            for band in lane["bands"]:
                seen += 1
                x = _x(geo, band["area"])
                if band["out_of_range"] or band["mass"] is None:
                    window = (x - 9, box[3] - 16, x + 9, box[3] - 1)
                    assert _has(pic, colours["oor"], window, tol=14), \
                        "泳道 %d 的超范围带没有空心方块在 x 轴上" % lane["lane"]
                    centre = _ring_centre(pic, colours["oor"], window)
                    assert centre is not None, "泳道 %d 的空心方块找不到中心" % lane["lane"]
                    assert _white(pic, centre[0], centre[1]), \
                        "泳道 %d 的超范围方块不是空心的" % lane["lane"]
                    assert _label_ink(pic, colours["oor"], x, box[3] - 8, reach=90, dy=44) >= 4, \
                        "泳道 %d 的超范围方块没有带泳道号的标签" % lane["lane"]
                else:
                    y = _y(geo, band["mass"])
                    assert box[1] < y < box[3], "样品质量 %.1f 落在主轴框外" % band["mass"]
                    assert _has(pic, colours["sample"], (x - 2, y - 2, x + 2, y + 2), tol=12), \
                        "泳道 %d 面积 %.1f 的样品方块不在 (%.1f, %.1f)" % (
                            lane["lane"], band["area"], x, y)
                    assert not _white(pic, x, y), "样品方块的中心是白的（该是实心）"
                    assert _label_ink(pic, colours["sample"], x, y) >= 4, \
                        "泳道 %d 的样品方块没有带泳道号的标签" % lane["lane"]
        assert seen == 4, "fixture 该有 4 条样品带，实际 %d" % seen
        assert len(geo["samples"]) == seen, \
            "curve_layout 没把每一条样品带都公布出来：%d" % len(geo["samples"])

        # a sample square sits at (area, mass) exactly, and the axes range covers it
        for sample in geo["samples"]:
            if sample["out"]:
                continue
            assert abs(sample["x"] - _x(geo, sample["area"])) <= 2.0, \
                "泳道 %d 的方块 x 不在 area 位置上" % sample["lane"]
            assert abs(sample["y"] - _y(geo, sample["mass"])) <= 2.0, \
                "泳道 %d 的方块 y 不在 mass 位置上" % sample["lane"]
            assert geo["main_box"][0] <= sample["x"] <= geo["main_box"][2], \
                "泳道 %d 的方块跑到框外" % sample["lane"]

        # labels must not be printed over the fitted line (a lane tag across the line reads as
        # a typo): no label-coloured ink on the line's row, outside the markers' own boxes
        volumes = _x_values(geo)
        collisions = []
        for sample in geo["samples"]:
            colour = colours["oor"] if sample["out"] else colours["sample"]
            for dx in range(-96, 97, 4):
                xx = sample["x"] + dx
                if abs(dx) <= 14 or not (box[0] + 2 <= xx <= box[2] - 2):
                    continue                  # the marker box itself and outside the frame
                volume = (xx - box[0]) / (box[2] - box[0]) * (volumes[1] - volumes[0]) \
                    + volumes[0]
                ly = _y(geo, _fit_value(curve, volume))
                for depth in range(-3, 4):
                    if _has(pic, colour, (xx, ly + depth, xx, ly + depth), tol=14):
                        collisions.append((sample["label"], int(xx), int(ly + depth)))
                        break
        assert not collisions, "泳道标签压在了拟合线上：%r" % (collisions[:4],)
    return True


# ---------------------------------------------------------------- 11) quant absent == unchanged

def check_quant_absent_identical():
    """Without `--quant` the picture is byte-for-byte what `plot_curve(curve, out)` writes."""
    curve = _linear_curve()
    quant = _quant()
    with tempfile.TemporaryDirectory() as td:
        file = _write(curve, os.path.join(td, "fig.curve.json"))
        cli_png = os.path.join(td, "cli.png")
        rc, stdout, stderr = _cli(["dens", "plot-curve", "--curve", file, "--out", cli_png])
        assert rc == 0 and stderr == "", (rc, stderr)
        _success_line(stdout, cli_png, *_size(cli_png), label="不带 --quant")
        api_png = os.path.join(td, "api.png")
        dens_plot = _plot()
        dens_plot.plot_curve(curve, api_png)
        assert _diff_pixels(cli_png, api_png) == 0, \
            "不传 quant 的 CLI 与 plot_curve(curve) 画面不一致（without it the plot is unchanged）"
        with_q = os.path.join(td, "q.png")
        rc, stdout, stderr = _cli(["dens", "plot-curve", "--curve", file, "--quant",
                                   _write(quant, os.path.join(td, "fig.quant.json")),
                                   "--out", with_q])
        assert rc == 0 and stderr == "", (rc, stderr)
        _success_line(stdout, with_q, *_size(with_q), label="带 --quant")
        assert _diff_pixels(with_q, cli_png) not in (0, None), "--quant 给了却没改变画面"
    return True


# ---------------------------------------------------------------- 12) API refusals

def check_api_errors():
    """Wrong kinds, a too-small canvas and every bad out path raise the documented error."""
    dens_plot = _plot()
    curve = _linear_curve()
    quant = _quant()
    with tempfile.TemporaryDirectory() as td:
        def expect(call, error, label):
            try:
                call()
            except error as e:
                message = str(e)
                assert "\n" not in message, "%s: 错误信息不是一行：%r" % (label, message)
                return message
            raise AssertionError("%s: 没有抛 %s" % (label, error.__name__))

        bad = dict(curve, kind="ep-craft-densitometry")
        message = expect(lambda: dens_plot.plot_curve(bad, os.path.join(td, "x.png")),
                         RuntimeError, "曲线 kind 不对")
        assert KIND in message, "kind 错误的提示没点名该有的 kind：%r" % message
        message = expect(lambda: dens_plot.plot_curve(curve, os.path.join(td, "x.png"),
                                                      quant=dict(quant, kind="ep-craft")),
                         RuntimeError, "quant kind 不对")
        assert QUANT_KIND in message, "quant kind 错误的提示没点名 kind：%r" % message
        message = expect(lambda: dens_plot.plot_curve(curve, os.path.join(td, "x.png"),
                                                      width=10), ValueError, "width=10")
        assert "600" in message and "10" in message, message
        message = expect(lambda: dens_plot.plot_curve(curve, os.path.join(td, "x.png"),
                                                      width=599), ValueError, "width=599")
        assert "600" in message and "599" in message, message
        message = expect(lambda: dens_plot.plot_curve(curve, os.path.join(td, "x.png"),
                                                      width="wide"), ValueError, "width 不是整数")
        assert "整数" in message or "width" in message, message
        # 600 is the floor and must work, not error
        png600, w600, h600, _g = _api_render(td, curve, name="w600.png", width=600)
        assert (w600, h600) == (600, 450), (w600, h600)
        assert not os.path.exists(os.path.join(td, "x.png")), \
            "失败的渲染留下了输出文件"
        message = expect(lambda: dens_plot.plot_curve(curve, os.path.join(td, "x.jpg")),
                         ValueError, "out 不是 .png")
        assert "png" in message, message
        message = expect(lambda: dens_plot.plot_curve(curve, td), ValueError, "out 是目录")
        assert ".png" in message or "目录" in message, message
        pic_dir = os.path.join(td, "dir.png")             # a directory that looks like a PNG
        os.makedirs(pic_dir)
        message = expect(lambda: dens_plot.plot_curve(curve, pic_dir), ValueError, "out 是 .png 目录")
        assert ".png" in message or "目录" in message, message
        assert not os.path.isfile(pic_dir), "把目录当成文件写了"
        message = expect(lambda: dens_plot.plot_curve(
            curve, os.path.join(td, "no-such-dir", "x.png")), ValueError, "out 的目录不存在")
        assert "目录" in message, message
        assert not os.path.exists(os.path.join(td, "no-such-dir")), "输出目录被偷偷建出来了"
    return True


# ---------------------------------------------------------------- 13) CLI refusals + overwrite

def check_cli_errors_and_overwrite():
    """Every refusal is one `[!] …` line with exit 2, and --out never overwrites an input."""
    curve = _linear_curve()
    quant = _quant()
    with tempfile.TemporaryDirectory() as td:
        curve_path = _write(curve, os.path.join(td, "fig.curve.json"))
        quant_path = _write(quant, os.path.join(td, "fig.quant.json"))
        bad_kind = _write(dict(curve, kind="ep-craft-densitometry"),
                          os.path.join(td, "bad.curve.json"))
        bad_quant = _write(dict(quant, kind="ep-craft-densitometry-curve"),
                           os.path.join(td, "bad.quant.json"))
        cases = (
            (["--curve", bad_kind], ("kind", "不是")),
            (["--curve", curve_path, "--quant", bad_quant], ("kind", "不是")),
            (["--curve", curve_path, "--width", "10"], ("width", "600")),
            (["--curve", curve_path, "--out", os.path.join(td, "x.jpg")], ("png", "PNG")),
            (["--curve", curve_path, "--out", td], ("目录", "png")),
            (["--curve", curve_path, "--out", os.path.join(td, "no", "x.png")], ("输出目录", "目录")),
            (["--curve", os.path.join(td, "nope.json")], ("找不到", "不存在")),
            (["--curve", curve_path, "--quant", os.path.join(td, "nope.json")],
             ("找不到", "不存在")),
            (["--curve", curve_path, "--out", curve_path], "--out"),
            (["--curve", curve_path, "--quant", quant_path, "--out", quant_path], "--out"),
        )
        for extra, needle in cases:
            rc, stdout, stderr = _cli(["dens", "plot-curve"] + list(extra))
            assert rc == 2, "%r: 该以 2 退出，实际 %d" % (extra, rc)
            assert stdout == "", "%r: 失败路径不该写 stdout：%r" % (extra, stdout)
            _one_line(stderr, repr(extra))
            if isinstance(needle, str):
                needle = (needle,)
            assert any(word in stderr for word in needle), \
                "%r: 错误行里没有 %r 之一：%r" % (extra, needle, stderr)
            assert "Traceback" not in stderr
        assert os.path.isfile(curve_path) and os.path.isfile(quant_path), "输入文件被删/改了"
        for path in (curve_path, quant_path):
            with open(path, encoding="utf-8") as f:
                assert json.load(f), "%s 被失败的命令写坏了" % path
    return True


# ---------------------------------------------------------------- 14) magick failure + cleanup

def check_magick_failure_and_cleanup():
    """A failing magick gives one `渲染失败（…）` line, no half PNG and no MVG debris."""
    dens_plot = _plot()
    curve = _linear_curve()
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "broken.png")
        old = magick_io.MAGICK
        magick_io.MAGICK = "magick-does-not-exist-ep-bqv4"
        try:
            try:
                dens_plot.plot_curve(curve, out)
            except RuntimeError as e:
                message = str(e)
                assert "\n" not in message, "渲染失败的信息不是单行：%r" % message
                assert message.startswith("渲染失败"), message
                assert out in message, "错误信息里没有输出路径：%r" % message
            else:
                raise AssertionError("坏 magick 却渲染成功了")
        finally:
            magick_io.MAGICK = old
        assert not os.path.exists(out), "失败后留下了半个 PNG：%s" % out
        hits = [n for n in os.listdir(td) if n.endswith(".mvg") or n.startswith(".dens-plot-")]
        assert not hits, "失败渲染后留下了临时文件：%r" % hits

        png, _w, _h, _geo = _api_render(td, curve, name="ok.png")
        _no_pixels(png, "成功渲染之后")
    return True


# ---------------------------------------------------------------- 15) real chain fixture

def check_chain_synthetic():
    """A curve built by the real chain (`_synthetic_report` → measure → build_curve → curve →
    plot-curve`) is drawn at its own numbers, including a dropped real standard lane."""
    from . import selftest_densitometry
    colours = _colours()
    lanes = [
        {"label": "S1", "bands": [(1.2, 30.0, 0.06)]},
        {"label": "S2", "bands": [(2.6, 60.0, 0.06)]},
        {"label": "S3", "bands": [(3.9, 120.0, 0.06)]},
        {"label": "S4", "bands": [(5.2, 40.0, 0.06)]},
    ]       # measured flank areas: 4.371 / 8.754 / 17.519 / 5.838 gray*cm
    with tempfile.TemporaryDirectory() as td:
        report = selftest_densitometry._synthetic_report(lanes=lanes, texture=0.0)
        report_path = os.path.join(td, "fig.mw.json")
        densitometry.write_json(report, report_path)
        with _chdir(td):
            rc, stdout, stderr = _cli(["dens", "measure", "fig.mw.json"])
            assert rc == 0 and stderr == "", (rc, stderr)
            rc, stdout, stderr = _cli(["dens", "curve", "--dens", "fig.dens.json",
                                       "--std", "1=10,2=19,3=40,4=13.5", "--drop", "4"])
            assert rc == 0 and stderr == "", (rc, stderr)
            curve_path = os.path.join(td, "fig.curve.json")
            assert os.path.isfile(curve_path), "curve 没有写出 fig.curve.json"
            curve = densitometry.load_json(curve_path, KIND)
            assert curve["source"]["drop"] == [4], curve["source"]
            rc, stdout, stderr = _cli(["dens", "plot-curve", "--curve", "fig.curve.json"])
            assert rc == 0 and stderr == "", (rc, stderr)
            png = os.path.join(td, "fig.curve.png")
            assert os.path.isfile(png)
            _success_line(stdout, png, *_size(png), label="真实链")
            _no_pixels(png, "真实链渲染")

        dens_plot = _plot()
        geo = dens_plot.curve_layout(curve, width=1600)
        pic = _Pic(png)
        used = [p for p in curve["points"] if p["used"]]
        assert len(used) == len(curve["residuals"]), \
            "真实链的残差条数与拟合点数不一致：%d vs %d" % (
                len(used), len(curve["residuals"]))
        for point in used:
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            assert _has(pic, colours["point"], (x - 2, y - 2, x + 2, y + 2), tol=12), \
                "真实链的标准点 %g 没画在 (% .1f, %.1f)" % (point["mass"], x, y)
        dropped = [p for p in curve["points"] if not p["used"]]
        assert dropped, "真实链该有一个 --drop 弃用的点"
        for point in dropped:
            x, y = _x(geo, point["volume"]), _y(geo, point["mass"])
            assert _white(pic, x, y), "真实链的弃用点 %g 不是空心" % point["mass"]
            assert _has(pic, colours["drop"], (x - 9, y - 9, x + 9, y + 9), tol=14), \
                "真实链的弃用点 %g 没有空心环" % point["mass"]
        vmin, vmax = geo["fitted"]["volume_min"], geo["fitted"]["volume_max"]
        for frac in (0.25, 0.75):
            volume = vmin + frac * (vmax - vmin)
            x, row = _x(geo, volume), _y(geo, _fit_value(curve, volume))
            assert _ink(pic, (x - 1, row - 4, x + 1, row + 4)), \
                "真实链的拟合线在 v=%.2f 没有墨" % volume
        text = geo["annotation"]
        assert "r2: %.4f" % curve["fit_params"]["r2"] in text, \
            "真实链的注释块 r2 与 curve.json 不一致：%r" % text
        assert "n: %d points" % curve["fit_params"]["n"] in text, text
    return True


# ---------------------------------------------------------------- colours distinct

def check_colour_constants():
    """The marker colours are distinct from white and from each other (so pixels can be told apart)."""
    colours = _colours()
    names = sorted(colours)
    for i, name in enumerate(names):
        assert colours[name] != WHITE, "颜色 %s 与白底相同" % name
        for other in names[i + 1:]:
            assert colours[name] != colours[other], "颜色 %s 与 %s 相同" % (name, other)
    return True


# ---------------------------------------------------------------- entry point

CHECKS = [
    check_cli_parser,
    check_default_path_and_alias,
    check_size_blank_and_rerun,
    check_fit_line_linear,
    check_fit_line_power,
    check_fit_line_wide_frame,
    check_points_and_dropped,
    check_error_bars,
    check_error_bar_axis_conflict,
    check_residuals,
    check_annotation,
    check_geometry_and_frames,
    check_quant_markers,
    check_quant_absent_identical,
    check_api_errors,
    check_cli_errors_and_overwrite,
    check_magick_failure_and_cleanup,
    check_chain_synthetic,
    check_colour_constants,
]


def run():
    """Run the whole acceptance suite (ep-bqv.4); returns the number of passed checks."""
    dens_plot = _plot()
    assert callable(getattr(dens_plot, "plot_curve", None)), \
        "dens_plot.plot_curve 不存在——ep-bqv.4（plot-curve）还没落到这个分支上"
    _colours()
    for check in CHECKS:
        check()
    print("selftest_plot_curve_acceptance %d/%d 通过" % (len(CHECKS), len(CHECKS)))
    name, template = ERROR_BAR_CONFLICT
    axis = str(getattr(dens_plot, "ERROR_BAR_AXIS", "?"))
    reading = "质量（mass）" if axis == "y" else "面积（area）"
    print("注意：已知契约冲突 %s —— %s" % (name, template % (axis, reading)))
    return len(CHECKS)


if __name__ == "__main__":
    run()
