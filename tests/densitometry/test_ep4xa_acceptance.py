#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ep4xa_acceptance — ep-4xa + 空白标准道的独立验收套件（测试方只写这一个文件）。

验收对象：`bd show ep-4xa`（统一 `dens plot`）加上「空白标准道（0 band 标准道）」的验收标准。
全部黑盒：dens.json 由本文件在内存里合成（剖面数组按真实管线取整：y 4 dp、gray / baseline 2 dp），
curve.json 由**公开**的 `densitometry.build_curve` 生成，quant.json 由公开的 `densitometry.quantify`
生成；渲染只走公开的 `dens_plot.plot_bands` / `dens_plot.plot_curve` 与 `epcraft` CLI。

验收条目

1. 空白标准道（`densitometry.build_curve`）
   - `std=[(空白道, None, 0.0), …]` 在 `baseline=flank` / `global` 下都成功：`points` 里有 `mass == 0.0`
     的点，其 `volume` 等于该空白道自身剖面的独立梯形积分（global 用 `baseline_global - gray`，
     flank 用 `flank_ramp`），窗口取**非空白标准带两道窗口的平均值**（3、4、5 三道的窗口刻意互不相同，
     平均值只可能是真的算出来的）；
   - `residuals` 里 `mass == 0.0` 那条的 `rel_pct` 是 `None`，正标准点的 `rel_pct` 是有限数字；
   - `mass == 0.0` + `fit="power"` 抛 `RuntimeError`；
   - 空白道上写 `2:1=0.0`（显式带号）抛 `RuntimeError`；
   - 所有标准道都是 0 band 抛 `RuntimeError`；
   - `parse_std` 接受 `2=0`（CLI 的 `--std` 写法），拒绝负数。

2. 空白道进 `plot_bands`
   - 可用的 Matplotlib 环境里，`--group`（API 的 `groups=`）含一个 0 band 泳道 + 非空白泳道 → 出合法 PNG；
   - 整组成员全是空白的 group → `ValueError`；
   - `plot_curve` 能把 `residuals` 里带 `rel_pct: None` 的 curve.json 画出来（不报错、PNG 非空白，
     且 `None` 那条残差不会被画成一个 0 残差点）。

3. 统一 CLI（`epcraft dens plot` / `epcraft densitometry plot`，ep-4xa）
   - `dens` / `densitometry` 两个组都注册 `plot`，接受 `--dens`、`--curve`、可选 `--quant`、可重复
     `--group`、`--baseline`、`--cols`、`--width`、`--bands-out`、`--curve-out`；
   - `plot-bands` / `plot-curve` 仍注册且仍能端到端出图；
   - 端到端（临时目录 + 真 CLI）：同时写出 `<dens stem>.bands.png` 与 `<curve stem>.curve.png`，
     给了 `--bands-out` / `--curve-out` 就以它们为准；
   - bands 图自动带上 `Standards …` 组（成员来自 curve.json 的标准点），**被 --drop 的标准道不在其中**；
   - curve 图的 quant 样品方块只保留用户 `--group` 里出现的 `(lane, band)`；quant.json 里未分组的
     泳道 / 未分组的带不画；
   - `--bands-out` 指向输入文件（dens / curve / quant）、`--curve-out` 指向输入文件、或两者相等
     都以退出码 2 拒绝（一行 `[!] `，输入文件原样不动）。

Matplotlib 是可选 `[plot]` extra：渲染类检查（第 2、3 组）在没有 Matplotlib 的环境里**显式跳过**
（打印跳过的项数），纯数学检查（第 1 组）两种环境都跑。

跑：PYTHONPATH=src python3 -m tests.test_ep4xa_acceptance
    （基础环境缺 Matplotlib 也能跑通；带 Matplotlib 的环境跑满全部检查。）
`run()` 返回实际跑过的检查数，并打印 `test_ep4xa_acceptance N/N 通过`。
"""
import contextlib
import io
import json
import math
import os
import struct
import tempfile
import unittest.mock

from epcraft import densitometry
from epcraft.gel import profiles

KIND = densitometry.KIND
CURVE_KIND = densitometry.CURVE_KIND
QUANT_KIND = densitometry.QUANT_KIND

DY = 0.01                     # cm / row（真实报告 y_cm 4 dp、dy ≈ 0.01）
HEIGHT_CM = 3.0
ROWS = int(round(HEIGHT_CM / DY)) + 1      # y_cm = 0.00 … 3.00
BG = 240.0                    # 灰度背景（0=黑、255=白）
FLANK_CM = densitometry.FLANK_CM
BLANK_CENTRE = 1.5            # 空白道那道很淡、很宽的染色起伏（不构成带）
BLANK_HALF = 0.30             # cm，compact support：support 外灰度严格等于 BG
BLANK_AMP = 3.0               # 灰度级
STD_WINDOWS = {               # (global, flank) — 三道故意互不相同，平均值才可能是真的算出来的
    3: ((1.34, 1.66), (1.36, 1.64)),
    4: ((1.32, 1.68), (1.35, 1.65)),
    5: ((1.30, 1.70), (1.34, 1.66)),
    6: ((1.28, 1.72), (1.33, 1.67)),
}
LANES = [
    {"lane": 2, "label": "blank 2", "blank": True, "bands": []},
    {"lane": 3, "label": "std 0.5", "bands": [(1.50, 0.09, 12.0)]},
    {"lane": 4, "label": "std 1.0", "bands": [(1.50, 0.09, 24.0)]},
    {"lane": 5, "label": "std 2.0", "bands": [(1.50, 0.09, 48.0)]},
    {"lane": 6, "label": "std 4.0", "bands": [(1.50, 0.09, 96.0)]},
    {"lane": 7, "label": "sample 7", "bands": [(1.10, 0.08, 16.0), (1.90, 0.08, 22.0)]},
    {"lane": 8, "label": "sample 8", "bands": [(2.30, 0.08, 20.0)]},
    {"lane": 9, "label": "sample 9", "bands": [(0.80, 0.08, 24.0)]},
    {"lane": 10, "label": "blank 10", "blank": True, "bands": []},
]
BLANK_LANES = (2, 10)
STD_LANES = (3, 4, 5)
DROPPED_LANE = 6
SAMPLE_GROUPS = "Samples=7:1,9:1"


# ---------------------------------------------------------------- 小工具：积分 / 窗口 / 剖面

def _y_rows():
    return [round(k * DY, 4) for k in range(ROWS)]


def _rows_in(y, y0, y1):
    """闭区间 [y0, y1] 内第一 / 最后一行的下标（窗口内没有行时返回空表）。"""
    idx = [k for k, v in enumerate(y) if y0 - 1e-9 <= v <= y1 + 1e-9]
    assert idx, "[%r, %r] 窗口里没有剖面行" % (y0, y1)
    return idx


def _trapezoid(y, ink, y0, y1):
    """独立实现的梯形积分：只累加窗口内的行（与 area_of 的定义同一数学，但不是同一个函数）。"""
    idx = _rows_in(y, y0, y1)
    total = 0.0
    for k in range(idx[0], idx[-1]):
        total += 0.5 * (ink[k] + ink[k + 1]) * (y[k + 1] - y[k])
    return total


def _ramp_value(ramp, value):
    """本测试自己的 clamp-线性插值（公开契约：端点之间线性、端点外取端值）。"""
    (yl, gl), (yr, gr) = ramp["left"], ramp["right"]
    yl, gl, yr, gr = float(yl), float(gl), float(yr), float(gr)
    if yr <= yl:
        return gl
    t = (float(value) - yl) / (yr - yl)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return gl + (gr - gl) * t


def _blank_gray(v):
    """空白道的灰度：BG 平坦背景 + 一道很淡、很宽、support 紧的余弦起伏（不构成带）。"""
    if abs(v - BLANK_CENTRE) >= BLANK_HALF:
        return BG
    return BG - BLANK_AMP * math.cos(math.pi * (v - BLANK_CENTRE) / (2.0 * BLANK_HALF)) ** 2


def _blank_ink(lane, baseline, y0, y1):
    """空白道在给定窗口上的独立 ink 序列（global 用存储的 baseline_global；flank 用 flank_ramp）。"""
    y = [float(v) for v in lane["profile"]["y_cm"]]
    gray = [float(v) for v in lane["profile"]["gray"]]
    if baseline == "global":
        base = [float(v) for v in lane["profile"]["baseline_global"]]
        return [b - g for b, g in zip(base, gray)]
    ramp = densitometry.flank_ramp(y, gray, y0, y1, y[0], y[-1], FLANK_CM,
                                   lane["profile"]["baseline_global"])
    return [_ramp_value(ramp, v) - g for v, g in zip(y, gray)]


def _blank_expected_volume(lane, baseline, y0, y1):
    return _trapezoid([float(v) for v in lane["profile"]["y_cm"]],
                      _blank_ink(lane, baseline, y0, y1), y0, y1)


# ---------------------------------------------------------------- 合成 dens.json

def _lane_dict(spec):
    """一条泳道 → dens 泳道（profile 两条基线、band 的窗口 / 面积 / ramp 都是自洽算出来的）。"""
    no = int(spec["lane"])
    y = _y_rows()
    if spec.get("blank"):
        gray = [round(_blank_gray(v), 2) for v in y]
    else:
        gray = [round(BG - sum(amp * math.exp(-0.5 * ((v - c) / sig) ** 2)
                               for c, sig, amp in spec["bands"]), 2) for v in y]
    base, _ink, _sm = profiles.ink_trace(gray, 1.0 / DY)
    baseline = [round(v, 2) for v in base]

    centres = [c for c, _sig, _amp in spec["bands"]]
    bands = []
    for pos, (c, _sig, _amp) in enumerate(spec["bands"], 1):
        y_up = (centres[pos - 2] + c) / 2.0 if pos >= 2 else y[0]
        y_dn = (c + centres[pos]) / 2.0 if pos < len(centres) else y[-1]
        std_w = STD_WINDOWS.get(no)
        if std_w is None:
            w0g, w1g = round(c - 0.15, 4), round(c + 0.15, 4)
            w0f, w1f = w0g, w1g
        else:
            (w0g, w1g), (w0f, w1f) = std_w
        ramp = densitometry.flank_ramp(y, gray, w0f, w1f, y_up, y_dn, FLANK_CM, baseline)
        ink_g = [b - g for b, g in zip(baseline, gray)]
        ink_f = [_ramp_value(ramp, v) - g for v, g in zip(y, gray)]
        area_g = _trapezoid(y, ink_g, w0g, w1g)
        area_f = _trapezoid(y, ink_f, w0f, w1f)
        peak_g = max(ink_g[k] for k in _rows_in(y, w0g, w1g))
        peak_f = max(ink_f[k] for k in _rows_in(y, w0f, w1f))
        bands.append({
            "index": pos,
            "y_cm": round(float(c), 4),
            "mw_kda": 45.0,
            "strongest": False,
            "peak_ink_global": round(float(peak_g), 2),
            "peak_ink_flank": round(float(peak_f), 2),
            "window_global": [round(float(w0g), 4), round(float(w1g), 4)],
            "window_flank": [round(float(w0f), 4), round(float(w1f), 4)],
            "area_global": round(float(area_g), 3),
            "area_flank": round(float(area_f), 3),
            "ramp": {"left": [round(float(ramp["left"][0]), 4), round(float(ramp["left"][1]), 2)],
                     "right": [round(float(ramp["right"][0]), 4), round(float(ramp["right"][1]), 2)],
                     "source": ramp.get("source", "flank")},
            "clipped": False,
            "flat_top": False,
            "notes": [],
        })
    if bands:
        best = max(bands, key=lambda b: (b["area_flank"], -b["index"]))
        best["strongest"] = True

    return {
        "lane": no,
        "label": spec.get("label"),
        "marker": False,
        "trimmed": False,
        "noise": 0.4,
        "profile": {"y_cm": y, "gray": gray, "baseline_global": baseline},
        "bands": bands,
        "area_global": round(sum(b["area_global"] for b in bands), 3),
        "area_flank": round(sum(b["area_flank"] for b in bands), 3),
    }


def _dens():
    return {
        "version": 1,
        "kind": KIND,
        "units": dict(densitometry.UNITS),
        "source": {"report": "", "gel": {"width_cm": 8.6, "height_cm": HEIGHT_CM},
                   "window": {"top_cm": 0.0, "bottom_cm": HEIGHT_CM},
                   "marker_lane": None},
        "params": {"flank_cm": FLANK_CM, "min_ink": 0.5, "smooth_cm": 0.03,
                   "window": densitometry.WINDOW_TEXT, "lanes": "all lanes"},
        "lanes": [_lane_dict(spec) for spec in LANES],
        "notes": [],
    }


def _lane_of(dens, no):
    for lane in dens["lanes"]:
        if int(lane["lane"]) == int(no):
            return lane
    raise AssertionError("合成 dens 里没有泳道 %d" % no)


def _std_average_window(dens, baseline, lanes=STD_LANES):
    """非空白标准带两道窗口的平均值（窗口按 baseline 取 window_global / window_flank）。"""
    key = "window_global" if baseline == "global" else "window_flank"
    wins = [_lane_of(dens, no)["bands"][0][key] for no in lanes]
    return (sum(float(w[0]) for w in wins) / len(wins),
            sum(float(w[1]) for w in wins) / len(wins))


def _curve_json(dens):
    """临时目录里用的 curve.json：标准道 3/4/5/6，其中 6 被 --drop（只保留 used: false）。"""
    return densitometry.build_curve(
        dens, [(3, None, 0.5), (4, None, 1.0), (5, None, 2.0), (6, None, 4.0)],
        fit="linear", baseline="flank", drop=[DROPPED_LANE],
        dens_path="fig.dens.json", std_text="3=0.5,4=1,5=2,6=4")


def _write_json(obj, path):
    densitometry.write_json(obj, path)
    return path


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- 环境 / CLI / 图形捕获

def _have_mpl():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def _mpl():
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    return matplotlib, Figure, FigureCanvasAgg


def _cli(argv):
    """跑一次公开 CLI，捕获 stdout / stderr 与退出码（argparse 的 SystemExit 也算退出码）。"""
    from epcraft.cli import gel_photo
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = gel_photo.main(list(argv), canvas=True)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 2
    return rc, out.getvalue(), err.getvalue()


def _cli_with_figures(argv):
    """跑 CLI 并捕获它保存的每个 Figure（路径 + Figure），不打开任何 GUI。"""
    _matplotlib, Figure, _Canvas = _mpl()
    captured = []
    real_savefig, real_clear = Figure.savefig, Figure.clear

    def savefig(self, fname, *args, **kwargs):
        try:
            name = os.fspath(getattr(fname, "name", fname))
        except TypeError:
            name = None
        captured.append((self, name))
        self._epcraft_saved = True
        return real_savefig(self, fname, *args, **kwargs)

    def clear(self, *args, **kwargs):
        # 渲染器 savefig 后会 clear()：保留 artist 以便检查，别真清掉。
        if getattr(self, "_epcraft_saved", False):
            return None
        return real_clear(self, *args, **kwargs)

    with unittest.mock.patch.object(Figure, "savefig", savefig), \
            unittest.mock.patch.object(Figure, "clear", clear):
        rc, out, err = _cli(argv)
    return rc, out, err, captured


def _fig_texts(fig):
    """Figure 上的全部绝对定位文字（渲染器用 fig.text 画标题 / 图例 / 标注）。"""
    return [artist.get_text() for artist in getattr(fig, "texts", [])
            if hasattr(artist, "get_text")]


def _is_curve_fig(fig):
    """curve 图带方程标注（`mass = … · area ± …`）；bands 图没有这行。"""
    return any(text.strip().startswith("mass =") for text in _fig_texts(fig))


def _legend_has_band(texts, lane, band):
    """图例里是否有 `lane.band` 这一行（兼容 cell 逐格写、整行 `3.1 …`、`Lane 3 b1` 三种排法）。"""
    want = "%d.%d" % (lane, band)
    for text in texts:
        head = text.strip()
        if head == want or head.startswith(want + " "):
            return True
        if head.startswith("Lane %d b%d" % (lane, band)):
            return True
    return False


def _sample_marker_areas(fig):
    """curve 图里所有样品方块（marker='s'，有数据点）的 area。"""
    xs = []
    for ax in fig.axes:
        for line in ax.lines:
            if (line.get_marker() or "none") != "s":
                continue
            xd = list(line.get_xdata())
            if xd:
                xs.extend(float(v) for v in xd)
    return xs


def _png_header(path):
    with open(path, "rb") as f:
        head = f.read(8)
        assert head == b"\x89PNG\r\n\x1a\n", "%s 不是 PNG（头 %r）" % (path, head)
        f.read(4)
        assert f.read(4) == b"IHDR", "%s 的 PNG 头里没有 IHDR" % path
        width, height = struct.unpack(">II", f.read(8))
    return int(width), int(height)


def _assert_png(path, width=None):
    assert os.path.isfile(path), "没有写出 PNG：%s" % path
    w, h = _png_header(path)
    assert w > 200 and h > 120, "PNG 太小：%dx%d（%s）" % (w, h, path)
    if width is not None:
        assert abs(w - width) <= 2, "PNG 宽 %d，请求 %d（%s）" % (w, width, path)
    _matplotlib, _Figure, _Canvas = _mpl()
    from matplotlib.image import imread
    img = imread(path)
    assert img.ndim == 3 and img.shape[2] in (3, 4), "PNG 形状异常：%r" % (img.shape,)
    rgb = img[:, :, :3]
    fraction = float((rgb < 0.98).any(axis=2).mean()) if rgb.size else 0.0
    assert fraction >= 0.005, "PNG 几乎是空白（非白像素 %.4f，%s）" % (fraction, path)
    return w, h


def _must_succeed(call, what):
    """验收要求成功的调用：任何异常都翻成一行 AssertionError（不吐 traceback）。"""
    try:
        return call()
    except AssertionError:
        raise
    except Exception as exc:
        first = (str(exc).splitlines() or [""])[0]
        raise AssertionError("%s 不应失败（%s: %s）" % (what, type(exc).__name__, first))


def _must_raise(kinds, call, what):
    """验收要求拒绝的调用：返回异常正文，并核对它是一行非空消息。"""
    try:
        call()
    except kinds as exc:
        text = str(exc)
        assert text.strip(), "%s 抛了 %s 但消息是空的" % (what, type(exc).__name__)
        assert len(text.splitlines()) == 1, "%s 的错误消息不是一行：%r" % (what, text)
        return text
    except AssertionError:
        raise
    except Exception as exc:
        raise AssertionError("%s 抛出 %s，期望 %r：%s"
                             % (what, type(exc).__name__, kinds, exc))
    raise AssertionError("%s 没有抛出 %r" % (what, kinds))


def _parse(ap, argv, what):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return ap.parse_args(argv)
    except SystemExit as exc:
        raise AssertionError("%s 被 parser 拒绝（SystemExit %r）：%r"
                             % (what, exc.code, argv))


def _error_line(stderr, what):
    lines = stderr.rstrip("\n").split("\n")
    assert len(lines) == 1, "%s：stderr 不是一行：%r" % (what, stderr)
    assert lines[0].startswith("[!] "), "%s：不是 [!] 开头的错误行：%r" % (what, lines[0])
    assert "Traceback" not in stderr, "%s：吐了 traceback：%r" % (what, stderr)


def _std_ok_stderr(err):
    if not err.strip():
        return True
    lines = [line for line in err.strip().splitlines() if line.strip()]
    if any("Traceback" in line or line.startswith("[!]") for line in lines):
        return False
    return all("LaneLabelWarning" in line for line in lines)


# ---------------------------------------------------------------- 1. 空白标准道（纯数学）

def check_parse_std_blank():
    """`--std` 语法接受 `2=0`（空白标准道），拒绝负数。"""
    got = densitometry.parse_std("2=0,3=0.5")
    assert got == [(2, None, 0.0), (3, None, 0.5)], got
    assert densitometry.parse_std("2:*=0") == [(2, "*", 0.0)]
    assert densitometry.parse_std(" 3 = 0.5 ") == [(3, None, 0.5)]
    _must_raise((RuntimeError,), lambda: densitometry.parse_std("2=-1"),
                "parse_std('2=-1')")
    _must_raise((RuntimeError,), lambda: densitometry.parse_std("2=nan"),
                "parse_std('2=nan')")
    return True


def _zero_point(curve):
    hits = [p for p in curve.get("points") or [] if float(p.get("mass", 1.0)) == 0.0]
    assert len(hits) == 1, "curve.points 里应恰有一个 mass == 0.0 的点（实际 %d 个）" % len(hits)
    return hits[0]


def _zero_residual(curve):
    for res in curve.get("residuals") or []:
        if float(res.get("mass", -1.0)) == 0.0:
            return res
    raise AssertionError("curve.residuals 里缺少 mass == 0.0 的那条")


def _check_blank_standard(baseline):
    """空白标准道在某个基线下成功，且 volume 等于独立梯形积分。"""
    dens = _dens()
    lane = _lane_of(dens, 2)
    y0, y1 = _std_average_window(dens, baseline)
    expected = _blank_expected_volume(lane, baseline, y0, y1)
    assert abs(expected) > 0.01, \
        "夹具不对：空白道在 [%.3f, %.3f] 上的 %s 积分只有 %.6f，太小测不出差异" % (y0, y1, baseline, expected)
    # 夹具自检：窗口远在剖面两端之内，flank 的 y_up / y_dn 取法不影响预期值，
    # 所以任何合理实现都必须落在同一个积分上（这才是「空白道自身剖面」的意思）。
    if baseline == "flank":
        alt = (float(lane["profile"]["y_cm"][0]) + 0.9, float(lane["profile"]["y_cm"][-1]) - 0.9)
        alt_ramp = densitometry.flank_ramp(
            [float(v) for v in lane["profile"]["y_cm"]],
            [float(v) for v in lane["profile"]["gray"]],
            y0, y1, alt[0], alt[1], FLANK_CM,
            lane["profile"]["baseline_global"])
        alt_ink = [_ramp_value(alt_ramp, v) - float(g)
                   for v, g in zip(lane["profile"]["y_cm"], lane["profile"]["gray"])]
        alt_value = _trapezoid([float(v) for v in lane["profile"]["y_cm"]], alt_ink, y0, y1)
        assert abs(alt_value - expected) < 5e-4, \
            "夹具不对：flank 端点取法不同会改变预期值（%.6f vs %.6f）" % (alt_value, expected)

    std = [(2, None, 0.0), (3, None, 0.5), (4, None, 1.0), (5, None, 2.0)]
    curve = _must_succeed(
        lambda: densitometry.build_curve(dens, std, fit="linear", baseline=baseline),
        "build_curve(std 含 0 质量空白道, baseline=%r)" % baseline)

    assert curve.get("kind") == CURVE_KIND, curve.get("kind")
    zero = _zero_point(curve)
    assert abs(float(zero["volume"]) - expected) <= 0.002, \
        ("空白标准道 volume %.3f ≠ 独立梯形积分 %.3f（baseline=%s，窗口 [%.3f, %.3f]）"
         % (float(zero["volume"]), expected, baseline, y0, y1))
    assert zero.get("n") in (1, None), \
        "空白标准点不该是重复点：n=%r" % (zero.get("n"),)

    res_zero = _zero_residual(curve)
    assert res_zero.get("rel_pct") is None, \
        "mass == 0.0 的残差必须是 rel_pct: None（收到 %r）" % (res_zero.get("rel_pct"),)
    positive = [r for r in curve["residuals"] if float(r["mass"]) > 0.0]
    assert len(positive) == 3, "正标准点的残差应是 3 条（收到 %d 条）" % len(positive)
    for res in positive:
        value = res.get("rel_pct")
        assert isinstance(value, (int, float)) and not isinstance(value, bool), \
            "正标准点的 rel_pct 应是数字（泳道质量 %r 收到 %r）" % (res.get("mass"), value)
        assert math.isfinite(float(value)), "正标准点的 rel_pct 不是有限数：%r" % (value,)

    # 正标准点的残差要能由存储的 fit_params 复算出来（模型确实按正标准点拟合）
    fp = curve["fit_params"]
    for res in positive:
        predicted = float(fp["intercept"]) + float(fp["slope"]) * float(res["volume"])
        recomputed = (predicted - float(res["mass"])) / float(res["mass"]) * 100.0
        assert abs(recomputed - float(res["rel_pct"])) <= 0.1, \
            "rel_pct %.3f 与 fit_params 复算的 %.3f 差太多" % (float(res["rel_pct"]), recomputed)
    return True


def check_blank_std_linear_global():
    return _check_blank_standard("global")


def check_blank_std_linear_flank():
    return _check_blank_standard("flank")


def check_blank_std_power_refused():
    """`mass == 0.0` + `fit="power"` 必须抛 RuntimeError（log10(0) 不是数）。"""
    dens = _dens()
    std = [(2, None, 0.0), (3, None, 0.5), (4, None, 1.0), (5, None, 2.0)]
    _must_raise((RuntimeError,),
                lambda: densitometry.build_curve(dens, std, fit="power", baseline="flank"),
                'build_curve(std 含 0 质量, fit="power")')
    return True


def check_blank_std_band_index_refused():
    """空白道写显式带号（`2:1=0.0`）→ RuntimeError（那道没有第 1 条带）。"""
    dens = _dens()
    std = [(2, 1, 0.0), (3, None, 0.5), (4, None, 1.0)]
    _must_raise((RuntimeError,),
                lambda: densitometry.build_curve(dens, std, fit="linear", baseline="flank"),
                "build_curve(std=[(2, 1, 0.0), …])")
    return True


def check_blank_std_all_blank_refused():
    """所有标准道都是 0 band → RuntimeError（没有能拟合的点）。"""
    dens = _dens()
    std = [(2, None, 0.0), (10, None, 0.0)]
    _must_raise((RuntimeError,),
                lambda: densitometry.build_curve(dens, std, fit="linear", baseline="flank"),
                "build_curve(std 全是空白道)")
    return True


# ---------------------------------------------------------------- 3a. CLI parser

def check_plot_parser_registration():
    """`dens plot` / `densitometry plot` 注册齐全；`plot-bands` / `plot-curve` 仍在。"""
    from epcraft.cli import gel_photo
    for group in ("dens", "densitometry"):
        ap = gel_photo.build_parser(canvas=True)
        args = _parse(ap, [group, "plot", "--dens", "d.json", "--curve", "c.json"],
                      "%s plot 的最小参数" % group)
        assert args.cmd == group, (args.cmd, group)
        assert args.action == "plot", "action 不是 plot：%r" % (getattr(args, "action", None),)
        assert callable(args.func), "plot 没有 handler：%r" % (vars(args),)
        assert args.dens == "d.json" and args.curve == "c.json", vars(args)
        assert getattr(args, "quant") is None, "没有 --quant 时 quant 应为 None：%r" % (vars(args),)
        assert str(args.baseline).lower() in ("flank", "global", "both"), \
            "--baseline 默认值异常：%r" % (args.baseline,)
        assert isinstance(args.cols, int) and args.cols >= 1, vars(args)
        assert isinstance(args.width, int) and args.width >= 600, vars(args)
        assert getattr(args, "bands_out", None) is None and getattr(args, "curve_out", None) is None, \
            "--bands-out / --curve-out 的默认值应是 None：%r" % (vars(args),)

        full = _parse(
            ap,
            [group, "plot", "--dens", "d.json", "--curve", "c.json",
             "--quant", "q.json", "--group", SAMPLE_GROUPS, "--group", "Blank=2",
             "--baseline", "both", "--cols", "2", "--width", "900",
             "--bands-out", "b.png", "--curve-out", "c.png"],
            "%s plot 的全部选项" % group)
        assert full.quant == "q.json", vars(full)
        given = getattr(full, "group", None) or getattr(full, "groups", None) or []
        assert len(given) == 2, "--group 应可重复（收到 %r）" % (given,)
        assert full.baseline == "both" and full.cols == 2 and full.width == 900, vars(full)
        assert full.bands_out == "b.png" and full.curve_out == "c.png", vars(full)

        # 缺 --curve 是用法错误（argparse 退出码 2），不是崩溃。
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                ap.parse_args([group, "plot", "--dens", "d.json"])
        except SystemExit as exc:
            assert exc.code == 2, "缺 --curve 时 parser 退出码 %r，应为 2" % (exc.code,)
        else:
            raise AssertionError("%s plot 缺 --curve 时 parser 没有拒绝" % group)

        for action, func_name, argv in (
                ("plot-bands", "cmd_plot_bands", ["--dens", "d.json"]),
                ("plot-curve", "cmd_plot_curve", ["--curve", "c.json"])):
            legacy = _parse(ap, [group, action] + argv, "%s %s" % (group, action))
            assert legacy.action == action, (legacy.action, action)
            assert legacy.func.__name__ == func_name, \
                "%s %s 的 handler 变成了 %r" % (group, action, legacy.func.__name__)
    return True


# ---------------------------------------------------------------- 2. 空白道进 plot_bands

def check_plot_bands_blank_group_renders():
    """含空白道（0 band）的 --group 与非空白泳道一起 → 合法 PNG，不报错。"""
    from epcraft import plotting as dens_plot
    dens = _dens()
    groups = [("Blank mixed", [(2, None), (3, None), (4, None)]), ("Samples", [(7, 1)])]
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "blank-group.png")
        _matplotlib, Figure, _Canvas = _mpl()
        captured = []
        real_savefig, real_clear = Figure.savefig, Figure.clear

        def savefig(self, fname, *args, **kwargs):
            captured.append(self)
            self._epcraft_saved = True
            return real_savefig(self, fname, *args, **kwargs)

        def clear(self, *args, **kwargs):
            if getattr(self, "_epcraft_saved", False):
                return None
            return real_clear(self, *args, **kwargs)

        with unittest.mock.patch.object(Figure, "savefig", savefig), \
                unittest.mock.patch.object(Figure, "clear", clear):
            ret = _must_succeed(
                lambda: dens_plot.plot_bands(dens, out, baseline="flank", cols=2,
                                             width=900, groups=groups),
                "plot_bands(groups 含 0 band 空白道)")
        assert isinstance(ret, tuple) and len(ret) == 2, "plot_bands 返回 %r，应为 (w, h)" % (ret,)
        _assert_png(out, width=900)
        assert captured, "plot_bands 没有通过 Figure.savefig 保存任何 Figure"
        texts = " | ".join(_fig_texts(captured[-1]))
        assert "Blank mixed" in texts, "含空白道的 group 没有画成 tile：%r" % (texts[:300],)
        assert "Samples" in texts, "非空白 group 没有画成 tile：%r" % (texts[:300],)
    return True


def check_plot_bands_all_blank_group_refused():
    """整组全是空白道 → ValueError（没有局部峰可叠）。"""
    from epcraft import plotting as dens_plot
    dens = _dens()
    groups = [("All blank", [(2, None), (10, None)])]
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "all-blank.png")
        _must_raise((ValueError,),
                    lambda: dens_plot.plot_bands(dens, out, baseline="flank", cols=1,
                                                 width=900, groups=groups),
                    "plot_bands(groups 全是空白道)")
        assert not os.path.exists(out), "全空白 group 被拒绝后不该留下 PNG：%s" % out
    return True


def check_plot_curve_none_rel_pct_renders():
    """curve.json 的 residuals 里 `rel_pct: None`（来自 0 质量空白标准）→ 仍能出图。"""
    from epcraft import plotting as dens_plot
    dens = _dens()
    curve = densitometry.build_curve(
        dens, [(3, None, 0.5), (4, None, 1.0), (5, None, 2.0)],
        fit="linear", baseline="global")
    y0, y1 = _std_average_window(dens, "global")
    volume = round(_blank_expected_volume(_lane_of(dens, 2), "global", y0, y1), 3)
    fp = curve["fit_params"]
    zero = {"mass": 0.0, "volume": volume, "n": 1, "sd": None, "cv_pct": None,
            "lanes": [2], "entries": [[2, None]], "used": True}
    curve["points"].insert(0, zero)
    curve["residuals"].insert(0, {"mass": 0.0, "volume": volume,
                                  "predicted_mass": round(float(fp["intercept"]), 3),
                                  "rel_pct": None})
    numeric_residuals = [r for r in curve["residuals"] if r["rel_pct"] is not None]

    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "blank-curve.png")
        _matplotlib, Figure, _Canvas = _mpl()
        captured = []
        real_savefig, real_clear = Figure.savefig, Figure.clear

        def savefig(self, fname, *args, **kwargs):
            captured.append(self)
            self._epcraft_saved = True
            return real_savefig(self, fname, *args, **kwargs)

        def clear(self, *args, **kwargs):
            if getattr(self, "_epcraft_saved", False):
                return None
            return real_clear(self, *args, **kwargs)

        with unittest.mock.patch.object(Figure, "savefig", savefig), \
                unittest.mock.patch.object(Figure, "clear", clear):
            ret = _must_succeed(lambda: dens_plot.plot_curve(curve, out, width=900),
                                "plot_curve(residuals 含 rel_pct: None)")
        assert isinstance(ret, tuple) and len(ret) == 2, "plot_curve 返回 %r，应为 (w, h)" % (ret,)
        _assert_png(out)
        assert captured, "plot_curve 没有通过 Figure.savefig 保存任何 Figure"
        fig = captured[-1]
        assert _is_curve_fig(fig), "curve 图里没有方程标注（不是 plot_curve 的产物？）"

        # `None` 那条残差不能被画成一个 0% 点：圆点总数不能超过「标准点 + 有数字的残差」。
        circles = 0
        for ax in fig.axes:
            for line in ax.lines:
                if (line.get_marker() or "none") == "o":
                    circles += len(list(line.get_xdata()))
        # 上界：标准点最多 4 个（3 个正标准 + 0 质量点）+ 3 条数字残差；把 `None` 画成 0 % 会多出一点。
        standard_circles = len(curve["points"])          # 3 个正标准点 + 1 个 0 质量点
        assert circles >= len(numeric_residuals), \
            "残差圆点只有 %d 个（数字残差有 %d 条），残差条没画出来" % (circles, len(numeric_residuals))
        assert circles <= standard_circles + len(numeric_residuals), \
            ("残差圆点数量不对：%d（最多 标准点 %d + 数字残差 %d = %d；"
             "rel_pct: None 那条不该被画成一个点）"
             % (circles, standard_circles, len(numeric_residuals),
                standard_circles + len(numeric_residuals)))
    return True


# ---------------------------------------------------------------- 3b. 统一 CLI 端到端

def _e2e_files(td):
    """dens / curve / quant 三件套写到 td，返回 (dens_path, curve_path, quant_path, quant)。"""
    dens = _dens()
    dens_path = _write_json(dens, os.path.join(td, "fig.dens.json"))
    curve = _curve_json(dens)
    curve_path = _write_json(curve, os.path.join(td, "fig.curve.json"))
    quant = densitometry.quantify(dens, curve, [densitometry.parse_sample("Samples=7:1,8,9")],
                                  dens_path=dens_path, curve_path=curve_path)
    quant_path = _write_json(quant, os.path.join(td, "fig.quant.json"))
    return dens_path, curve_path, quant_path, quant


def _plot_argv(dens_path, curve_path, quant_path, *extra):
    return ["dens", "plot", "--dens", dens_path, "--curve", curve_path,
            "--quant", quant_path, "--group", SAMPLE_GROUPS,
            "--baseline", "both", "--cols", "2", "--width", "900"] + list(extra)


def check_cli_plot_end_to_end():
    """`dens plot` 一次写出两张图、自动带 Standards 组、并按 --group 过滤 quant 样品。"""
    with tempfile.TemporaryDirectory() as td:
        dens_path, curve_path, quant_path, quant = _e2e_files(td)
        rc, out, err, captured = _cli_with_figures(
            _plot_argv(dens_path, curve_path, quant_path))
        assert rc == 0, "dens plot 退出码 %r，stderr=%r" % (rc, err)
        assert not err.strip() or _std_ok_stderr(err), "dens plot 打错了：%r" % err
        assert len(captured) == 2, "一次 dens plot 应保存 2 张图（bands + curve），实际 %d 张：%r" \
            % (len(captured), [name for _fig, name in captured])

        bands_png = os.path.join(td, "fig.bands.png")
        curve_png = os.path.join(td, "fig.curve.png")
        _assert_png(bands_png, width=900)
        _assert_png(curve_png)
        # 默认名只看磁盘上的结果（渲染器写入是原子替换，savefig 可能先落临时名）。
        written = sorted(name for name in os.listdir(td) if name.endswith(".png"))
        assert written == ["fig.bands.png", "fig.curve.png"], \
            "默认输出名应是 <dens stem>.bands.png 与 <curve stem>.curve.png，实际 %r" % (written,)

        bands = [fig for fig, _name in captured if not _is_curve_fig(fig)]
        curves = [fig for fig, _name in captured if _is_curve_fig(fig)]
        assert len(bands) == 1 and len(curves) == 1, \
            "捕获到的图分不清 bands / curve（bands %d、curve %d）" % (len(bands), len(curves))
        band_texts = [text for text in _fig_texts(bands[0])]

        # (a) 自动 Standards 组：名字以 Standards 开头，列出 3/4/5 的带行，drop 掉的 6 不在其中。
        std_titles = [text for text in band_texts if text.strip().startswith("Standards")]
        assert std_titles, "bands 图里没有自动生成的 `Standards …` 组：%r" % (band_texts[:20],)
        for lane_no in STD_LANES:
            assert _legend_has_band(band_texts, lane_no, 1), \
                ("Standards 组里缺少标准道 %d 的带 1 图例行：%r"
                 % (lane_no, band_texts[:40]))
        assert not _legend_has_band(band_texts, DROPPED_LANE, 1), \
            "被 --drop 的标准道 6 仍然出现在 bands 图里（Standards 组没排除 drop）"
        assert "Samples" in band_texts, "用户给的 --group Samples 没画成 tile"

        # (b) quant 样品方块只保留 (7,1) 与 (9,1)：7.2 与未分组的 8.1 不画。
        dens_obj = _dens()
        by_pair = {}
        for lane in dens_obj["lanes"]:
            for band in lane["bands"]:
                by_pair[(int(lane["lane"]), band["index"])] = float(band["area_flank"])
        for series in quant["samples"]:
            for member in series["members"]:
                by_pair[(int(member["lane"]), member["band"])] = float(member["area"])
        want = {round(by_pair[(7, 1)], 3), round(by_pair[(9, 1)], 3)}
        got = {round(area, 3) for area in _sample_marker_areas(curves[0])}
        assert got == want, ("curve 图的样品方块应是 --group 选中的 (7,1)/(9,1) 面积 %r，"
                             "实际 %r（未分组的 (7,2) 面积 %r、泳道 8 的 %r 不应出现）"
                             % (sorted(want), sorted(got),
                                round(by_pair[(7, 2)], 3), round(by_pair[(8, 1)], 3)))
        assert round(by_pair[(7, 2)], 3) not in got, "未分组的带 (7,2) 被画进了 curve 图"
        assert round(by_pair[(8, 1)], 3) not in got, "未分组的泳道 8 被画进了 curve 图"

        # (c) 老命令仍然可用（同一份输入）。
        legacy_bands = os.path.join(td, "legacy.bands.png")
        legacy_curve = os.path.join(td, "legacy.curve.png")
        rc, out, err = _cli(["dens", "plot-bands", "--dens", dens_path, "--out", legacy_bands,
                             "--baseline", "flank", "--width", "900"])
        assert rc == 0, "plot-bands 退出码 %r，stderr=%r" % (rc, err)
        _assert_png(legacy_bands, width=900)
        rc, out, err = _cli(["dens", "plot-curve", "--curve", curve_path,
                             "--quant", quant_path, "--out", legacy_curve])
        assert rc == 0, "plot-curve 退出码 %r，stderr=%r" % (rc, err)
        _assert_png(legacy_curve)
    return True


def check_cli_plot_explicit_outputs():
    """`--bands-out` / `--curve-out` 覆盖默认名，且默认名不再出现。"""
    with tempfile.TemporaryDirectory() as td:
        dens_path, curve_path, quant_path, _quant = _e2e_files(td)
        bands_out = os.path.join(td, "custom-bands.png")
        curve_out = os.path.join(td, "custom-curve.png")
        rc, out, err, captured = _cli_with_figures(
            _plot_argv(dens_path, curve_path, quant_path,
                       "--bands-out", bands_out, "--curve-out", curve_out))
        assert rc == 0, "dens plot --bands-out/--curve-out 退出码 %r，stderr=%r" % (rc, err)
        _assert_png(bands_out, width=900)
        _assert_png(curve_out)
        assert not os.path.exists(os.path.join(td, "fig.bands.png")), \
            "给了 --bands-out 仍写出了默认 fig.bands.png"
        assert not os.path.exists(os.path.join(td, "fig.curve.png")), \
            "给了 --curve-out 仍写出了默认 fig.curve.png"
        written = sorted(name for name in os.listdir(td) if name.endswith(".png"))
        assert written == ["custom-bands.png", "custom-curve.png"], \
            "--bands-out/--curve-out 没被完全尊重：目录里的 PNG 是 %r" % (written,)
    return True


def check_cli_plot_refusals():
    """`--bands-out` / `--curve-out` 指向输入文件、或两者相同 → 退出码 2，输入不被覆盖。"""
    with tempfile.TemporaryDirectory() as td:
        dens_path, curve_path, quant_path, _quant = _e2e_files(td)
        dens_before = _read_json(dens_path)
        curve_before = _read_json(curve_path)
        quant_before = _read_json(quant_path)

        curve_out_same = os.path.join(td, "same.png")
        cases = [
            (["--bands-out", dens_path], "bands-out 指向 --dens"),
            (["--curve-out", curve_path], "curve-out 指向 --curve"),
            (["--bands-out", quant_path], "bands-out 指向 --quant"),
            (["--curve-out", dens_path], "curve-out 指向 --dens"),
            (["--bands-out", curve_out_same, "--curve-out", curve_out_same],
             "bands-out 与 curve-out 相等"),
        ]
        for extra, what in cases:
            rc, out, err = _cli(_plot_argv(dens_path, curve_path, quant_path, *extra))
            assert rc == 2, "%s 时退出码 %r（应为 2），stdout=%r stderr=%r" % (what, rc, out, err)
            _error_line(err, what)
        assert _read_json(dens_path) == dens_before, "dens.json 被 dens plot 覆盖了"
        assert _read_json(curve_path) == curve_before, "curve.json 被 dens plot 覆盖了"
        assert _read_json(quant_path) == quant_before, "quant.json 被 dens plot 覆盖了"
        assert not os.path.exists(os.path.join(td, "same.png")), \
            "--bands-out == --curve-out 被拒绝后仍写下了 same.png"
        assert not os.path.exists(os.path.join(td, "fig.bands.png")), \
            "拒绝运行却留下了 fig.bands.png"
    return True


# ---------------------------------------------------------------- 入口

CHECKS_BASE = [
    check_parse_std_blank,
    check_blank_std_linear_global,
    check_blank_std_linear_flank,
    check_blank_std_power_refused,
    check_blank_std_band_index_refused,
    check_blank_std_all_blank_refused,
    check_plot_parser_registration,
]
CHECKS_PLOT = [
    check_plot_bands_blank_group_renders,
    check_plot_bands_all_blank_group_refused,
    check_plot_curve_none_rel_pct_renders,
    check_cli_plot_end_to_end,
    check_cli_plot_explicit_outputs,
    check_cli_plot_refusals,
]


def run():
    """跑整套验收；返回实际跑过的检查数。没有 Matplotlib 时显式跳过渲染类检查。"""
    checks = list(CHECKS_BASE)
    skipped = 0
    if _have_mpl():
        checks += list(CHECKS_PLOT)
    else:
        skipped = len(CHECKS_PLOT)
    failures = []
    for check in checks:
        try:
            check()
        except Exception as exc:                    # 一条检查一条失败行，继续跑其余的
            first = (str(exc).splitlines() or [""])[0]
            failures.append("%s：%s: %s" % (check.__name__, type(exc).__name__, first))
    if failures:
        for line in failures:
            print("  ✗ %s" % line)
        raise SystemExit("test_ep4xa_acceptance %d/%d 通过（%d 个失败）"
                         % (len(checks) - len(failures), len(checks), len(failures)))
    if skipped:
        print("test_ep4xa_acceptance %d/%d 通过（matplotlib 缺失，跳过 %d 项渲染检查）"
              % (len(checks), len(checks), skipped))
    else:
        print("test_ep4xa_acceptance %d/%d 通过" % (len(checks), len(checks)))
    return len(checks)


if __name__ == "__main__":
    raise SystemExit(run())
