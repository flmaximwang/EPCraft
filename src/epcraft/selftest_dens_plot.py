#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_dens_plot — acceptance checks for `dens plot-bands` (spec §3.4, §4.7, §7; ep-bqv.2).

Black-box, CLI first: every case builds a synthetic dens.json from the committed
`selftest_densitometry._synthetic_report` + `densitometry.measure`, runs the public command
through `gel_photo.main`, and reads the PNG back with ImageMagick.

Two sources of truth, deliberately separate: WHICH region must be shaded comes from the
dens.json the command was handed (its `window_flank` / `window_global` coordinates and its
lane flags), while WHERE that region lands in pixels comes from `dens_plot.band_layout`, the
renderer's own shared mapping (`Panel.x(gray)` / `Panel.y(y_cm)`) — the tests do not duplicate
layout arithmetic, and a picture that disagrees with the data still fails.

`run()` returns the number of passed checks and prints one `selftest_dens_plot N/N 通过` line.
Requires ImageMagick, like the other pixel suites (`selftest_crop`).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

from . import dens_plot
from . import densitometry
from . import magick_io
from . import selftest_densitometry
from . import text_layout


# ---------------------------------------------------------------- public CLI harness

def _run(argv):
    """Run the public CLI in-process → (rc, stdout, stderr); argparse exits count as rc 2."""
    from . import gel_photo
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = gel_photo.main(["dens"] + list(argv))
        except SystemExit as e:                     # a missing sub-parser is a usage error
            rc = e.code if isinstance(e.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _one_line(stderr, label):
    """The CLI's error boundary: exactly one `[!] …` line, no traceback (§3, selftest_cli)."""
    lines = stderr.rstrip("\n").split("\n")
    assert len(lines) == 1, "%s: stderr 不是一行：%r" % (label, stderr)
    assert lines[0].startswith("[!] "), "%s: 不是 [!] 开头的错误行：%r" % (label, lines[0])
    assert "Traceback" not in stderr, "%s: 吐了 traceback：%r" % (label, stderr)


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- pixels

def _pixels(path):
    """(w, h, rgb flat bytes) at native size — read_ppm(longest=0) does not resample."""
    return magick_io.read_ppm(path, longest=0)


def _px(img, row, x):
    """(r, g, b) at (x, row) of a `_pixels` triple."""
    w = img[0]
    k = (row * w + x) * 3
    return tuple(img[2][k:k + 3])


def _blend(base, color, alpha):
    """8-bit composite of `color` at `alpha` over `base` — what ImageMagick writes."""
    return tuple(int(round(base[i] * (1.0 - alpha) + color[i] * alpha)) for i in range(3))


def _fill_family(px):
    """The hue family of a translucent fill pixel: 'orange', 'blue', 'blend' or None.

    Independently of the renderer's RGB constants (§4.7 asks for "blue-ish" and "orange-ish"
    fills): a fill blends with white at `FILL_ALPHA < 1`, so it is LIGHT (min channel >= 140),
    TINTED (channels spread by >= 8) and dominated by one channel — orange for the flank fill,
    blue for the global one.  Where both are drawn the two tints cancel into a near-neutral
    but still tinted colour ("blend").  Neutral greys (axis, grid, half-covered dashes) and
    dark ink (curve, text) fail the light test, so they never read as a fill.

    Colour alone still cannot separate a fill from a stroke: the antialiased edge of the blue
    dashed baseline is a light blue-ish pixel too.  `_fill_at` adds the shape test that tells
    them apart.
    """
    lo, hi = min(px), max(px)
    if lo < 140 or hi - lo < 8:          # a stroke is dark in at least one channel
        return None
    if px[2] - px[0] >= 25:
        return "blue"
    if px[0] - px[2] >= 25:
        return "orange"
    return "blend"


FILL_ROW_PAD = 1      # antialiased row a fill may cover beyond its window's own rows
FILL_VRUN = 3         # identical rows a fill interior repeats over
FILL_HRUN = 5         # identical columns a fill interior repeats over


def _run_len(px_at, k, step, limit, ref):
    """Rows (or columns) of `ref` reachable from k by `step`, up to `limit`."""
    n = 0
    while n < limit:
        k += step
        if px_at(k) != ref:
            break
        n += 1
    return n


def _fill_at(img, row, x):
    """'orange' / 'blue' / 'blend' when (x, row) is inside a shaded window, else None.

    Colour alone cannot separate a fill from a stroke: the antialiased edge of the blue dashed
    baseline is a light blue-ish pixel too, and the dash runs vertically, so it also repeats
    itself down a column.  What separates them is that a fill is a large flat AREA — it repeats
    over `FILL_VRUN` rows and `FILL_HRUN` columns at once — while a stroke repeats in one
    direction only and is at most two pixels wide in the other.  Text antialiasing fails on
    colour (dark in one channel), so it never gets this far.
    """
    hue = _fill_family(_px(img, row, x))
    if hue is None:
        return None
    ref = _px(img, row, x)
    w, h = img[0], img[1]
    if x + 1 >= w or row + 1 >= h:
        return None
    vrun = 1 + _run_len(lambda k: _px(img, k, x), row, 1, FILL_VRUN - 1, ref) \
        + _run_len(lambda k: _px(img, k, x), row, -1, FILL_VRUN - 1, ref)
    hrun = 1 + _run_len(lambda k: _px(img, row, k), x, 1, FILL_HRUN - 1, ref) \
        + _run_len(lambda k: _px(img, row, k), x, -1, FILL_HRUN - 1, ref)
    if vrun < FILL_VRUN or hrun < FILL_HRUN:
        return None
    return hue


def _is_fill(img, row, x):
    """True when (x, row) is inside a shaded window of any baseline."""
    return _fill_at(img, row, x) is not None


def _is_orange_ink(px):
    """The opaque flank-ramp stroke (`FLANK_RGB` family): saturated, and far from white.

    The translucent fill (250,215,173) is excluded by `max(px) <= 240` plus the green/blue
    gap; antialiased stroke edges are excluded the same way.
    """
    return max(px) <= 240 and px[0] - px[2] >= 70 and px[0] - px[1] >= 40


def _is_dark(px):
    """The gray profile stroke (`CURVE_RGB` is 32,32,32; antialiased neighbours are gray)."""
    return max(px) <= 90


def _non_white_count(img, row, x_from, x_to):
    """Non-white pixels in one image row over the half-open column range."""
    return sum(1 for x in range(x_from, x_to) if _px(img, row, x) != (255, 255, 255))


def _fill_span(img, row, x_from, x_to, min_run=3):
    """Total width of the translucent-fill runs (>= `min_run` px) in one image row.

    A shaded window covers a band of columns; a stroke (the dashed baseline, a glyph, a rule)
    spans one or two.  Counting only runs of at least three pixels keeps the row test free of
    stroke antialiasing without needing the per-pixel shape test `_fill_at` applies.
    """
    total, run = 0, 0
    for x in range(x_from, x_to):
        if _fill_family(_px(img, row, x)):
            run += 1
            continue
        if run >= min_run:
            total += run
        run = 0
    if run >= min_run:
        total += run
    return total


def _fill_count(img, row, x_from, x_to):
    """Shaded-window pixels in one image row over the half-open column range."""
    return sum(1 for x in range(x_from, x_to) if _is_fill(img, row, x))


def _non_white(path):
    w, h, rgb = _pixels(path)
    n = w * h
    cnt = 0
    for k in range(0, n * 3, 3):
        if rgb[k] != 255 or rgb[k + 1] != 255 or rgb[k + 2] != 255:
            cnt += 1
    return n, cnt


def _diff_region(a, b, rows):
    """Pixels that differ between two same-size images, restricted to `rows`."""
    wa, ha, ra = a
    wb, hb, rb = b
    assert (wa, ha) == (wb, hb), ("尺寸不同", (wa, ha), (wb, hb))
    n = 0
    for row in rows:
        if not 0 <= row < ha:
            continue
        for x in range(wa):
            k = (row * wa + x) * 3
            if ra[k:k + 3] != rb[k:k + 3]:
                n += 1
    return n


def _diff_pixels(a, b):
    """Pixels that differ between two same-size PNGs (None when the sizes differ)."""
    wa, ha, ra = _pixels(a)
    wb, hb, rb = _pixels(b)
    if (wa, ha) != (wb, hb):
        return None
    return sum(1 for k in range(0, len(ra), 3) if ra[k:k + 3] != rb[k:k + 3])


# ---------------------------------------------------------------- fixture

# Three identical lanes, two Gaussians each at 1.2 / 4.4 cm over the 6 cm profile: far enough
# apart that neither window is clamped, and both windows have a flank and a global width.
LANES = [{"label": "S1", "marker": False, "bands": [(1.2, 30.0, 0.05), (4.4, 30.0, 0.05)]},
         {"label": "S2", "marker": False, "bands": [(1.2, 30.0, 0.05), (4.4, 30.0, 0.05)]},
         {"label": "S3", "marker": False, "bands": [(1.2, 30.0, 0.05), (4.4, 30.0, 0.05)]}]

# depths that lie outside every window of the fixture, i.e. rows that must carry no fill at all
GAP_CM = (0.4, 2.8, 5.3)


def _write_dens(td, lanes=None, name="fig.dens.json"):
    """Synthetic report v2 → measured dens.json at td/name (the command's real input shape)."""
    rep = selftest_densitometry._synthetic_report(lanes or LANES)
    dens = densitometry.measure(rep)
    path = os.path.join(td, name)
    densitometry.write_json(dens, path)
    return path, dens


def _variant(dens, td, name, mutate):
    """Copy the dens dict, mutate it, write td/name → the copy's path (renderer input only)."""
    dup = json.loads(json.dumps(dens))
    mutate(dup)
    path = os.path.join(td, name)
    densitometry.write_json(dup, path)
    return path


def _scratch(td):
    """Every plot scratch file left in td / beside the outputs (*.mvg or tempfile-named)."""
    hits = []
    for name in os.listdir(td):
        if name.endswith(".mvg") or name.startswith(".dens-plot-") or name.startswith("dens_plot"):
            hits.append(os.path.join(td, name))
    return hits


def _push_ramp(band, gray):
    """Push a band's ramp endpoints to `gray` (both ends), as a hand-edited dens.json could."""
    for end in ("left", "right"):
        band["ramp"][end][1] = float(gray)


def _window_rows(profile, window):
    """Profile rows inside a band's closed integration window [y0, y1]."""
    a, b = float(window[0]) - 1e-9, float(window[1]) + 1e-9
    return [k for k, v in enumerate(profile["y_cm"]) if a <= float(v) <= b]


def _row_index(profile, y_cm):
    """The profile row whose y_cm equals the band centre exactly (fixture gives exact rows)."""
    return [k for k, v in enumerate(profile["y_cm"]) if abs(float(v) - float(y_cm)) <= 1e-9][0]


# ---------------------------------------------------------------- 1) parser registration

def check_parser():
    """`plot-bands` is registered under both group names with the spec defaults, primary `--dens`."""
    from . import gel_photo
    ap = gel_photo.build_parser(canvas=True)
    for group in ("dens", "densitometry"):
        a = ap.parse_args([group, "plot-bands", "--dens", "d.json"])
        assert a.cmd == group and a.action == "plot-bands", (group, a.cmd, a.action)
        assert a.func.__name__ == "cmd_plot_bands", (group, a.func.__name__)
        assert a.out is None and a.lanes is None, (group, a.out, a.lanes)
        assert a.baseline == "both" and a.cols == 3 and a.width == 2400, \
            (group, a.baseline, a.cols, a.width)
    a = ap.parse_args(["dens", "plot-bands", "--dens", "d.json", "--out", "x.png",
                       "--lanes", "2-4", "--baseline", "flank", "--cols", "4", "--width", "1200"])
    assert (a.out, a.lanes, a.baseline, a.cols, a.width) == ("x.png", "2-4", "flank", 4, 1200), a
    # the group is reachable from the public `epcraft` command line, not only from build_parser
    assert "dens" in gel_photo.CANVAS_COMMANDS, "dens 别名不在 CANVAS_COMMANDS"
    return True


# ---------------------------------------------------------------- 2) default + explicit paths

def check_paths():
    """`fig.dens.json` → `fig.bands.png` by default; a non-chain name falls back; --out is honored."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, dens = _write_dens(td)
        os.rename(dens_path, os.path.join(td, "fig.mw.json"))          # not a .dens.json name
        dens_path = os.path.join(td, "fig.mw.json")
        rc, out, err = _run(["plot-bands", "--dens", dens_path])
        assert rc == 0 and err == "", (rc, err)
        assert os.path.isfile(os.path.join(td, "fig.mw.bands.png")), "退落名不是 <stem>.bands.png"
        assert not os.path.exists(os.path.join(td, "fig.dens.bands.png")), "退落名不该叠 .dens"
        assert not os.path.exists(os.path.join(td, "fig.bands.png")), "退落名不该吃掉一段后缀"
        # the chain name the rest of the workflow produces: fig.dens.json → fig.bands.png
        dens_path, dens = _write_dens(td)
        rc, out, err = _run(["plot-bands", "--dens", dens_path])
        assert rc == 0 and err == "", (rc, err)
        png = os.path.join(td, "fig.bands.png")
        assert os.path.isfile(png), "默认输出不是 fig.bands.png"
        # one `[i]` line, stdout only, naming the file and the pixel size (spec §3.4)
        lines = out.strip().split("\n")
        assert len(lines) == 1 and lines[0].startswith("[i] "), out
        w, h, _rgb = _pixels(png)
        assert dens_path in lines[0] and png in lines[0], lines[0]
        assert ("%dx%d" % (w, h)) in lines[0] and str(w) == "2400", lines[0]
        # explicit --out wins, and the same dens can be rendered again next to it
        other = os.path.join(td, "sub", "named.png")
        os.makedirs(os.path.dirname(other))
        rc, out, err = _run(["plot-bands", "--dens", dens_path, "--out", other])
        assert rc == 0 and err == "", (rc, err)
        assert os.path.isfile(other) and os.path.isfile(png), "显式 --out 没写出来"
        # the alias command draws the same picture (same renderer, same data)
        alias = os.path.join(td, "alias.png")
        from . import gel_photo
        out_s, err_s = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_s), contextlib.redirect_stderr(err_s):
            rc2 = gel_photo.main(["densitometry", "plot-bands", "--dens", dens_path,
                                 "--out", alias])
        assert rc2 == 0 and err_s.getvalue() == "", (rc2, err_s.getvalue())
        assert _diff_pixels(other, alias) == 0, "别名与长名字渲染的 PNG 不一致"
    return True


# ---------------------------------------------------------------- 3) size + non-blank + idempotence

def check_size_and_blank():
    """The PNG exists at the requested width, is not blank, and a rerun is pixel-identical."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, dens = _write_dens(td)
        png = os.path.join(td, "default.png")
        rc, _o, err = _run(["plot-bands", "--dens", dens_path, "--out", png])
        assert rc == 0 and err == "", (rc, err)
        w, h, _rgb = _pixels(png)
        assert (w, h) == (2400, dens_plot.band_layout(dens, "both", None, 3, 2400)["height"]), (w, h)
        n, tint = _non_white(png)
        assert tint / float(n) >= 0.02, "图几乎全白（%.4f 非白）" % (tint / float(n))
        assert _bytes(png)[:8] == b"\x89PNG\r\n\x1a\n", "输出不是 PNG"
        # a narrower canvas is exactly as wide as asked, still not blank
        narrow = os.path.join(td, "narrow.png")
        rc, _o, err = _run(["plot-bands", "--dens", dens_path, "--out", narrow, "--width", "800"])
        assert rc == 0 and err == "", (rc, err)
        w2, _h2, _r2 = _pixels(narrow)
        assert w2 == 800, "请求 --width 800，像素宽却是 %d" % w2
        _n, tint2 = _non_white(narrow)
        assert tint2 > 0.02 * _n, "窄图是空白"
        # the same input renders the same picture (pixel-identical; the PNG's own date tEXt
        # chunk differs, so file bytes must not be compared)
        again = os.path.join(td, "again.png")
        rc, _o, err = _run(["plot-bands", "--dens", dens_path, "--out", again])
        assert rc == 0 and err == "", (rc, err)
        assert _diff_pixels(png, again) == 0, "同一输入两次渲染的画面不一致"
    return True


# ---------------------------------------------------------------- 4) shading vs the windows

def check_baseline_shading():
    """The shaded area is the integrated window: fill inside, nothing outside, hue per baseline.

    Positions come from `band_layout` (`Panel.x(gray)` / `Panel.y(y_cm)`); what must be shaded
    comes from the dens.json windows, so a renderer that shades the wrong rows or the wrong
    columns fails even though it agrees with itself.
    """
    with tempfile.TemporaryDirectory() as td:
        dens_path, dens = _write_dens(td)
        imgs = {}
        for base in ("flank", "global", "both"):
            png = os.path.join(td, base + ".png")
            rc, _o, err = _run(["plot-bands", "--dens", dens_path, "--out", png,
                                "--baseline", base])
            assert rc == 0 and err == "", (base, rc, err)
            imgs[base] = _pixels(png)
        sizes = {i[0] for i in imgs.values()}, {i[1] for i in imgs.values()}
        assert sizes == ({2400}, {734}), \
            "三种 baseline 的图尺寸不一致：%r" % ([(k, v[:2]) for k, v in imgs.items()],)
        geo = dens_plot.band_layout(dens, "both", None, 3, 2400)
        lane = dens["lanes"][0]
        prof = lane["profile"]

        for no in geo["lane_order"]:
            panel = geo["panels"][no]
            x0, y0, x1, y1 = panel.box
            for band in lane["bands"]:
                k = _row_index(prof, band["y_cm"])
                y_cm = float(band["y_cm"])
                row = int(round(panel.y(y_cm)))
                xg = int(round(panel.x(float(prof["gray"][k]))))
                xb = int(round(panel.x(float(prof["baseline_global"][k]))))
                ramp_x = max(float(band["ramp"]["left"][1]), float(band["ramp"]["right"][1]))
                x_ramp = int(round(panel.x(ramp_x)))
                xm = (xg + xb) // 2
                # (a) the gray profile is drawn where the mapping says it is (dark stroke)
                assert _is_dark(_px(imgs["both"], row, xg)), \
                    "泳道 %d 带 %d：x(%g) 处不是深色曲线：%r" % (
                        no, band["index"], float(prof["gray"][k]),
                        _px(imgs["both"], row, xg))
                assert y0 < row < y1, (no, band["index"], row, y0, y1)
                # (b) between the curve and the global baseline: orange for flank, blue for global,
                #     a neutral blend (both fills over each other) for both — white means no fill
                p_f = _px(imgs["flank"], row, xm)
                p_g = _px(imgs["global"], row, xm)
                p_b = _px(imgs["both"], row, xm)
                assert _fill_at(imgs["flank"], row, xm) == "orange", \
                    "泳道 %d 带 %d：--baseline flank 的填充不是橙色系（读到 %r）" % (
                        no, band["index"], p_f)
                assert _fill_at(imgs["global"], row, xm) == "blue", \
                    "泳道 %d 带 %d：--baseline global 的填充不是蓝色系（读到 %r）" % (
                        no, band["index"], p_g)
                assert _fill_at(imgs["both"], row, xm) == "blend", \
                    "泳道 %d 带 %d：两个填充叠在同一列却没混合成第三种颜色（读到 %r）" % (
                        no, band["index"], p_b)
                # (c) the flank fill runs on past the global baseline to the ramp; the global fill
                #     stops at the baseline.  (That band of columns exists only because the flank
                #     window is wider, so it also proves the two windows are drawn separately.)
                lo, stop = min(xb, x_ramp) + 4, max(xb, x_ramp)
                assert stop > lo + 6, ("测试几何太窄", no, band["index"], lo, stop)
                assert _fill_count(imgs["flank"], row, lo, stop) >= 8, \
                    "flank 填充没延伸到斜坡：%r" % (_fill_count(imgs["flank"], row, lo, stop),)
                assert _fill_count(imgs["global"], row, lo, stop) == 0, \
                    "global 填充越过了自己的基线：%r" % (
                        _fill_count(imgs["global"], row, lo, stop),)
                assert _fill_count(imgs["both"], row, lo, stop) >= 8, \
                    "both 里看不到 flank 填充：%r" % (_fill_count(imgs["both"], row, lo, stop),)

            # (c2) the fill's VERTICAL extent is the window of the baseline being shaded: with
            #      `--baseline global` the blue area must not spread into the flank window's extra
            #      rows.  A fill row is one that holds a WIDE run of fill-coloured pixels (strokes
            #      and their antialiasing never span 3+ px); those rows are contiguous, so every
            #      one of them must sit inside a window of that baseline (±1 for the outline).
            for base, win in (("flank", "window_flank"), ("global", "window_global")):
                wide = {r for r in range(int(y0) + 1, int(y1))
                        if _fill_span(imgs[base], r, int(x0) + 1, int(x1)) >= 3}
                allowed = set()
                per_band = []
                for other in lane["bands"]:
                    pred = {int(round(panel.y(float(prof["y_cm"][k]))))
                            for k in _window_rows(prof, other[win])}
                    assert pred, ("窗口里没有剖面行", no, win)
                    per_band.append((other["index"], pred))
                    allowed |= set(range(min(pred) - FILL_ROW_PAD, max(pred) + FILL_ROW_PAD + 1))
                assert wide, "泳道 %d：--baseline %s 一个填充行都没有" % (no, base)
                outside = sorted(wide - allowed)
                assert not outside, \
                    "泳道 %d：--baseline %s 的填充画到了窗口之外（行 %r，窗口行 %r）" % (
                        no, base, outside[:6],
                        [(name, min(pred), max(pred)) for name, pred in per_band])
                for name, pred in per_band:
                    assert len(wide & pred) * 4 >= len(pred), \
                        "泳道 %d 带 %d：--baseline %s 的填充没盖住窗口（%d/%d 行）" % (
                            no, name, base, len(wide & pred), len(pred))

            # (c3) the flank ramp segment is clipped to gray 0..255 (§4.7).  `measure` only
            #      produces in-range ramps, so this case hands the renderer a dens.json whose
            #      ramp ends at gray 265 — just past the axis.  The dash must stop at the panel's
            #      right edge (x(255) == x1) instead of running on into the gutter, which is
            #      blank white in a correct render (only the polygon fill may reach it).
            over = _variant(dens, td, "ramp-over.dens.json",
                            lambda d: _push_ramp(d["lanes"][0]["bands"][0], 265.0))
            over_png = os.path.join(td, "ramp-over.png")
            rc, _o, err = _run(["plot-bands", "--dens", over, "--out", over_png,
                                "--baseline", "flank"])
            assert rc == 0 and err == "", (rc, err)
            over_img = _pixels(over_png)
            over_geo = dens_plot.band_layout(dens, "flank", None, 3, 2400)
            over_panel = over_geo["panels"][1]
            ox0, oy0, ox1, oy1 = [int(v) for v in over_panel.box]
            right = min([int(over_geo["panels"][o].box[0]) for o in over_geo["lane_order"]
                         if int(over_geo["panels"][o].box[0]) > ox1] + [over_geo["width"] - 1])
            over_band = dens["lanes"][0]["bands"][0]          # the band whose ramp was pushed
            r0 = int(round(over_panel.y(float(over_band["ramp"]["left"][0]))) - 5)
            r1 = int(round(over_panel.y(float(over_band["ramp"]["right"][0]))) + 5)
            gutter = [(r, x) for r in range(max(oy0 + 1, r0), min(oy1, r1))
                      for x in range(ox1 + 3, right)
                      if _is_orange_ink(_px(over_img, r, x))]
            assert not gutter, \
                "斜坡超出 gray 255 后橙色虚线没被裁剪，画到了面板外（x %d..%d）：%r" % (
                    ox1 + 3, right - 1, gutter[:3])


            # (d) rows outside every window carry no fill, and the left of the curve stays clean
            for depth in GAP_CM:
                row = int(round(panel.y(float(depth))))
                k = min(range(len(prof["y_cm"])),
                        key=lambda i: abs(float(prof["y_cm"][i]) - float(depth)))
                xg = int(round(panel.x(float(prof["gray"][k]))))
                for base in ("flank", "global", "both"):
                    assert _fill_count(imgs[base], row, int(x0) + 2, int(x1) - 1) == 0, \
                        "%s 在窗口外的 y=%.2f cm（行 %d）画了填充" % (base, depth, row)
                    assert _non_white_count(imgs[base], row, int(x0) + 2, xg - 3) == 0, \
                        "%s 在 y=%.2f cm（行 %d）曲线左侧不是白的" % (base, depth, row)
    return True


# ---------------------------------------------------------------- 5) grid options

def check_grid_options():
    """--cols / --lanes build the grid the shared layout predicts (rows grow, selection shrinks)."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, dens = _write_dens(td)
        cases = (("cols3", 3, None), ("cols1", 1, None),
                 ("lanes2", 1, [2]), ("lanes13", 1, [1, 3]))
        h = {}
        for key, cols, lanes in cases:
            png = os.path.join(td, key + ".png")
            argv = ["plot-bands", "--dens", dens_path, "--out", png, "--cols", str(cols)]
            if lanes is not None:
                argv += ["--lanes", ",".join(str(v) for v in lanes)]
            rc, out, err = _run(argv)
            assert rc == 0 and err == "", (key, rc, err)
            w, hh, _r = _pixels(png)
            assert w == 2400, (key, w)
            expected = dens_plot.band_layout(dens, "both", lanes, cols, 2400)
            assert hh == expected["height"], (key, hh, expected["height"])
            assert ("%dx%d" % (w, hh)) in out, (key, out)
            h[key] = hh
        assert h["cols1"] > h["cols3"], ("cols 1 应比 cols 3 多两行", h)
        assert h["lanes2"] < h["cols1"] / 2.0, ("--lanes 2 应只剩一格高", h)
        assert h["lanes13"] < h["cols1"] and h["lanes13"] > h["lanes2"], ("两格应高于一格", h)
        # --lanes keeps the requested lane only: one panel means one lane's band labels
        rc, out, err = _run(["plot-bands", "--dens", dens_path, "--out",
                             os.path.join(td, "sel.png"), "--lanes", "3"])
        assert rc == 0 and err == "", (rc, err)
        assert "1 条泳道" in out, out
    return True


# ---------------------------------------------------------------- 6) width guard (narrow grid)

def check_width_guard():
    """A grid too narrow to draw a positive plot area is refused, not rendered mirrored."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, _dens = _write_dens(td)
        bad = os.path.join(td, "too-narrow.png")
        rc, out, err = _run(["plot-bands", "--dens", dens_path, "--out", bad,
                             "--cols", "10", "--width", "600"])
        assert rc == 2, ("width 600 + cols 10 应拒绝，rc=%r" % rc)
        assert out == "", "失败时不该有 stdout：%r" % out
        _one_line(err, "width 600 + cols 10")
        assert not os.path.exists(bad), "被拒绝的渲染却写出了 PNG"
        assert _scratch(td) == [], "被拒绝的渲染留下了临时脚本：%r" % _scratch(td)
        # the same width with the default column count is a legitimate narrow grid
        good = os.path.join(td, "narrow3.png")
        rc, out, err = _run(["plot-bands", "--dens", dens_path, "--out", good,
                             "--cols", "3", "--width", "600"])
        assert rc == 0 and err == "", (rc, err)
        w, _h, _r = _pixels(good)
        assert w == 600, w
        _n, tint = _non_white(good)
        assert tint > 0.02 * _n, "600 宽的合法网格是空白"
    return True


# ---------------------------------------------------------------- 7) output/input refusals

def check_refusals():
    """Directory / non-PNG / missing dir / input overwrite: one `[!]` line, rc 2, nothing written."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, _dens = _write_dens(td)
        before = _bytes(dens_path)
        cases = (
            ("目录 --out", ["--dens", dens_path, "--out", td]),
            ("非 .png --out", ["--dens", dens_path, "--out", os.path.join(td, "x.jpg")]),
            ("目录不存在", ["--dens", dens_path, "--out", os.path.join(td, "nope", "x.png")]),
            ("覆盖输入 dens", ["--dens", dens_path, "--out", dens_path]),
            ("cols 0", ["--dens", dens_path, "--out", os.path.join(td, "f.png"), "--cols", "0"]),
            ("width 10", ["--dens", dens_path, "--out", os.path.join(td, "g.png"), "--width", "10"]),
            ("缺 dens 文件", ["--dens", os.path.join(td, "missing.dens.json"),
                             "--out", os.path.join(td, "a.png")]),
            ("kind 不对", ["--dens", _bad_kind(td), "--out", os.path.join(td, "b.png")]),
            ("--lanes 越界", ["--dens", dens_path, "--out", os.path.join(td, "c.png"),
                            "--lanes", "9"]),
            ("--lanes 乱写", ["--dens", dens_path, "--out", os.path.join(td, "d.png"),
                            "--lanes", "1,,3"]),
        )
        for label, argv in cases:
            rc, out, err = _run(["plot-bands"] + argv)
            assert rc == 2, (label, rc, err)
            assert out == "", (label, out)
            _one_line(err, label)
        # an unknown --baseline is an argparse `choices` violation: usage on stderr, exit 2,
        # never the command body — the usage line, not a `[!]` line (as in `selftest_cli`)
        rc, out, err = _run(["plot-bands", "--dens", dens_path, "--out",
                             os.path.join(td, "e.png"), "--baseline", "nope"])
        assert rc == 2 and out == "", (rc, out)
        assert "--baseline" in err and "invalid choice" in err, err
        assert not err.startswith("[!] "), "choices 错误不该走命令体的一行错误：%r" % err
        assert _bytes(dens_path) == before, "拒绝路径改写了输入 dens"
        for name in ("x.jpg", "a.png", "b.png", "c.png", "d.png", "e.png", "f.png", "g.png"):
            assert not os.path.exists(os.path.join(td, name)), "失败命令写出了 %s" % name
        assert _scratch(td) == [], "失败路径留下临时脚本：%r" % _scratch(td)
    return True


def _bad_kind(td):
    path = os.path.join(td, "wrong.dens.json")
    densitometry.write_json({"version": 1, "kind": "ep-craft-densitometry-curve", "lanes": []},
                            path)
    return path


# ---------------------------------------------------------------- 8) hard link to the input

def check_hardlink_guard():
    """A second name for the input dens (same inode) must not be overwritten as the PNG."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, _dens = _write_dens(td)
        alias = os.path.join(td, "alias.png")
        os.link(dens_path, alias)
        before = _bytes(dens_path)
        rc, out, err = _run(["plot-bands", "--dens", dens_path, "--out", alias])
        assert rc == 2, ("硬链接别名应被拒绝", rc)
        assert out == "", out
        _one_line(err, "硬链接别名")
        assert _bytes(dens_path) == before, "硬链接别名把输入 dens 覆盖成 PNG 了"
        assert _bytes(alias)[:8] != b"\x89PNG\r\n\x1a\n", "输入文件的 inode 变成了 PNG"
        assert os.path.getsize(dens_path) == os.path.getsize(alias), "同 inode 的两个名字应同大小"
        assert _scratch(td) == [], "硬链接失败路径留下临时脚本：%r" % _scratch(td)
    return True


# ---------------------------------------------------------------- 9) real ImageMagick + cleanup

def check_real_magick():
    """The PNG is made by the real `magick`; a broken one is ONE `[!]` line; no MVG survives."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, dens = _write_dens(td)
        png = os.path.join(td, "real.png")
        magick = shutil.which(magick_io.MAGICK)
        assert magick, "测试环境找不到 magick：%r（GEL_MAGICK / PATH）" % (magick_io.MAGICK,)
        cpv = subprocess.run([magick, "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        assert cpv.returncode == 0 and b"ImageMagick" in cpv.stdout, cpv.stderr[:200]
        # out of process: the documented `python3 -m epcraft dens …` entry point, real binary
        env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        cp = subprocess.run([sys.executable, "-m", "epcraft", "dens", "plot-bands",
                             "--dens", dens_path, "--out", png],
                            cwd=td, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert cp.returncode == 0, (cp.returncode, cp.stderr.decode()[:300])
        assert cp.stderr == b"", cp.stderr[:300]
        assert os.path.isfile(png) and _pixels(png)[0] == 2400
        assert _scratch(td) == [], "成功渲染后留下了临时脚本：%r" % _scratch(td)
        # a broken magick becomes ONE `[!]` line with rc 2 (no traceback), and writes nothing
        missing = os.path.join(td, "no-magick.png")
        env_bad = dict(env, GEL_MAGICK=os.path.join(td, "no-such-magick"))
        cp2 = subprocess.run([sys.executable, "-m", "epcraft", "dens", "plot-bands",
                              "--dens", dens_path, "--out", missing],
                             cwd=td, env=env_bad, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert cp2.returncode == 2, (cp2.returncode, cp2.stderr.decode()[:300])
        assert cp2.stdout == b"", cp2.stdout[:200]
        lines = cp2.stderr.decode().rstrip("\n").split("\n")
        assert len(lines) == 1 and lines[0].startswith("[!] "), cp2.stderr.decode()
        assert not os.path.exists(missing), "magick 缺失时仍写出了 PNG"
        assert _scratch(td) == [], "magick 缺失时留下了临时脚本：%r" % _scratch(td)
        # in process, the same failure through `_render`'s finally block: one-line RuntimeError
        orig = magick_io.MAGICK
        magick_io.MAGICK = os.path.join(td, "no-such-magick")
        try:
            try:
                dens_plot.plot_bands(dens, os.path.join(td, "inproc.png"))
            except RuntimeError as e:
                msg = str(e)
            else:
                raise AssertionError("magick 不可执行时 plot_bands 没报 RuntimeError")
        finally:
            magick_io.MAGICK = orig
        assert "\n" not in msg and msg.strip(), "RuntimeError 不是一行：%r" % msg
        assert not os.path.exists(os.path.join(td, "inproc.png")), "失败时写出了 PNG"
        assert _scratch(td) == [], "失败的渲染留下了 MVG 脚本：%r" % _scratch(td)
    return True


# ---------------------------------------------------------------- 10) lane flags reach the picture

def check_flags_and_labels():
    """clipped / flat top / note tags and the lane header are visible: changing them changes pixels."""
    with tempfile.TemporaryDirectory() as td:
        dens_path, dens = _write_dens(td)
        base_png = os.path.join(td, "base.png")
        rc, _o, err = _run(["plot-bands", "--dens", dens_path, "--out", base_png])
        assert rc == 0 and err == "", (rc, err)

        def _flagged(dup):
            for no, lane in enumerate(dup["lanes"]):
                for band in lane["bands"]:
                    band["clipped"] = (no == 0 and band["index"] == 1)
                    band["flat_top"] = (no == 1 and band["index"] == 2)
                    band["notes"] = (["clamped_mid_up"] if (no == 2 and band["index"] == 1) else [])

        flagged_path = _variant(dens, td, "flagged.dens.json", _flagged)
        flagged_png = os.path.join(td, "flagged.png")
        rc, _o, err = _run(["plot-bands", "--dens", flagged_path, "--out", flagged_png])
        assert rc == 0 and err == "", (rc, err)
        assert _diff_pixels(base_png, flagged_png) not in (0, None), \
            "clipped / flat top / notes 改了，图却一模一样（标志没画出来）"

        # the header has a lane title: switching the label changes the picture
        labelled = _variant(dens, td, "labelled.dens.json",
                            lambda d: d["lanes"][0].__setitem__("label", "STANDARD"))
        labelled_png = os.path.join(td, "labelled.png")
        rc, _o, err = _run(["plot-bands", "--dens", labelled, "--out", labelled_png])
        assert rc == 0 and err == "", (rc, err)
        assert _diff_pixels(base_png, labelled_png) not in (0, None), \
            "泳道标签没进画面（LANE n: label 表头没画）"

        # non-ASCII / quote labels are sanitised instead of breaking the MVG script (ASCII rule)
        weird = _variant(dens, td, "weird.dens.json",
                         lambda d: d["lanes"][0].__setitem__("label", "α'β 泳道"))
        weird_png = os.path.join(td, "weird.png")
        rc, _o, err = _run(["plot-bands", "--dens", weird, "--out", weird_png])
        assert rc == 0 and err == "", (rc, err)
        assert os.path.isfile(weird_png), "非 ASCII 标签把渲染弄崩了（本该转成 ASCII）"
        assert _diff_pixels(base_png, weird_png) not in (0, None), "改标签后图没变"
        assert _scratch(td) == [], "标签路径留下临时脚本：%r" % _scratch(td)

        # the title block states the run parameters (spec §4.7: "the run parameters once in the
        # margin"), so it must carry ink and it must change with --baseline
        base_img = _pixels(base_png)
        title_rows = range(dens_plot.MARGIN, dens_plot.MARGIN + dens_plot.TITLE_H)
        title_ink = sum(1 for r in title_rows for x in range(base_img[0])
                        if _px(base_img, r, x) != (255, 255, 255))
        assert title_ink > 200, "标题栏几乎是空的（%d 个像素）" % title_ink
        flank_png = os.path.join(td, "title-flank.png")
        rc, _o, err = _run(["plot-bands", "--dens", dens_path, "--out", flank_png,
                            "--baseline", "flank"])
        assert rc == 0 and err == "", (rc, err)
        diff = _diff_region(base_img, _pixels(flank_png), title_rows)
        assert diff > 0, "改了 --baseline，标题栏却一模一样（参数行没写出来）"

        # the font comes from the repository picker (spec §2.10): a real file, or the Arial fallback
        font = text_layout.pick_font()
        assert font is None or os.path.exists(font), "字体选择器返回了不存在的文件：%r" % (font,)
    return True


# ---------------------------------------------------------------- entry point

CHECKS = (check_parser, check_paths, check_size_and_blank, check_baseline_shading,
          check_grid_options, check_width_guard, check_refusals, check_hardlink_guard,
          check_real_magick, check_flags_and_labels)


def run():
    for check in CHECKS:
        check()
    print("selftest_dens_plot %d/%d 通过" % (len(CHECKS), len(CHECKS)))
    return len(CHECKS)


if __name__ == "__main__":
    raise SystemExit(run())
