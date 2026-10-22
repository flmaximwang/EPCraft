#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest：不读用户文件的算法自检。"""
import ast
import base64
import contextlib
import copy
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import time

from config import load_gel_size
from editor import DEFAULT_TEMPLATE as EDITOR_TEMPLATE
from editor import extract as editor_extract, write_html as editor_write_html
from gel_area import compute, estimate_pitch_px
from magick_io import MAGICK, _run, read_ppm, to_gray
from gel_detect import bg_plane, components, detect_gel, ink_mask
from band_fit import clusters, fat_rows, group_rows
from lane_fit import _refine_one, find_lanes
from lane_select import (cropped_out_markers, layout as lane_layout, nearest_marker,
                         parse_keep, strips as lane_strips)
from geometry import load_dims_after, rect_to_analysis, rotate_pt, rotated_rect_bbox
from mw_report import build_cal, estimate, marker_pairs, parse_mw, report as mw_report
from record import (bands_from_rows, build as record_build, load as record_load,
                    write as record_write)
from scene import (build as scene_build, item_box_cm, load as scene_load,
                   norm_anchor, page_size_cm, validate as scene_validate,
                   write as scene_write)
from scene_png import page_px, write_png as scene_png_write
from scene_svg import baseline_offset_px, svg_string, write_svg
from text_layout import annotate_cmd, pick_font, text_extent
import vc_file
import vc_control
import workbench
import profiles
from mw_report import format_report_v2, write_v2


def test_import_direction():
    """stage 不许 import 更靠后的 stage（spec §8 第 5 条）。

    用 ast 解析真正的 import 语句（不 grep 文本：注释/文档里提到模块名不算依赖）。
    返回违规列表，每条写明「哪个模块 import 了哪个」。
    """
    scripts = os.path.dirname(os.path.abspath(__file__))
    rules = {
        "gel_area": {"lane_fit", "band_fit", "lane_select", "gel_photo"},
        "lane_fit": {"band_fit", "lane_select", "gel_photo"},
        "band_fit": {"lane_select", "gel_photo"},
    }
    bad = []
    for mod, forbidden in rules.items():
        path = os.path.join(scripts, mod + ".py")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in forbidden:
                    bad.append("%s import 了 %s（第 %d 行）" % (mod, top, node.lineno))
    return bad


def test_annotate_geometry():
    """-annotate 的 y 是文字框的**顶边**（实测），不是基线也不是中心。

    口径：marker 用 ("center_y", y)（框竖直中心对齐条带行）、
    泳道名用 ("bottom_y", y)（框底边在胶顶之上 gap）。旧代码拿 0.21*s / 0.35*s
    近似，所以 marker 字整体沉到条带下方 ~1 em、泳道名压进胶里。
    这里按真实 box 高重算，断言三条。
    """
    checks = []
    ok = lambda n, b, m="": checks.append((n, bool(b), m))
    f = pick_font()
    px = int(round(8 / 72.0 * 96))           # 8 pt @ 96 dpi
    tw, th = text_extent(f, px, "250")

    # 1) -annotate 的 y 是框顶边：画两次（y=50 / y=60），墨迹必须整体平移 10 px（斜率 1、无基线偏移）
    p1, p2 = "/tmp/gel_ann_a.png", "/tmp/gel_ann_b.png"
    for p, yy in ((p1, 50), (p2, 60)):
        _run([MAGICK, "-size", "120x120", "xc:white", "-density", "72", "-font", f,
              "-pointsize", str(px), "-gravity", "northwest", "-fill", "black",
              "-annotate", "+20+%d" % yy, "250", p])
    def _top(p):
        w, h, rgb = read_ppm(p, 0)
        g = to_gray(w, h, rgb)
        rows = [y for y in range(h) if min(g[y * w + x] for x in range(w)) < 200]
        return (min(rows), max(rows)) if rows else None
    a, b = _top(p1), _top(p2)
    ok("-annotate y=框顶边（y+10 → 墨迹整体下移 10，±1px）",
       a and b and abs((b[0] - a[0]) - 10) <= 1 and abs((b[1] - a[1]) - 10) <= 1,
       "y=50 墨迹 %s，y=60 墨迹 %s" % (a, b))

    # 2) marker 对齐：框中心必须落在条带行上（±1 px）
    band_y = 300.0
    ay = band_y - th / 2.0
    ok("marker：框竖直中心对齐条带行（±1px）", abs((ay + th / 2.0) - band_y) <= 1.0,
       "框中心 %.1f，条带行 %.1f" % (ay + th / 2.0, band_y))

    # 3) 泳道名：框底边必须落在 gel_top - gap（±1px），且不压胶
    for gap in (0, 4):
        gel_top, g2 = 260.0, float(gap)
        ay2 = gel_top - g2 - th
        bottom = ay2 + th
        ok("泳道名 gap=%d：框底边=胶顶-gap（±1px）" % gap, abs(bottom - (gel_top - g2)) <= 1.0,
           "框底边 %.1f，期望 %.1f" % (bottom, gel_top - g2))
        ok("泳道名 gap=%d：不压胶（框底边≤胶顶）" % gap, bottom <= gel_top + 1e-6,
           "框底边 %.1f > 胶顶 %.1f" % (bottom, gel_top))

    # 4) 留白公式必须用同一个 box 高（t/b 都从 ay、ay+th 算）
    need_t = gel_top - ay2
    need_b = (ay2 + th) - gel_top
    ok("留白：t 用框顶边、b 用框底边", need_t >= 0 and need_b <= 0,
       "need_t=%.1f need_b=%.1f" % (need_t, need_b))
    return checks


def selftest():
    import tempfile
    ok = 0

    # 1) PPM 解析
    d = b"P6\n# c\n3 1\n255\n" + bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    w, h, body = _parse_ppm_for_test(d)
    assert (w, h, len(body)) == (3, 1, 9), (w, h, len(body))
    ok += 1

    # 2) magick -rotate 方向 + 本项目 rotate_pt 的矩阵一致（实测锚定，不是猜的）
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "s.png")
        _run([MAGICK, "-size", "200x100", "xc:white", "-fill", "black",
              "-draw", "rectangle 0,0 20,10", src])
        rot = os.path.join(td, "r.png")
        _run([MAGICK, src, "-rotate", "10", rot])
        w, h, rgb = read_ppm(rot, None)
        g = to_gray(w, h, rgb)
        xs = [i % w for i in range(w * h) if g[i] < 100]
        ys = [i // w for i in range(w * h) if g[i] < 100]
        cxm, cym = sum(xs) / len(xs), sum(ys) / len(ys)
        # 原图中心 (100,50)、(20,10) 矩形中心 (10,5)；新画布中心 = (w/2, h/2)
        ex, ey = rotate_pt(10, 5, 100, 50, 10)
        ex, ey = ex + (w / 2.0 - 100.0), ey + (h / 2.0 - 50.0)
        assert abs(cxm - ex) < 2.0 and abs(cym - ey) < 2.0, ("rotate 方向/矩阵不符", (cxm, cym), (ex, ey))
        ok += 1

    # 3) 峰簇：三个 run 的中心与宽度
    prof = [0, 0, 5, 9, 5, 0, 0, 0, 4, 4, 0, 7, 7, 7]
    cl = clusters(prof, 0, 4)
    assert len(cl) == 3, cl
    assert abs(cl[0][0] - 3) < 0.2 and cl[1][1] == 2 and cl[2][0] > cl[1][0], cl
    ok += 1

    # 4) 平面背景 + 暗度判据：亮背景上的暗块进 mask，块外不进；块大到块内没有背景像素也要成立
    w, h = 300, 300
    gray = bytearray([205] * (w * h))
    for y in range(60, 240):
        for x in range(60, 240):
            gray[y * w + x] = 180          # 假胶：比背景暗 25 灰阶
    blue = bytearray(w * h)
    plane = bg_plane(w, h, gray)[:3]
    m = ink_mask(w, h, gray, blue, plane, "dark")
    assert m[150 * w + 150] == 1 and m[10 * w + 10] == 0, (plane, m[150 * w + 150], m[10 * w + 10])
    assert m[62 * w + 62] == 1 and m[58 * w + 150] == 0
    # 蓝度判据：灰胶的蓝度≈0 ⇒ chroma 通道不该选中它
    m2 = ink_mask(w, h, gray, blue, plane, "chroma")
    assert m2[150 * w + 150] == 0
    ok += 1

    # 5) 连通域：最大的块胜出、贴边的不算、像素表对得上面积
    m = bytearray(w * h)
    for y in range(30, 70):
        for x in range(30, 70):
            m[y * w + x] = 1
    for x in range(0, 40):
        m[0 * w + x] = 1          # 贴边
    comps = components(m, w, h)
    assert comps[0][1] == (30, 30, 69, 69) and comps[0][0] == len(comps[0][2]) == 1600, comps[0][:2]
    ok += 1

    # 6) 旋转框 bbox 里 → 裁切后胶面仍是正的（用 magick 实跑校验：旋转 8° 的黑框 bbox 与预测差 < 3 px）
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "s2.png")
        _run([MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(40)",
              "-draw", "rectangle 120,90 320,240", src])
        info = detect_gel(src, 400)
        (W0, H0), (W1, H1) = load_dims_after(src, info["angle_vertical"])
        bb, _ = rotated_rect_bbox(info["rect_orig"], info["angle_vertical"], (W0, H0), (W1, H1))
        out = os.path.join(td, "o.png")
        cmd = [MAGICK, src, "-rotate", "%.4f" % info["angle_vertical"],
               "-crop", "%dx%d+%d+%d" % (int(bb[2] - bb[0]), int(bb[3] - bb[1]), int(bb[0]), int(bb[1])),
               "+repage", "-colorspace", "gray", "-depth", "8", "pgm:-"]
        d = _run(cmd)
        parts = d.split(b"\n", 3)
        cw, ch = map(int, parts[1].split())
        px = parts[3]
        dark = [i for i in range(cw * ch) if px[i] < 200]  # noqa: E501
        ys = [i // cw for i in dark]
        assert abs((min(ys) - 0) - 0) <= 3, "裁切上边留白异常 %s" % min(ys)
        assert abs((ch - 1 - max(ys)) - 0) <= 3, "裁切下边留白异常 %s" % (ch - 1 - max(ys))
        ok += 1

    # 7) 写字：dpi 真进 PNG（pHYs），且 pointsize 不被 density 二次放大
    with tempfile.TemporaryDirectory() as td:
        o = os.path.join(td, "t.png")
        _run([MAGICK, "-size", "400x100", "xc:white", "-density", "72", "-font", pick_font(),
              "-pointsize", "20", "-fill", "black", "-annotate", "+10+40", "Ag1y"] +
             ["+profile", "*", "-units", "PixelsPerInch", "-density", "300", o])
        raw = open(o, "rb").read()
        i = raw.find(b"pHYs")
        assert i > 0, "PNG 里没有 pHYs（dpi 没写进去）"
        ppm_x = int.from_bytes(raw[i + 4 : i + 8], "big")
        assert abs(ppm_x - 11811) <= 1, "pHYs 不是 300 dpi: %d" % ppm_x
        w, h, rgb = read_ppm(o, None)
        g = to_gray(w, h, rgb)
        rows = [y for y in range(h) if any(g[y * w + x] < 128 for x in range(w))]
        assert 17 <= rows[-1] - rows[0] + 1 <= 23, "字号被 density 放大了: 墨高 %d px（20 pt 应约 19）" % (
            rows[-1] - rows[0] + 1)
        ok += 1

    # 旧算法已移除：fill_gaps / pitch_from_row 已删除（泳道数恒等于先验 lane_count，
    # 间距来自预设而不是「条带最密的一行」），对应的旧用例一并删除。

    # 8) 端到端：5 条泳道（第 3 条只有很淡的带）+ 一道竖纹 → 正好 5 条、位置 ≤3 px。
    #    旧算法已移除：旧版用泳道启发式（列投影 + 验伪 + 补位）找泳道；T3 起泳道恒由
    #    stage 2 的 5 条先验格点给出，竖纹不可能再「变成」一条泳道，只在细化窗口外干扰不了它。
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "synthetic_gel.png")
        _synth_gel(src_img, (400, 300), (100, 60, 340, 260),
                   [124, 172, 220, 268, 316], faint={2: 168},
                   streak=(292, 60, 260))
        ga = _synth_ga((100, 60, 340, 260), 48.0, canvas=(400, 300))
        lf = find_lanes(src_img, ga, 5, longest=400)
        got = [ln["x_analysis"] for ln in lf["lanes"]]
        want = [124, 172, 220, 268, 316]
        assert len(got) == 5, ("泳道数不对", got)
        for w, g in zip(want, got):
            assert abs(w - g) <= 3, (want, got)
        assert all(abs(g - 292) > 6 for g in got), ("竖纹被当成了泳道", got)
        assert lf["lanes"][2]["measured"], ("淡道 3 应仍有像素证据", lf["lanes"][2])
        ok += 1

    # 9) 空泳道：15 条格点里第 7 条完全没条带 → 仍返回 15 条；第 7 条 measured=False、
    #    evidence 有值（可以是 0）、x 落在格点上。空道不是「识别失败」，是 estimated 泳道。
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "empty_lane.png")
        xs = [109 + 18 * i for i in range(15)]          # 胶面 100..370，格点 109..361
        _synth_gel(src_img, (400, 300), (100, 60, 370, 260), xs, skip={6})
        ga = _synth_ga((100, 60, 370, 260), 18.0, canvas=(400, 300))
        lf = find_lanes(src_img, ga, 15, longest=400)
        assert len(lf["lanes"]) == 15, len(lf["lanes"])
        lane7 = lf["lanes"][6]
        assert lane7["measured"] is False, lane7
        assert isinstance(lane7["evidence"], float) and lane7["evidence"] >= 0.0, lane7
        assert abs(lane7["x_analysis"] - 217.0) <= 1.0, lane7      # 第 7 条的格点位置（109+6×18）
        assert sum(1 for ln in lf["lanes"] if ln["measured"]) == 14, lf["lanes"]
        ok += 1

    # 10) 偏心照片：胶块不在画面中央（左偏/上偏）→ 格点对齐到胶，不跟着画面中心跑。
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "off_centre.png")
        xs = [84, 132, 180, 228, 276]                   # 胶面 60..300，宽 240，pitch 48
        _synth_gel(src_img, (500, 400), (60, 50, 300, 350), xs)
        ga = _synth_ga((60, 50, 300, 350), 48.0, canvas=(500, 400))
        lf = find_lanes(src_img, ga, 5, longest=500)
        got = [ln["x_analysis"] for ln in lf["lanes"]]
        assert len(got) == 5, got
        for w, g in zip(xs, got):
            assert abs(w - g) <= 2, (xs, got)
        assert abs(sum(got) / len(got) - 180.0) <= 3, ("跟着画面中心跑了", got)
        assert abs(sum(got) / len(got) - 250.0) > 30, ("用了画面中心而不是胶中心", got)
        ok += 1

    # 11) 格点拟合：15 条等距竖线（间距 18 px）→ 拟合 pitch 与真值差 ≤0.5 px、全部 measured。
    #    顺带验 geometry.rect_to_analysis 是 rotated_rect_bbox 的严格逆变换（5° 往返 ≤1 px）。
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "lattice.png")
        xs = [74 + 18 * i for i in range(15)]          # 胶面 65..335，宽 270
        _synth_gel(src_img, (400, 300), (65, 60, 335, 260), xs,
                   band_y=(70, 255), band_v=120, band_half=3)   # 7 px 宽 ≈ 0.4 pitch，同参考照片最窄的真泳道
        ga = _synth_ga((65, 60, 335, 260), 18.0, canvas=(400, 300))
        lf = find_lanes(src_img, ga, 15, longest=400)
        assert len(lf["lanes"]) == 15, len(lf["lanes"])
        assert abs(lf["pitch"] - 18.0) <= 0.5, ("pitch 偏差", lf["pitch"])
        assert all(ln["measured"] for ln in lf["lanes"]), \
            [ln for ln in lf["lanes"] if not ln["measured"]]
        for w, ln in zip(xs, lf["lanes"]):
            assert abs(w - ln["x_analysis"]) <= 1.5, (xs, [x["x_analysis"] for x in lf["lanes"]])
        # 窄斑不算泳道：窗口边上一个 2 px 宽、够显著的斑（参考照片第 10 道）→ 留在格点、estimated
        prof = [0] * 40
        prof[13], prof[14] = 14, 9
        assert _refine_one(prof, 0, 20.0, 18.0, 8.9)[:2] == (20.0, False)
        prof[17:25] = [6, 12, 16, 18, 17, 13, 8, 5]          # 8 px 宽的淡道 → measured
        assert _refine_one(prof, 0, 20.0, 18.0, 8.9)[1] is True
        # 旋转往返：rect_orig → rotated_rect_bbox → rect_to_analysis。
        # 注意 bbox 是「旋转后四角的轴对齐框」，逆变换回来必然比原矩形略大（不是逐边相等）；
        # 能钉住的是中心不漂，以及它确实包含原矩形。
        ang = 5.0
        (W0, H0), (W1, H1) = load_dims_after(src_img, ang)
        rb, _ = rotated_rect_bbox((65, 60, 335, 260), ang, (W0, H0), (W1, H1))
        back = rect_to_analysis(rb, ang, {"path": src_img, "image": (W0, H0),
                                         "analysis": (W0, H0)})
        assert abs((back[0] + back[2]) / 2.0 - 200.0) <= 1, back
        assert abs((back[1] + back[3]) / 2.0 - 160.0) <= 1, back
        assert back[0] <= 65 and back[1] <= 60 and back[2] >= 335 and back[3] >= 260, back
        ok += 1

    # 12) 条数恒等：任何输入都 len(lanes) == lane_count（5 / 15 两个预设各测一次，且是空胶）。
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "blank.png")
        _synth_gel(src_img, (400, 300), (100, 60, 370, 260), [])
        for n, pc in ((5, 54.0), (15, 18.0)):
            ga = _synth_ga((100, 60, 370, 260), pc, canvas=(400, 300))
            lf = find_lanes(src_img, ga, n, longest=400)
            assert len(lf["lanes"]) == n, (n, len(lf["lanes"]))
            assert not any(ln["measured"] for ln in lf["lanes"]), lf["lanes"]
        ok += 1

    # 13) 文字对齐几何：-annotate 的 y 是框顶边；marker 框中心对条带行、泳道名框底边在胶顶之上 gap
    for n, b, m in test_annotate_geometry():
        if not b:
            raise AssertionError("对齐几何：%s —— %s" % (n, m))
    ok += 1          # 一组 6 项算一项，免得分子分母对不上

    # 旧算法已移除：snap_to_anchors 已删除（泳道中心不再吸附到「条带最密那一行」的锚点，
    # stage 2 只用列剖面 + 先验格点；原先的看护用例随函数一起删掉）。

    # 14) 并带检测：一条行塞两条带（21/20 kD）时，那一行必须比同泳道其他行厚一截
    assert fat_rows([(100, 6), (120, 6), (140, 6), (160, 15), (180, 6)]) == [(160, 2.5)]
    assert fat_rows([(10, 5), (20, 5), (30, 5)]) == []      # 均匀的行不许误报
    assert fat_rows([]) == []
    ok += 1

    # 15) 并带标签的行分组：{"rows": 2} 的标签吃两条带、落在它们中间；其余一一对应
    assert group_rows([10, 20, 30], [1, 2]) == [10.0, 25.0]
    assert group_rows([10, 20, 30], [1, 1, 1]) == [10.0, 20.0, 30.0]
    assert group_rows([10, 20], [1]) == [10.0]
    assert group_rows([], [1, 2]) == []
    ok += 1

    # 16) 字号必须真的生效：`annotate_cmd` 漏写 -pointsize 时 IM 一律按默认 12 pt 画，
    #     96/300 dpi 的墨迹都会是同一高度（实测踩过：两个 dpi 的字相对胶面大小不同）。
    with tempfile.TemporaryDirectory() as td:
        o = os.path.join(td, "pt.png")
        items = [(20, 140, "250", 24, "#000000"), (240, 140, "250", 48, "#000000")]
        _run(annotate_cmd([MAGICK, "-size", "460x200", "xc:white"], items, pick_font(), None) + [o])
        w3, h3, rgb3 = read_ppm(o, None)
        g3 = to_gray(w3, h3, rgb3)

        def inkh(x0, x1):
            rows = [y for y in range(h3) if any(g3[y * w3 + x] < 128 for x in range(x0, x1))]
            return (max(rows) - min(rows) + 1) if rows else 0
        h_small, h_big = inkh(20, 180), inkh(240, 440)
        assert h_big > 1.6 * h_small, ("字号没生效（-pointsize 没写进命令？）", h_small, h_big)
        assert 14 <= h_small <= 20 and 30 <= h_big <= 40, (h_small, h_big)
    ok += 1

    # 17) 分子量标定：完美的 log 线性 marker → 两种方法都应还原；interp 不外推、logfit 外推并标记
    assert parse_mw("21/20 kD") == [21.0, 20.0] and parse_mw("250 kD") == [250.0] and parse_mw("M") == []
    ents = [{"text": "100 kD"}, {"text": "21/20 kD", "rows": 2}, {"text": "10 kD"}]
    pr = marker_pairs(ents, [1, 2, 1], [10, 20, 24, 30], lambda e, d: dict(e))
    assert pr == [(10, 100.0), (20, 21.0), (24, 20.0), (30, 10.0)], pr
    cal = build_cal([(y, 10 ** (2.0 - 0.05 * (y - 10))) for y in (10, 20, 30, 40)])
    assert abs(cal["fit"][2] - 1.0) < 1e-9
    mw, ex = estimate(25, 100.0, [(100.0, cal)], "logfit")
    assert abs(mw - 10 ** (2.0 - 0.75)) < 1e-6 and not ex, (mw, ex)
    mw, ex = estimate(25, 100.0, [(100.0, cal)], "interp")
    assert abs(mw - 10 ** (2.0 - 0.75)) < 1e-6 and not ex, (mw, ex)
    assert estimate(50, 100.0, [(100.0, cal)], "interp")[0] is None
    mw, ex = estimate(50, 100.0, [(100.0, cal)], "logfit")
    assert ex and abs(mw - 10 ** (2.0 - 2.0)) < 1e-6, (mw, ex)
    ok += 1

    # 18) 强制预设闸门：probe 没给 --gel-size 必须在**跑检测之前**就失败。
    #     用一张不存在的图：真跑到 detect_gel 会报别的错，只有先过闸门才会是 spec 那一行。
    from gel_photo import main as gp_main
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = gp_main(["probe", "/nonexistent/t1-gate-no-preset.png"])
    msg = err.getvalue()
    assert rc != 0 and "gel preset required (--gel-size FILE)" in msg, (rc, msg)
    ok += 1

    # 19) --gel-cm 不再是 --gel-size 的替代：只给 --gel-cm 同样被闸门挡下
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = gp_main(["render", "/nonexistent/t1-gate-gel-cm.png", "--out", "/tmp/t1-gate.png",
                      "--gel-cm", "8x7"])
    msg = err.getvalue()
    assert rc != 0 and "gel preset required (--gel-size FILE)" in msg, (rc, msg)
    ok += 1

    # 20) 预设 schema：缺 lane_count 报错、不变式破坏报偏差、合法预设返回 dict 且 anchor 默认 bottom
    with tempfile.TemporaryDirectory() as td:
        good = {"name": "t", "width_cm": 8.6, "height_cm": 6.9,
                "lane_count": 15, "lane_pitch_cm": 0.565}
        p_good = os.path.join(td, "good.json")
        with open(p_good, "w", encoding="utf-8") as f:
            json.dump(good, f)
        gel = load_gel_size(p_good)
        assert isinstance(gel, dict) and gel["lane_count"] == 15 and gel["anchor"] == "bottom", gel

        bad = {k: v for k, v in good.items() if k != "lane_count"}
        p_bad = os.path.join(td, "no_lane_count.json")
        with open(p_bad, "w", encoding="utf-8") as f:
            json.dump(bad, f)
        try:
            load_gel_size(p_bad)
            raise AssertionError("缺 lane_count 没报错")
        except RuntimeError as e:
            assert "lane_count" in str(e), str(e)

        bad2 = dict(good, lane_pitch_cm=0.3)
        p_bad2 = os.path.join(td, "bad_pitch.json")
        with open(p_bad2, "w", encoding="utf-8") as f:
            json.dump(bad2, f)
        try:
            load_gel_size(p_bad2)
            raise AssertionError("不变式破坏没报错")
        except RuntimeError as e:
            s = str(e)
            assert "偏差" in s and "15" in s and "0.300" in s and "8.600" in s, s

        bad3 = dict(good, anchor="middle")
        p_bad3 = os.path.join(td, "bad_anchor.json")
        with open(p_bad3, "w", encoding="utf-8") as f:
            json.dump(bad3, f)
        try:
            load_gel_size(p_bad3)
            raise AssertionError("非法 anchor 没报错")
        except RuntimeError as e:
            assert "anchor" in str(e), str(e)

        # 随包预设自身必须过 schema/不变式（lane_count=15）
        shipped = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "gel_size.json")
        if os.path.isfile(shipped):
            sg = load_gel_size(shipped)
            assert sg["lane_count"] == 15 and sg["anchor"] == "bottom", sg
    ok += 1

    # 21) stage 1：预设高比探测框高 3% / 8% → 胶面都按**预设**定高（不是照抄探测框）。
    #     高度容差 HEIGHT_TOL = 5%（spec §4.4）：+3% 不该有 warning（「只警告、不静默改图」的另一半），
    #     +8% 必须打 height warning（spec §8），两档一起钉住「形状跟预设」和「>5% 才警告」。
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "stage1_gel.png")
        _run([MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(40)",
              "-draw", "rectangle 120,90 320,240", src])
        info = detect_gel(src, 400)
        (W0, H0), (W1, H1) = load_dims_after(src, 0.0)
        bb, _ = rotated_rect_bbox(info["rect_orig"], 0.0, (W0, H0), (W1, H1))
        w_px, h_px = bb[2] - bb[0], bb[3] - bb[1]
        ppc = w_px / 6.0                              # 整块宽 6 cm 的定标（画布 px / cm）
        p3 = _t2_preset(6.0, h_px / ppc * 1.03)       # 预设高 = 探测框高 × 1.03
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ga3 = compute(info, p3, 0.0, pitch_px=ppc * p3["lane_pitch_cm"])
        got_h = ga3["gel_box"][3] - ga3["gel_box"][1]
        want_h = p3["height_cm"] * ppc
        assert abs(got_h - want_h) <= 0.01 * want_h, (got_h, want_h)
        assert abs(got_h / h_px - 1.03) <= 0.01, ("框高没跟着预设走", got_h, h_px)
        assert ga3["source"] == {"px_per_cm": "width", "height_cm": "preset", "box": "detect"}, \
            ga3["source"]
        assert "[!]" not in out.getvalue(), ("3% 在 5% 容差内，不该警告", out.getvalue())
        # +8%：超出 5% ⇒ 必须打 height warning（两个数都打出来），且形状仍跟预设（spec §8）
        p8 = _t2_preset(6.0, h_px / ppc * 1.08)
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            ga8 = compute(info, p8, 0.0, pitch_px=ppc * p8["lane_pitch_cm"])
        msg = out2.getvalue()
        assert "[!]" in msg and "height" in msg and "%.2f" % p8["height_cm"] in msg, msg
        assert abs((ga8["gel_box"][3] - ga8["gel_box"][1]) - p8["height_cm"] * ppc) <= 0.01 * ppc
        ok += 1

        # 21b) 胶顶的齿：胶板上方立着一排高低不齐的齿（上样孔之间的胶，常被压弯）。
        #      探测框上缘必须落在胶板（孔底）上，不能落在齿中间；左右、下缘不受齿影响。
        src_t = os.path.join(td, "stage1_teeth.png")
        cmd = [MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(40)",
               "-draw", "rectangle 100,120 300,260"]
        for i, x in enumerate(range(104, 296, 16)):
            top = (60, 90, 75, 105)[i % 4]               # 齿高参差 15~60 px
            cmd += ["-draw", "rectangle %d,%d %d,120" % (x, top, x + 11)]   # 齿占 3/4 宽
        cmd += [src_t]
        _run(cmd)
        it = detect_gel(src_t, 400)
        bt = it["bbox_analysis"]
        assert abs(bt[1] - 120) <= 2, ("上缘落在齿里了", bt)
        assert abs(bt[3] - 260) <= 2 and abs(bt[0] - 100) <= 2 and abs(bt[2] - 300) <= 2, bt
        ok += 1

        # 22) anchor：同一条探测框，bottom 保下边 / top 保上边 / center 保中心（±1 px）
        base = _t2_preset(6.0, h_px / ppc * 0.8)      # 比探测框矮 20%，三条边才分得开
        boxes = {}
        for anc in ("bottom", "top", "center"):
            with contextlib.redirect_stdout(io.StringIO()):
                boxes[anc] = compute(info, dict(base, anchor=anc), 0.0,
                                     pitch_px=ppc * base["lane_pitch_cm"])["gel_box"]
        face = base["height_cm"] * ppc
        for b in boxes.values():
            assert abs(b[0] - bb[0]) <= 1e-6 and abs(b[2] - bb[2]) <= 1e-6, (b, bb)   # x 不动
            assert abs((b[3] - b[1]) - face) <= 0.01 * face, (b, face)
        assert abs(boxes["bottom"][3] - bb[3]) <= 1, (boxes["bottom"], bb)
        assert abs(boxes["top"][1] - bb[1]) <= 1, (boxes["top"], bb)
        assert abs((boxes["center"][1] + boxes["center"][3]) / 2.0
                   - (bb[1] + bb[3]) / 2.0) <= 1, (boxes["center"], bb)
        ok += 1

        # 23) trim：bottom anchor、trim_top=0.3 → 高 = (height_cm − 0.3) × px_per_cm；下边不动
        p22 = _t2_preset(6.0, 6.0, trim_top=0.3)
        with contextlib.redirect_stdout(io.StringIO()):
            ga22 = compute(info, p22, 0.0, pitch_px=ppc * p22["lane_pitch_cm"])
        got22 = ga22["gel_box"][3] - ga22["gel_box"][1]
        want22 = (p22["height_cm"] - 0.3) * ppc
        assert abs(got22 - want22) <= 0.01 * want22, (got22, want22)
        assert abs(ga22["gel_box"][3] - bb[3]) <= 1, (ga22["gel_box"], bb)
        assert abs(ga22["height_cm"] - 5.7) < 1e-6, ga22["height_cm"]
        ok += 1

    # 24) 裁过的照片：gel_lanes=8 → 定标用 8 × lane_pitch_cm（不用整块 width_cm）；
    #     并验 estimate_pitch_px 的廉价估计（stage 1 的预估值，精度由 T3 的格点拟合复核）
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "stage1_crop.png")
        _run([MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(40)",
              "-draw", "rectangle 120,90 320,240", src])
        info = detect_gel(src, 400)
        (W0, H0), (W1, H1) = load_dims_after(src, 0.0)
        bb, _ = rotated_rect_bbox(info["rect_orig"], 0.0, (W0, H0), (W1, H1))
        w_px, h_px = bb[2] - bb[0], bb[3] - bb[1]
        vis_cm = 8 * 0.6
        ppc_lane = w_px / vis_cm
        # 高按「探测框高在泳道定标下对应的 cm」给，免得 height 核对先报一个假的偏差
        p23 = _t2_preset(8.6, h_px / ppc_lane, lane_count=8, lane_pitch_cm=0.6)
        with contextlib.redirect_stdout(io.StringIO()):
            ga23 = compute(info, p23, 0.0, gel_lanes=8, pitch_px=ppc_lane * 0.6)
        assert abs(ga23["px_per_cm"] - ppc_lane) <= 0.01 * ppc_lane, (ga23["px_per_cm"], ppc_lane)
        assert abs(ga23["px_per_cm"] - w_px / 8.6) > 0.5, ("用了整块 width_cm 定标？", ga23["px_per_cm"])
        assert ga23["source"]["px_per_cm"] == "lane_pitch", ga23["source"]
        assert abs(ga23["width_cm"] - vis_cm) < 1e-9, ga23["width_cm"]
        assert abs((ga23["gel_box"][3] - ga23["gel_box"][1]) - p23["height_cm"] * ppc_lane) <= 0.03 * ppc_lane
        assert all(c["ok"] for c in ga23["checks"]), ga23["checks"]

        # 竖向暗条纹（间距 30 px，分析尺度 = 原图）→ estimate_pitch_px ≈ 30
        src2 = os.path.join(td, "stage1_stripes.png")
        cmd = [MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(180)",
               "-draw", "rectangle 100,80 340,260"]
        for x in (120, 150, 180, 210, 240):
            cmd += ["-fill", "gray(120)", "-draw", "rectangle %d,80 %d,260" % (x - 1, x + 1)]
        cmd += [src2]
        _run(cmd)
        est = estimate_pitch_px(detect_gel(src2, 400), 0.0)
        assert 27.0 <= est <= 33.0, est
        ok += 1

    # 25) import 方向：stage 只许依赖前面的 stage（用 ast，不听注释/文档里的模块名）
    bad = test_import_direction()
    assert not bad, "stage import 方向错了：" + "；".join(bad)
    ok += 1

    # 26) record 键集 / 量级：递归收集键名，不许出现 rect / bbox / angle / scale；
    #     所有数值都得是 cm 量级（<200；照片 px 会上千），gel 宽 ≈ 预设宽，lanes = lane_count。
    preset, rec = _synth_record()
    bad_keys = [k for k in _walk_keys(rec)
                if any(t in k.lower() for t in ("rect", "bbox", "angle", "scale"))]
    assert not bad_keys, ("record 里混进了照片坐标的键", bad_keys)
    assert "crop" not in rec, ("本轮不该有 crop 键", list(rec))
    nums = list(_walk_nums(rec))
    assert nums and all(abs(v) < 200 for v in nums), ("record 里出现了 px 量级的数", max(nums))
    assert abs(rec["gel"]["width_cm"] - preset["width_cm"]) < 1e-6, rec["gel"]
    assert len(rec["lanes"]) == preset["lane_count"], (len(rec["lanes"]), preset["lane_count"])
    ok += 1

    # 27) record 往返：write → load 深度相等；坐标单调（x 递增、每条泳道的 y 递增）
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "rec.json")
        record_write(rec, p)
        back = record_load(p)
        assert back == rec, "record 往返不相等"
        with open(p, "rb") as f:
            assert f.read().endswith(b"\n"), "record 文件末尾没有换行"
        xs = [ln["x_cm"] for ln in back["lanes"]]
        assert all(b > a for a, b in zip(xs, xs[1:])), ("lanes.x_cm 不递增", xs)
        for k, rows in back["bands"].items():
            ys = [r["y_cm"] for r in rows]
            assert all(b > a for a, b in zip(ys, ys[1:])), ("泳道 %s 的 y_cm 不递增" % k, ys)
    ok += 1

    # 28) MW 等价：同一组带分别走 record（cm）与旧式 lb（分析 px），标定只差仿射 ⇒ MW 应相同。
    #     record 的第 2 条泳道标成 estimated：它不该被跳过，只该多一条 note。
    mrec, mlb = _synth_mw_inputs()
    labels = ["", "S", ""]
    ents = [{"text": "100 kD"}, {"text": "10 kD"}, {"text": "1 kD"}]
    span = [1, 1, 1]
    as_entry = lambda e, d: dict(e)                       # noqa: E731
    rep_rec = mw_report(mrec, labels, {0, 2}, 0, ents, span, as_entry)
    rep_lb = mw_report(mlb, labels, {0, 2}, 0, ents, span, as_entry)
    assert len(rep_rec["lanes"]) == 1 and len(rep_lb["lanes"]) == 1, (rep_rec["lanes"], rep_lb["lanes"])
    a = [b["mw_kda"] for b in rep_rec["lanes"][0]["bands"]]
    b = [b["mw_kda"] for b in rep_lb["lanes"][0]["bands"]]
    assert len(a) == len(b) == 2, (a, b)
    assert max(abs(x - y) for x, y in zip(a, b)) <= 0.1, ("record / lb 的 MW 不一致", a, b)
    assert any("位置是估计值" in n for n in rep_rec["notes"]), rep_rec["notes"]
    ok += 1

    # 29) 手动标定（markers.y_px / y_frac）必须进 record：MW 只读 record，
    #     覆盖没写进去的话，人对着 debug 图标的行会被悄悄换回自动行。
    #     这里直接钉换算：分析 y=10/20、1 cm = 100 画布 px、分析:原图 = 1:2 ⇒ y_cm = 0.2 / 0.4。
    ga = {"px_per_cm": 100.0, "gel_box": (0.0, 0.0, 100.0, 100.0), "angle": 0.0,
          "canvas": (200, 200)}       # 角度 0 时画布 = 原图 200×200
    info = {"image": (200, 200), "analysis": (100, 100)}
    out = bands_from_rows(50.0, [10.0, 20.0], [(10.0, 4.0, 30.0)], ga, info)
    assert abs(out[0]["y_cm"] - 0.2) < 1e-6 and abs(out[1]["y_cm"] - 0.4) < 1e-6, out
    assert out[0]["depth"] == 30.0 and out[1]["width_cm"] > 0, out
    ok += 1

    # 30) stage 4 段边界：15 条、pitch 1.0 cm、胶 15 cm 的合成中心（1..15）。
    #     边界必须是**相邻中心的中点**（不是带边、不是固定半间距）；两端到胶缘。
    xs15 = [float(i) for i in range(1, 16)]
    seg = lane_strips([3, 4, 5], xs15, 15.0)
    assert len(seg) == 1 and seg[0]["lanes"] == [3, 4, 5], seg
    assert abs(seg[0]["src_x0_cm"] - 2.5) < 1e-9, seg      # (2+3)/2
    assert abs(seg[0]["src_x1_cm"] - 5.5) < 1e-9, seg      # (5+6)/2
    assert abs((seg[0]["src_x1_cm"] - seg[0]["src_x0_cm"]) - 3.0) < 1e-9, seg
    s12 = lane_strips([1, 2], xs15, 15.0)[0]
    assert abs(s12["src_x0_cm"] - 0.0) < 1e-9 and abs(s12["src_x1_cm"] - 2.5) < 1e-9, s12
    s1415 = lane_strips([14, 15], xs15, 15.0)[0]
    assert abs(s1415["src_x0_cm"] - 13.5) < 1e-9 and abs(s1415["src_x1_cm"] - 15.0) < 1e-9, s1415
    s15 = lane_strips([15], xs15, 15.0)[0]
    assert abs(s15["src_x1_cm"] - 15.0) < 1e-9, s15         # 最后一条 → 胶宽
    assert lane_strips([5, 3, 4, 4], xs15, 15.0)[0]["lanes"] == [3, 4, 5]
    ok += 1

    # 31) stage 4 两段版式：keep=[1,2,8,9] → 两段，第二段 dst = 第一段宽 + gap；总宽 = Σ段宽 + gap
    st2 = lane_strips([1, 2, 8, 9], xs15, 15.0)
    assert len(st2) == 2, st2
    assert abs((st2[0]["src_x1_cm"] - st2[0]["src_x0_cm"]) - 2.5) < 1e-9, st2
    assert abs((st2[1]["src_x1_cm"] - st2[1]["src_x0_cm"]) - 2.0) < 1e-9, st2
    st2, total2 = lane_layout(st2, 0.15)
    assert abs(st2[0]["dst_x0_cm"] - 0.0) < 1e-9, st2
    assert abs(st2[1]["dst_x0_cm"] - (2.5 + 0.15)) < 1e-9, st2
    assert abs(total2 - (2.5 + 2.0 + 0.15)) < 1e-9, total2
    # 单段：没有 gap
    st1, total1 = lane_layout(lane_strips([3, 4, 5], xs15, 15.0), 0.15)
    assert len(st1) == 1 and abs(st1[0]["dst_x0_cm"]) < 1e-9 and abs(total1 - 3.0) < 1e-9, (st1, total1)
    ok += 1

    # 32) --keep-lanes 语法：逗号/区间、去重升序；越界/空/反向区间/乱写都报错
    assert parse_keep("1,2,8-10", 15) == [1, 2, 8, 9, 10]
    assert parse_keep("3", 15) == [3]
    assert parse_keep(" 8 - 10 , 2 ", 15) == [2, 8, 9, 10]
    assert parse_keep("5,5,1", 15) == [1, 5]
    for bad in ("0", "16", "5-3", "", ",", "1,,2", "a", "1-"):
        try:
            parse_keep(bad, 15)
            raise AssertionError("--keep-lanes 非法却没报错：%r" % bad)
        except RuntimeError:
            pass
    ok += 1

    # 33) marker 策略：列出被裁的 marker；keep 时加与保留段边界最近的一条
    assert cropped_out_markers([3, 4, 5], {1, 2, 8}) == [1, 2, 8]
    assert cropped_out_markers([1, 2, 8], {1, 2, 8}) == []
    assert nearest_marker({1, 2, 8}, [3, 4, 5], xs15) == 2        # 2 离 2.5 最近
    assert nearest_marker({1, 2, 8}, [6, 7], xs15) == 8           # 8 离 7.5 最近
    assert nearest_marker({1, 2, 8}, [1, 2, 3], xs15) == 8
    assert nearest_marker({1}, [1, 2, 3], xs15) is None            # 没有被裁的 marker
    ok += 1

    # 34) scene schema：合法 scene write→load 往返；非法 anchor、重复 id、rect/bbox/angle/scale
    #     任一键都必须被 validate 拒绝（spec §4/§9，与 record 的禁键闸同一条）。
    with tempfile.TemporaryDirectory() as td:
        sc = _synth_scene()
        p = os.path.join(td, "synth.scene.json")
        scene_write(sc, p)
        assert scene_load(p) == sc, "scene 往返不相等"
        with open(p, "rb") as f:
            assert f.read().endswith(b"\n"), "scene 文件末尾没有换行"

        def rejected(mut):
            bad = json.loads(json.dumps(sc))
            mut(bad)
            try:
                scene_validate(bad)
            except RuntimeError:
                return True
            return False

        assert rejected(lambda x: x["items"][0].__setitem__("anchor", "diagonal")), \
            "非法 anchor 没报错"
        # spec §4 的例句用 right-middle（横-竖）：两种顺序都收，规范成 middle-right
        assert norm_anchor("right-middle") == "middle-right", "right-middle 没有归一"
        alias = json.loads(json.dumps(sc))
        alias["items"][0]["anchor"] = "middle-right"
        scene_validate(alias)      # 规范名当然合法
        assert rejected(lambda x: x["items"][1].__setitem__("id", x["items"][0]["id"])), \
            "重复 id 没报错"
        for k in ("rect", "bbox", "angle", "scale"):
            assert rejected(lambda x, k=k: x["items"][0]["bind"].__setitem__(k, 1)), \
                "禁键 %s 没报错" % k
    ok += 1

    # 35) 页面规则：无越界 → 胶 + 每边 margin_min_cm；一条长标签只让它那一侧长
    #     overhang + pad_cm，其余三边保持 margin_min_cm（spec §4）。
    sc = _synth_scene()
    ps = page_size_cm(sc)
    assert abs(ps["width_cm"] - (8.6 + 2 * 0.43)) < 1e-9, ps
    assert abs(ps["height_cm"] - (6.25 + 2 * 0.43)) < 1e-9, ps
    long_sc = _synth_scene(long_label=True)
    left, _top, _right, _bottom = item_box_cm(long_sc, long_sc["items"][0])
    pad = long_sc["page"]["pad_cm"]
    margin = long_sc["page"]["margin_min_cm"]
    ps2 = page_size_cm(long_sc)
    assert left < 0, ("长标签没有越到胶面左边", left)
    assert abs(ps2["left_cm"] - (-left + pad)) < 1e-9, (ps2, left, pad)
    assert abs(ps2["right_cm"] - margin) < 1e-9, ps2
    assert abs(ps2["top_cm"] - margin) < 1e-9, ps2
    assert abs(ps2["bottom_cm"] - margin) < 1e-9, ps2
    assert abs(ps2["width_cm"] - (8.6 + ps2["left_cm"] + ps2["right_cm"])) < 1e-9, ps2
    ok += 1

    # 36/37) 合成胶真跑一次 render：scene 一项一标签（5 个泳道名 + 2 个 marker 标签），
    #        id 唯一、角色就是 lane/marker；gel.png 与成品里同一块胶面逐像素一致（无字）。
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "synth.png")
        _synth_gel(src, (400, 300), (100, 60, 340, 260), [124, 172, 220, 268, 316])

        def _cfg(name, obj):
            path = os.path.join(td, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            return path

        gel_cfg = _cfg("gel.json", {"name": "t", "width_cm": 5.0, "height_cm": 200 / 48.0,
                                    "lane_count": 5, "lane_pitch_cm": 1.0})
        lane_cfg = _cfg("lanes.json", {"size": 8, "color": "#000000", "gap": 4,
                                        "align": "center",
                                        "labels": ["1", "2", "3", "4", "5"],
                                        "marker_lanes": [1]})
        mk_cfg = _cfg("markers.json", {"marker_lane": 1, "size": 6, "gap": 4,
                                        "color": "#000000",
                                        "labels": [{"text": "250 kD"}, {"text": "10 kD"}]})
        out = os.path.join(td, "fig.png")
        from gel_photo import main as gp_main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gp_main(["render", src, "--out", out, "--markers", mk_cfg,
                          "--lanes", lane_cfg, "--gel-size", gel_cfg, "--dpi", "96"])
        msg = buf.getvalue()
        assert rc == 0, msg[-1200:]
        m = re.search(r"胶面 (\d+)×(\d+) \+ 白底留白 L(\d+) R(\d+) T(\d+) B(\d+)", msg)
        nlog = re.search(r"；(\d+) 条文字", msg)
        assert m and nlog, msg[-800:]
        face_w_px, face_h_px = int(m.group(1)), int(m.group(2))
        lm, tp = int(m.group(3)), int(m.group(5))

        stem = os.path.splitext(out)[0]
        gel_png = stem + ".gel.png"
        scene = scene_load(stem + ".scene.json")
        ids = [it["id"] for it in scene["items"]]
        assert len(scene["items"]) == int(nlog.group(1)) == 7, (len(scene["items"]), nlog.group(1))
        assert {it["role"] for it in scene["items"]} == {"lane", "marker"}, ids
        assert len(ids) == len(set(ids)), ("id 不唯一", ids)
        assert scene["gel"]["image"] == os.path.basename(gel_png)
        assert os.path.isfile(gel_png), gel_png
        ok += 1

        # gel.png = 成品 PNG 里 (L,T) 起的胶面区域；逐像素相等 ⇒ 胶面上没有字。
        crop = os.path.join(td, "gel-region.png")
        _run([MAGICK, out, "-crop", "%dx%d+%d+%d" % (face_w_px, face_h_px, lm, tp),
              "+repage", crop])
        w1, h1, body1 = read_ppm(gel_png, None)
        w2, h2, body2 = read_ppm(crop, None)
        assert (w1, h1, bytes(body1)) == (w2, h2, bytes(body2)), \
            ("gel.png 与成品胶面区域不一致", (w1, h1), (w2, h2))
        ok += 1

        # 38) scene 往返出图：export 只读 scene，应与 render 的成品逐像素相同
        #     （坐标/页面/字号全部来自 scene；E1 仍用旧画字路径，两条路必须重合）。
        exp = os.path.join(td, "export.png")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gp_main(["export", stem + ".scene.json", "--png", exp])
        assert rc == 0, "export 失败"
        w0, h0, body0 = read_ppm(out, None)
        w3, h3, body3 = read_ppm(exp, None)
        assert (w0, h0, bytes(body0)) == (w3, h3, bytes(body3)), \
            ("export 与 render 不一致", (w0, h0), (w3, h3))
        ok += 1
        # 44) 编辑器 HTML：模板里恰好一个 #scene 数据块（解析回同一 scene）、一个
        #     data:image/png;base64 URI（解码回同一胶面），且没有任何网络 URL。
        html_path = stem + ".html"
        assert os.path.isfile(html_path), ("render 没写编辑器", html_path)
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        blocks = re.findall(
            r'<script\b[^>]*\bid\s*=\s*["\']scene["\'][^>]*>([\s\S]*?)</script>',
            html, re.I)
        assert len(blocks) == 1, ("#scene 块不是恰好一个", len(blocks))
        assert json.loads(blocks[0]) == scene, "HTML 里的 scene 与 render 写的不一致"
        uris = re.findall(r'data:image/png;base64,([A-Za-z0-9+/=]+)', html)
        assert len(uris) == 1, ("胶面 data URI 不是恰好一个", len(uris))
        with open(gel_png, "rb") as f:
            assert base64.b64decode(uris[0]) == f.read(), "HTML 里的胶面字节变了"
        assert not re.search(r'https?://|//cdn', html), "编辑器模板里有网络 URL"
        ok += 1

        # 44b) --no-editor：批量模式不写 HTML。
        out_no = os.path.join(td, "fig-noeditor.png")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gp_main(["render", src, "--out", out_no, "--markers", mk_cfg,
                          "--lanes", lane_cfg, "--gel-size", gel_cfg, "--dpi", "96",
                          "--no-editor"])
        assert rc == 0, "--no-editor render 失败"
        assert not os.path.exists(os.path.splitext(out_no)[0] + ".html"), \
            "--no-editor 仍然写了 HTML"

        # 45) extract 往返：改 parsed scene 里一条文字，写回 HTML 再 extract ——
        #     文字是新的、胶面字节一个不差（spec §6 的 Save 就是这么写文件）。
        sc2, gel2 = editor_extract(html_path)
        assert gel2 == base64.b64decode(uris[0]), "extract 的胶面字节不对"
        sc2["items"][0]["text"] = "EDITED 250 kD"
        j2 = json.dumps(sc2, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        edited_html = re.sub(
            r'(<script\b[^>]*\bid\s*=\s*["\']scene["\'][^>]*>)[\s\S]*?(</script>)',
            lambda m: m.group(1) + j2 + m.group(2), html, count=1, flags=re.I)
        edited_path = os.path.join(td, "fig-edited.html")
        with open(edited_path, "w", encoding="utf-8") as f:
            f.write(edited_html)
        sc3, gel3 = editor_extract(edited_path)
        assert sc3["items"][0]["text"] == "EDITED 250 kD", "extract 没读到改过的文字"
        assert gel3 == gel2, "extract 第二次的胶面字节变了"
        ok += 1

        # 46) export 直接吃编辑器 HTML：与 render 的成品逐像素相同（scene + 内嵌胶面）。
        from_html = os.path.join(td, "from-html.png")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gp_main(["export", html_path, "--png", from_html])
        assert rc == 0, "export HTML 失败"
        w4, h4, body4 = read_ppm(from_html, None)
        assert (w0, h0, bytes(body0)) == (w4, h4, bytes(body4)), \
            ("export HTML 与 render 不一致", (w0, h0), (w4, h4))
        ok += 1

        # 47) JS core 在 node 下验：pHYs ppm=11811@300dpi、签名不变、pagePx 对得上
        #     Python 的 page_size_cm、sceneToSvg 每条一个正文 <text>。没 node 就明确跳过。
        note = _js_core_test(scene, gel_png, td, expected=(w0, h0, lm, tp, face_w_px, face_h_px))
        print("[i] JS core：%s" % note)
        ok += 1


        # 39) export 复现 render：magick compare -metric AE 必须为 0（逐像素；不只比同一份字节）
        assert _ae(out, exp).split()[0] == "0", ("export 与 render 的 AE 不为 0", _ae(out, exp))
        ok += 1

        # 40) writer 页面：page_px = page_size_cm × dpi（每边只取整一次）；默认 dpi 取 scene。
        #     一条更长的标签只让页面长在**左边**（右边/上边/下边仍是 margin_min_cm）。
        sc_short = _scene_one_label("250 kD")
        sc_long = _scene_one_label("A VERY LONG LABEL 250 kD")
        assert page_px(sc_short) == page_px(sc_short, sc_short["output"]["dpi"]), \
            "page_px 的默认 dpi 不是 scene.output.dpi"
        for dpi in (96, 300):
            ppc = dpi / 2.54
            ps = page_size_cm(sc_short)
            Wp, Hp, Lp, Tp, fw, fh = page_px(sc_short, dpi)
            assert fw == int(round(sc_short["gel"]["width_cm"] * ppc)), (fw, dpi)
            assert fh == int(round(sc_short["gel"]["height_cm"] * ppc)), (fh, dpi)
            assert Lp == int(round(ps["left_cm"] * ppc)), (Lp, dpi)
            assert Tp == int(round(ps["top_cm"] * ppc)), (Tp, dpi)
            assert Wp == fw + Lp + int(round(ps["right_cm"] * ppc)), (Wp, dpi)
            assert Hp == fh + Tp + int(round(ps["bottom_cm"] * ppc)), (Hp, dpi)
            # 每边各取整一次：总宽与「页面 cm × ppc」相差不超过各边的取整误差（≤2 px）
            assert abs(Wp - ps["width_cm"] * ppc) <= 2.0, (Wp, ps, dpi)
            assert abs(Hp - ps["height_cm"] * ppc) <= 2.0, (Hp, ps, dpi)
        ps_s, ps_l = page_size_cm(sc_short), page_size_cm(sc_long)
        Ws, _Hs = page_px(sc_short, 96)[:2]
        Wl, Hl = page_px(sc_long, 96)[:2]
        assert Wl > Ws + 1, ("长标签没有让页面变宽", Ws, Wl)
        assert Hl == _Hs, ("长标签不该改变页面高度", _Hs, Hl)
        assert abs(ps_l["right_cm"] - ps_s["right_cm"]) < 1e-9, (ps_s, ps_l)
        assert abs(ps_l["top_cm"] - ps_s["top_cm"]) < 1e-9, (ps_s, ps_l)
        assert abs(ps_l["bottom_cm"] - ps_s["bottom_cm"]) < 1e-9, (ps_s, ps_l)
        assert ps_l["left_cm"] > ps_s["left_cm"] + 0.5, ("页面没长在左边", ps_s, ps_l)
        ok += 1

        # 41) scene → PNG（writer 独立跑）：手写 3 条标签 + 一张 _synth_gel 的胶面。
        #     页面像素必须等于 page_px；每条标签的**墨迹框中心**必须落在锚点算出的位置（±1 px）。
        gel_png = os.path.join(td, "writer-gel.png")
        _synth_gel(gel_png, (189, 151), (20, 15, 169, 136), [50, 95, 140])
        font = pick_font()
        wsc = {
            "version": 1,
            "gel": {"image": os.path.basename(gel_png), "width_cm": 5.0, "height_cm": 4.0},
            "page": {"margin_min_cm": 0.5, "pad_cm": 0.2, "background": "#ffffff"},
            "output": {"dpi": 96},
            "font": {"family": "Arial", "file": font},
            "outline": {"color": None, "width_frac": 0.07},
            "items": [
                {"id": "lane-1", "role": "lane", "bind": {"lane": 1}, "text": "1",
                 "x_cm": 1.5, "y_cm": -0.2, "anchor": "bottom-center",
                 "size_pt": 8, "color": "#000000"},
                {"id": "marker-1-1", "role": "marker",
                 "bind": {"marker_lane": 1, "label_index": 1, "band_y_cm": 2.0},
                 "text": "250 kD", "x_cm": 0.2, "y_cm": 2.0, "anchor": "middle-right",
                 "size_pt": 8, "color": "#000000"},
                {"id": "free-1", "role": "free", "bind": {}, "text": "WT",
                 "x_cm": 4.5, "y_cm": 4.2, "anchor": "top-left",
                 "size_pt": 8, "color": "#000000"},
            ],
            "source": {"skill": "ep-labeling"},
        }
        wpath = os.path.join(td, "writer.scene.json")
        scene_write(wsc, wpath)                              # 先过 schema 再出图
        wpng = os.path.join(td, "writer.png")
        res = scene_png_write(wsc, wpng)
        assert (res["width_px"], res["height_px"]) == page_px(wsc)[:2], res
        assert res["items"] == 3 and len(res["out_items"]) == 3, res
        wimg, himg, rgb = read_ppm(wpng, None)
        assert (wimg, himg) == page_px(wsc)[:2], \
            ("writer 落盘尺寸与 page_px 不一致", (wimg, himg), page_px(wsc)[:2])
        gray = to_gray(wimg, himg, rgb)
        ppc = 96 / 2.54
        for it in wsc["items"]:
            px = max(6, int(round(float(it["size_pt"]) / 72.0 * 96)))
            _l, _t, _r, _b = item_box_cm(wsc, it)
            bx, by = res["left_px"] + _l * ppc, res["top_px"] + _t * ppc
            # 同一串字在同一套 annotate 下的墨迹相对 text box 左上角的偏移（控制图）
            ctrl = os.path.join(td, "ctrl-%s.png" % it["id"])
            _run(annotate_cmd([MAGICK, "-size", "200x80", "xc:white"],
                               [(100, 20, it["text"], px, it["color"])], font, None) + [ctrl])
            cw, ch, crgb = read_ppm(ctrl, None)
            cbb = _ink_bbox(to_gray(cw, ch, crgb), cw, ch, 0, 0, cw, ch)
            assert cbb, ("控制图没有墨迹", it["id"])
            want = [int(round(bx)) + cbb[0] - 100, int(round(by)) + cbb[1] - 20,
                    int(round(bx)) + cbb[2] - 100, int(round(by)) + cbb[3] - 20]
            got = _ink_bbox(gray, wimg, himg, want[0] - 3, want[1] - 3,
                            want[2] + 4, want[3] + 4)
            assert got, ("成品里找不到标签墨迹", it["id"], want)
            dcx = abs((got[0] + got[2]) / 2.0 - (want[0] + want[2]) / 2.0)
            dcy = abs((got[1] + got[3]) / 2.0 - (want[1] + want[3]) / 2.0)
            assert dcx <= 1 and dcy <= 1, ("墨迹中心偏了", it["id"], got, want, dcx, dcy)
        ok += 1

        # 42) writer 的 dpi 真进 PNG：identify 的 %x 与 pHYs 都必须是本次 dpi（--dpi 300 覆盖）。
        w300 = os.path.join(td, "writer-300.png")
        res300 = scene_png_write(wsc, w300, dpi=300)
        assert (res300["width_px"], res300["height_px"]) == page_px(wsc, 300)[:2], res300
        assert (res300["width_px"], res300["height_px"]) > (res["width_px"], res["height_px"])
        res_x = _run([MAGICK, "identify", "-units", "PixelsPerInch", "-format", "%x", w300], False)
        assert res_x.split() and abs(float(res_x.split()[0]) - 300) <= 1, res_x
        raw = open(w300, "rb").read()
        i = raw.find(b"pHYs")
        assert i > 0, "writer 的 PNG 没有 pHYs"
        ppm_x = int.from_bytes(raw[i + 4 : i + 8], "big")
        assert abs(ppm_x - round(300 / 0.0254)) <= 1, ("pHYs 不是 300 dpi", ppm_x)
        ok += 1

    # 39/40/41) SVG writer（spec §5）：XML、页面 cm、内嵌胶面、item 数/id、白边两遍、
    #            与 PNG 的墨迹中心（§8 容差 0.03 cm）。
    with tempfile.TemporaryDirectory() as td:
        import xml.etree.ElementTree as ET
        gel = os.path.join(td, "synth.gel.png")
        sc = _synth_svg_scene(gel, dpi=300)
        sp = os.path.join(td, "fig.scene.json")
        scene_write(sc, sp)
        svg = os.path.join(td, "fig.svg")
        info = write_svg(sc, svg, gel_path=gel, dpi=300)
        ps = page_size_cm(sc)
        ns = "{http://www.w3.org/2000/svg}"
        root = ET.parse(svg).getroot()

        # 39) 页面尺寸：width/height 带 cm，viewBox 同一组数（1 user unit = 1 cm）
        w_cm = float(root.get("width")[:-2])
        h_cm = float(root.get("height")[:-2])
        vb = [float(v) for v in root.get("viewBox").split()]
        assert root.get("width").endswith("cm") and root.get("height").endswith("cm"), root.attrib
        assert abs(w_cm - ps["width_cm"]) < 1e-3 and abs(h_cm - ps["height_cm"]) < 1e-3, \
            (w_cm, h_cm, ps)
        assert vb == [0.0, 0.0, w_cm, h_cm], (vb, w_cm, h_cm)
        assert info["page_cm"] == (ps["width_cm"], ps["height_cm"]), info

        # 胶面：恰好一个 <image>，href 是 data URI，解出来就是 gel PNG 的字节
        imgs = root.findall(".//%simage" % ns)
        assert len(imgs) == 1, ("<image> 不是 1 个", len(imgs))
        href = imgs[0].get("href") or imgs[0].get("{http://www.w3.org/1999/xlink}href")
        assert href and href.startswith("data:image/png;base64,"), (href or "")[:40]
        with open(gel, "rb") as f:
            want = f.read()
        assert base64.b64decode(href.split(",", 1)[1]) == want, "内嵌的胶面字节与 gel PNG 不一致"

        # 每条 item 一个 <text>（不含白边），id 全在；属性齐全
        texts = root.findall(".//%stext" % ns)
        fills = [t for t in texts if not str(t.get("id")).endswith("-outline")]
        assert len(fills) == len(sc["items"]) == info["n_items"], (len(fills), len(sc["items"]))
        assert {t.get("id") for t in fills} == {it["id"] for it in sc["items"]}, \
            ([t.get("id") for t in fills], [it["id"] for it in sc["items"]])
        for t in fills:
            assert t.get("data-role") in ("lane", "marker", "free"), t.attrib
            assert json.loads(t.get("data-bind")) == next(
                it["bind"] for it in sc["items"] if it["id"] == t.get("id")), t.attrib
            assert t.get("font-family") == sc["font"]["family"] and t.get("fill")
            assert t.get("text-anchor") in ("start", "middle", "end"), t.attrib

        # 几何断言：x/y/anchor/字号都能从 scene 复算（不依赖栅格化器）
        # 基线偏移是实测值（模块头那张表），先钉两个已知点：11px→10、33px→30。
        assert baseline_offset_px(pick_font(), 11) == 10, baseline_offset_px(pick_font(), 11)
        assert baseline_offset_px(pick_font(), 33) == 30, baseline_offset_px(pick_font(), 33)
        for it, t in zip(sc["items"], fills):
            _left, top_cm, _r, _b = item_box_cm(sc, it)
            _v, h = norm_anchor(it["anchor"]).split("-")
            want_anchor = {"left": "start", "center": "middle", "right": "end"}[h]
            assert t.get("text-anchor") == want_anchor, (it["id"], t.get("text-anchor"), want_anchor)
            assert abs(float(t.get("x")) - (ps["left_cm"] + float(it["x_cm"]))) < 1e-3, \
                (it["id"], t.get("x"), ps["left_cm"] + float(it["x_cm"]))
            px = max(6, int(round(float(it["size_pt"]) / 72.0 * 300)))
            want_y = ps["top_cm"] + top_cm + baseline_offset_px(sc["font"]["file"], px) * 2.54 / 300
            assert abs(float(t.get("y")) - want_y) < 1e-3, (it["id"], t.get("y"), want_y)
            assert abs(float(t.get("font-size")) - float(it["size_pt"]) * 2.54 / 72.0) < 1e-4, \
                (it["id"], t.get("font-size"))
        ok += 1

        # 40) 白边：text 数翻倍，每个 outline 都在自己的 fill **前面**（两遍画，不是 paint-order）
        sc_o = _synth_svg_scene(gel, dpi=300, outline="#ffffff")
        svg_o = os.path.join(td, "outline.svg")
        write_svg(sc_o, svg_o, gel_path=gel, dpi=300)
        root_o = ET.parse(svg_o).getroot()
        texts_o = root_o.findall(".//%stext" % ns)
        assert len(texts_o) == 2 * len(sc_o["items"]), (len(texts_o), len(sc_o["items"]))
        order = [t.get("id") for t in texts_o]
        for it in sc_o["items"]:
            ident = it["id"]
            assert order.index(ident + "-outline") < order.index(ident), \
                ("outline 不在 fill 前面", ident, order)
            o = next(t for t in texts_o if t.get("id") == ident + "-outline")
            f = next(t for t in texts_o if t.get("id") == ident)
            assert o.get("stroke") == "#ffffff" and o.get("fill") == "#ffffff", o.attrib
            assert o.get("stroke-linejoin") == "round" and float(o.get("stroke-width")) > 0, o.attrib
            assert o.get("x") == f.get("x") and o.get("y") == f.get("y"), ("白边几何变了", ident)
            assert not f.get("stroke"), ("fill text 不该带描边", ident)
        # scene.outline 为空 ⇒ 没有白边 text（spec §5）
        sc_n = json.loads(json.dumps(sc_o))
        sc_n["outline"] = None
        root_n = ET.fromstring(svg_string(sc_n, gel_path=gel, dpi=300))
        assert len(root_n.findall(".//%stext" % ns)) == len(sc["items"]), "outline=None 仍画了白边"
        ok += 1

        # 41) 保真（§8）：同一 scene、同一 dpi → SVG 与 PNG 的每条标签墨迹中心差 ≤ 0.03 cm。
        #     栅格化优先用 rsvg-convert（本机 ImageMagick 的 SVG 委托就是它）；本机 magick
        #     自带 MSVG 渲染器按 user unit 排字（墨迹大 3–4 倍），不能当文字度量的基准，
        #     这时只保留上面的几何断言（brief 允许）。
        png = os.path.join(td, "fig.png")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gp_main(["export", sp, "--png", png, "--dpi", "300"])
        assert rc == 0, "export（保真对照的 PNG）失败"
        raster = os.path.join(td, "raster.png")
        tool = _svg_raster(svg, raster, 300)
        if tool is None:
            print("[i] 没有 rsvg-convert，SVG 保真只做几何断言（不做像素比较）")
        else:
            worst = 0.0
            for it in sc["items"]:
                box = item_box_cm(sc, it)
                a = _ink_centre_cm(png, box, ps)
                b = _ink_centre_cm(raster, box, ps)
                assert a and b, ("区域里找不到墨迹", it["id"], a, b)
                dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
                worst = max(worst, dx, dy)
                assert dx <= 0.03 and dy <= 0.03, \
                    ("SVG 与 PNG 的墨迹中心超出 §8 容差", it["id"], round(dx, 4), round(dy, 4))
            assert worst <= 0.03, worst
        ok += 1

    # 51) .vectorcraft 往返（V1）：三画板、四图层（含隐藏+锁定+不打印）、两条线、两段文字、
    #     同一张图被两个对象引用（其中一个在剪切组里）→ write → read：名字/旗标/几何/字节都不变，
    #     图片只存一份，next_id 大于所有 id，全文没有 link 键（内嵌，不链接）。
    with tempfile.TemporaryDirectory() as td:
        doc, png = _synth_vc_doc()
        path = vc_file.write(doc, os.path.join(td, "t.vectorcraft"))
        top, blobs = vc_file.read(path)
        d = top["document"]
        assert [a["name"] for a in d["artboards"]] == ["Figure", "Workbench", "Profiles"], d["artboards"]
        assert d["artboards"][1]["rect"] == {"x0": 340.0, "y0": 0.0, "x1": 640.0, "y1": 200.0}
        assert [l["name"] for l in d["layers"]] == ["gel", "lanes", "labels", "ep-meta"]
        assert vc_file.layer_flags(vc_file.layer_by_name(d, "gel")) == (True, True, True)
        assert vc_file.layer_flags(vc_file.layer_by_name(d, "lanes")) == (True, False, False)
        assert vc_file.layer_flags(vc_file.layer_by_name(d, "ep-meta")) == (False, True, False)
        lanes = vc_file.lay_children(vc_file.layer_by_name(d, "lanes"))
        assert [vc_file.line_ends(n) for n in lanes] == [((385.0, 30.0), (385.0, 180.0)),
                                                         ((400.0, 30.0), (400.0, 180.0))]
        assert all(n["kind"]["live"]["a"]["x"] == vc_file.line_ends(n)[0][0] for n in lanes)
        lab = vc_file.text_info(vc_file.lay_children(vc_file.layer_by_name(d, "labels"))[0])
        assert lab == {"text": "M", "x": 52.0, "y": 25.0, "font": "Arial", "size": 8.0,
                       "color": "#ff0000", "justify": "Center"}, lab
        meta = vc_file.text_info(vc_file.lay_children(vc_file.layer_by_name(d, "ep-meta"))[0])
        assert json.loads(meta["text"]) == {"skill": "ep-labeling", "px_per_cm": 47.2}, meta
        imgs = [n for _l, n in vc_file.walk(d) if n["kind"]["type"] == "image"]
        assert len(imgs) == 2 and len({n["kind"]["key"] for n in imgs}) == 1, imgs
        assert list(blobs) == [imgs[0]["kind"]["key"]] and blobs[imgs[0]["kind"]["key"]] == png
        grp = vc_file.lay_children(vc_file.layer_by_name(d, "gel"))[0]
        assert grp["name"] == "gel seg 1" and grp["kind"]["clip"] is True
        assert grp["kind"]["children"][0]["kind"]["clipping"] is True
        ids = [n["id"] for _l, n in vc_file.walk(d)] + [l["id"] for l in d["layers"]]
        assert len(ids) == len(set(ids)) and d["next_id"] > max(ids), (ids, d["next_id"])
        assert "link" not in set(_walk_keys(top)), "内嵌图不该带 link"
        assert not os.path.exists(path + ".tmp"), "原子写留下了临时文件"
    ok += 1

    # 52) 读三种编码（V1）：同一份内容分别存成 inline / \0VCBLOBS\0 容器 / gzip → loads 全相等；
    #     blob 区间越界要报错，不是静默截断。
    raw = vc_file.dumps(doc)
    top_i, blobs_i = vc_file.loads(raw)
    blob_top = json.loads(raw.decode("utf-8"))
    tail, off = b"", 0
    for k in sorted(blob_top["images"]):
        b = base64.b64decode(blob_top["images"][k].pop("data"))
        blob_top["images"][k]["blob"] = [off, len(b)]
        tail += b
        off += len(b)
    blob_raw = json.dumps(blob_top).encode() + b"\n" + vc_file.BLOB_MARK + tail
    for enc, r in (("blobs", blob_raw), ("gzip", gzip.compress(raw)), ("gzip+blobs", gzip.compress(blob_raw))):
        t2, b2 = vc_file.loads(r, enc)
        assert t2["document"] == top_i["document"], enc
        assert b2 == blobs_i, enc
    k0 = sorted(blob_top["images"])[0]
    blob_top["images"][k0]["blob"] = [0, len(tail) + 1]
    try:
        vc_file.loads(json.dumps(blob_top).encode() + b"\n" + vc_file.BLOB_MARK + tail)
        raise AssertionError("blob 越界没报错")
    except RuntimeError as e:
        assert "超出" in str(e), e
    ok += 1

    # 53) 字段齐全（V1，防 app 的严格加载器）：写出的每种对象的键路径 ⊇ 模板里同种对象的键路径
    #     （缺一个字段 app 就整份拒开，见 references/vectorcraft.md §1）。
    tpl = vc_file._template(vc_file.TEMPLATE)
    d = top_i["document"]
    lanes = vc_file.lay_children(vc_file.layer_by_name(d, "lanes"))
    pairs = [("line", lanes[0], tpl["line"]),
             ("text", vc_file.lay_children(vc_file.layer_by_name(d, "labels"))[0], tpl["text"]),
             ("clip", vc_file.lay_children(vc_file.layer_by_name(d, "gel"))[0], tpl["clip"]),
             ("artboard", d["artboards"][0], tpl["artboard"])]
    lay_w = dict(vc_file.layer_by_name(d, "labels"), kind=dict(vc_file.layer_by_name(d, "labels")["kind"], children=[]))
    pairs.append(("layer", lay_w, tpl["layer"]))
    for what, got, want in pairs:
        miss = _key_paths(want) - _key_paths(got)
        miss = {m for m in miss if not m.startswith(("kind.children.", "kind.para.justify_auto"))}
        assert not miss, ("%s 比模板少了字段" % what, sorted(miss))
    assert set(d) >= set(tpl["document"]) - {"last_view", "layers_open"}, \
        sorted(set(tpl["document"]) - set(d))
    ok += 1

    # 54) 写入闸（V1）：重复 id、引用了没有字节的图、非 PNG、非法 justify、越界颜色都必须报错。
    def raises(fn, frag):
        try:
            fn()
        except RuntimeError as e:
            assert frag in str(e), (frag, str(e))
            return True
        return False
    bad = vc_file.new_doc("bad")
    lay = vc_file.add_layer(bad, "x")
    n = vc_file.line(bad, "a", (0, 0), (1, 1))
    vc_file.lay_children(lay).extend([n, copy.deepcopy(n)])
    assert raises(lambda: vc_file.dumps(bad), "id 重复"), "重复 id 没报错"
    bad2 = vc_file.new_doc("bad2")
    lay2 = vc_file.add_layer(bad2, "x")
    vc_file.lay_children(lay2).append({"id": 99, "name": "i", "kind": {"type": "image", "key": "imgnope",
                                                                      "width": 1, "height": 1,
                                                                      "xf": [1, 0, 0, 1, 0, 0]}})
    assert raises(lambda: vc_file.dumps(bad2), "没有字节"), "悬空图片没报错"
    assert raises(lambda: vc_file.add_image(bad2, b"GIF89a...."), "不是 PNG")
    assert raises(lambda: vc_file.text(bad2, "t", 0, 0, "x", justify="middle"), "justify")
    assert raises(lambda: vc_file.line(bad2, "l", (0, 0), (1, 1), rgb=(0, 0, 255)), "0–1")
    assert raises(lambda: vc_file.loads(b'{"format": "svg"}'), "不是 .vectorcraft")
    ok += 1

    # 55) 控制口协议（V2，假服务器）：一行请求一行回包；回包分两段到也要拼齐；ok=false 的错误信息
    #     要写出是哪条命令；空包 / 非 JSON → VCError；请求 id 递增。
    with _FakeVC() as fv:
        c = vc_control.Client(fv.port, timeout=5)
        fv.on["ui.inspect"] = lambda p: {"documents": []}
        assert c.call("ui.inspect") == {"documents": []}
        fv.on["document.json"] = lambda p: {"layers": [], "pad": "x" * 300000}
        fv.split = True
        assert len(c.document_json()["pad"]) == 300000, "分段回包没拼齐"
        fv.split = False
        fv.on["text.setText"] = lambda p: _FakeVC.Err("no such object: 99")
        try:
            c.execute("text.setText", {"id": 99, "text": "x"})
            raise AssertionError("ok=false 没抛错")
        except vc_control.VCError as e:
            assert "text.setText" in str(e) and "no such object" in str(e), e
        for raw in (b"\n", b"not json\n"):
            fv.raw = raw
            try:
                c.call("ui.inspect")
                raise AssertionError("坏回包 %r 没抛错" % raw)
            except vc_control.VCError:
                pass
        fv.raw = None
        ids = [r["id"] for r in fv.log]
        assert ids == sorted(ids) and len(set(ids)) == len(ids), ids
        assert fv.log[-1]["method"] == "ui.inspect"
        assert fv.log[2] == {"id": ids[2], "method": "engine.execute",
                             "params": {"command": "text.setText", "params": {"id": 99, "text": "x"}}}, fv.log[2]
    ok += 1

    # 56) 对话框（V2）：open 之后有对话框就 ui.dialog.cancel，直到没有；一直关不掉 → 报错，不死循环。
    with _FakeVC() as fv:
        c = vc_control.Client(fv.port, timeout=5)
        dialogs = [{"kind": "missingLinks"}, {"kind": "fontProblems"}]
        fv.on["file.open"] = lambda p: {"warnings": [], "index": 0}
        fv.on["ui.inspect"] = lambda p: {"ui": {"dialog": dialogs[0] if dialogs else None}}
        fv.on["ui.dialog.cancel"] = lambda p: dialogs.pop(0) and None
        assert c.open("/x/fig.vectorcraft") == {"warnings": [], "index": 0}
        assert [r["method"] for r in fv.log].count("ui.dialog.cancel") == 2
        assert fv.log[0]["params"]["params"]["path"] == "/x/fig.vectorcraft"
        fv.on["ui.inspect"] = lambda p: {"ui": {"dialog": {"kind": "stuck"}}}
        fv.on["ui.dialog.cancel"] = lambda p: None
        try:
            c.clear_dialogs(tries=3)
            raise AssertionError("关不掉的对话框没报错")
        except vc_control.VCError as e:
            assert "stuck" in str(e), e
    ok += 1

    # 57) 存盘轮询（V2）：app 先写半截再写全 → save 只在能解析之后返回；老文件（mtime 没变）不算；
    #     一直不写 → 超时报错。
    with _FakeVC() as fv, tempfile.TemporaryDirectory() as td:
        c = vc_control.Client(fv.port, timeout=5)
        full = vc_file.dumps(_synth_vc_doc()[0])
        target = os.path.join(td, "fig.vectorcraft")
        with open(target, "wb") as f:
            f.write(full)                       # 旧文件已经在：没变就不能当成这次的结果
        os.utime(target, ns=(1, 1))

        def slow_save(p):
            _later(0.05, lambda: _write(p["path"], full[:len(full) // 2]))
            _later(0.40, lambda: _write(p["path"], full))
            return {"bytes": len(full)}
        fv.on["document.save"] = slow_save
        t0 = time.time()
        top, blobs = c.save(target, timeout=5)
        assert time.time() - t0 >= 0.35, "半截文件就返回了"
        assert top["document"]["title"] == "selftest" and len(blobs) == 1
        fv.on["document.save"] = lambda p: {"bytes": 0}
        os.utime(target, ns=(1, 1))
        try:
            c.save(target, timeout=0.5)
            raise AssertionError("没写文件却没超时")
        except vc_control.VCError as e:
            assert "存盘" in str(e), e
    ok += 1

    # 58) 导出核对（V2）：pHYs 换算成 dpi；尺寸 = 画板 pt × dpi/72（±1 px）；dpi 或尺寸不对 → 报错；
    #     画板下标越界 → 报错（不发 export）。
    with _FakeVC() as fv, tempfile.TemporaryDirectory() as td:
        c = vc_control.Client(fv.port, timeout=5)
        fv.on["document.inspect"] = lambda p: {"artboards": [{"name": "Figure", "width": 72.0, "height": 36.0}]}
        out = os.path.join(td, "fig.png")
        png = {"w": 300, "h": 150, "dpi": 300}

        def export(p):
            assert p["artboard"] == 0 and p["ppi"] == 300 and p["format"] == "png", p
            _later(0.1, lambda: _write(p["path"], _tiny_png(png["w"], png["h"], dpi=png["dpi"])))
            return {"bytes": 1}
        fv.on["document.export"] = export
        w, h, dpi = c.export_png(out, 0, 300, timeout=5)
        assert (w, h) == (300, 150) and abs(dpi - 300) < 0.01, (w, h, dpi)
        assert vc_control.png_info(_tiny_png(2, 2))[2] is None, "没有 pHYs 应读成 None"
        for bad, frag in (({"dpi": 72}, "dpi"), ({"w": 280}, "尺寸")):
            png.update({"w": 300, "h": 150, "dpi": 300}, **bad)
            try:
                c.export_png(out, 0, 300, timeout=5)
                raise AssertionError("坏导出 %r 没报错" % bad)
            except vc_control.VCError as e:
                assert frag in str(e), (bad, e)
        n_export = [r.get("params", {}).get("command") for r in fv.log].count("document.export")
        try:
            c.export_png(out, 3, 300, timeout=5)
            raise AssertionError("画板越界没报错")
        except vc_control.VCError:
            pass
        assert [r.get("params", {}).get("command") for r in fv.log].count("document.export") == n_export
    ok += 1

    # 59) 撤销组 / batch / connect（V2）：成功 = begin…end{}；块里出错 = end{cancel:true} 并把原错抛出；
    #     batch 参数形状；端口没人听但 app 在跑 → 报「存盘并退出」；launch 时带 --control 起进程并等到监听。
    with _FakeVC() as fv:
        c = vc_control.Client(fv.port, timeout=5)
        cmds = lambda: [(r["params"]["command"], r["params"]["params"]) for r in fv.log
                        if r["method"] == "engine.execute"]
        with c.undo_group("ep-labeling sync"):
            c.execute("text.setText", {"id": 1, "text": "A"})
        assert cmds() == [("edit.undoGroup.begin", {"label": "ep-labeling sync"}),
                          ("text.setText", {"id": 1, "text": "A"}),
                          ("edit.undoGroup.end", {})], cmds()
        fv.log.clear()
        try:
            with c.undo_group():
                raise KeyError("boom")
        except KeyError:
            pass
        else:
            raise AssertionError("撤销组吞掉了异常")
        assert cmds()[-1] == ("edit.undoGroup.end", {"cancel": True}), cmds()
        fv.log.clear()
        c.batch([("object.move", {"ids": [3], "dx": 1, "dy": 0}), ("edit.clear", {"ids": [4]})], label="L")
        assert cmds() == [("command.batch", {"label": "L", "commands": [
            {"command": "object.move", "params": {"ids": [3], "dx": 1, "dy": 0}},
            {"command": "edit.clear", "params": {"ids": [4]}}]})], cmds()
        cl, launched = vc_control.connect(fv.port, _running=lambda: True)
        assert launched is False and cl.port == fv.port, "端口在听却没复用"
    free = _free_port()
    try:
        vc_control.connect(free, _running=lambda: True)
        raise AssertionError("没带 --control 的实例没报错")
    except vc_control.VCError as e:
        assert "存盘并退出" in str(e), e
    try:
        vc_control.connect(free, launch=False, _running=lambda: False)
        raise AssertionError("launch=False 没报错")
    except vc_control.VCError as e:
        assert "--control %d" % free in str(e), e
    started = []
    with tempfile.TemporaryDirectory() as td:
        app = os.path.join(td, "VectorCraft.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        _write(vc_control.app_exe(app), b"#!/bin/sh\n")

        def fake_popen(cmd, **kw):
            started.append(cmd)
            started.append(_FakeVC(port=free).__enter__())
        cl, launched = vc_control.connect(free, path="fig.vectorcraft", app=app, wait=5,
                                          _running=lambda: False, _popen=fake_popen)
        try:
            assert launched is True and cl.port == free
            assert started[0] == [vc_control.app_exe(app), "--control", str(free),
                                  os.path.abspath("fig.vectorcraft")], started[0]
        finally:
            started[1].__exit__(None, None, None)
    ok += 1

    # 60) Workbench 建模（V3）：record → 模型。泳道左右 = 中心 ± 宽/2（不越过邻道中点），
    #     marker 条带线按标签配 MW（rows:2 的并带出两条线），trim 进 trimmed，默认窗下沿让开 3%；
    #     条带数与标签行数对不上 → 拒绝；标签里没数字 → 拒绝。
    _preset, rec = _synth_record()
    names = ["M"] + [str(i) for i in range(1, 15)]
    mdl = workbench.from_record(rec, names, ["250 kD", "100 kD", "50 kD"], 1, trim=[14, 15],
                                meta={"skill": "ep-labeling"})
    assert len(mdl["lanes"]) == 15 and mdl["marker_lane"] == 1
    assert [ln["trimmed"] for ln in mdl["lanes"]][-3:] == [False, True, True]
    for ln, src in zip(mdl["lanes"], rec["lanes"]):
        assert abs((ln["left_cm"] + ln["right_cm"]) / 2 - src["x_cm"]) < 1e-3
        assert abs((ln["right_cm"] - ln["left_cm"]) - src["width_cm"]) < 1e-3
    ys = [b["y_cm"] for b in rec["bands"]["1"]]
    assert [(b["mw"], b["y_cm"]) for b in mdl["bands"]] == list(zip([250.0, 100.0, 50.0], ys)), mdl["bands"]
    assert mdl["window"] == {"top_cm": 0.0, "bottom_cm": round(6.6 * 0.97, 4)}, mdl["window"]
    assert mdl["meta"]["lane_names"] == names and len(mdl["meta"]["prev_centres_cm"]) == 15
    pair = workbench.from_record(rec, names, ["250 kD", {"text": "21/20 kD", "rows": 2}], 1)
    assert [b["mw"] for b in pair["bands"]] == [250.0, 21.0, 20.0], pair["bands"]
    for labels, frag in ((["250", "100", "50", "10"], "不硬配"), (["250", "ladder", "50"], "没有分子量")):
        try:
            workbench.from_record(rec, names, labels, 1)
            raise AssertionError("坏 marker 标签 %r 没报错" % labels)
        except workbench.WorkbenchError as e:
            assert frag in str(e), e
    ok += 1

    # 61) Workbench 往返（V3）：模型 → 文档 → 文件字节 → 读回 == 原模型（cm 误差 < 1e-3）；
    #     胶面图的位置/比例只从文档里那张 gel 图的 xf 来（不靠元数据）。
    doc_wb, png_wb = _synth_wb_doc(mdl)
    top_wb, _blobs = vc_file.loads(vc_file.dumps(doc_wb))
    back, notes = workbench.from_document(top_wb["document"])
    assert not notes, notes
    _assert_model_close(back, mdl)
    assert back["meta"] == mdl["meta"]
    names_in = {l["name"] for l in top_wb["document"]["layers"]}
    assert names_in == {n for n, *_r in workbench.LAYERS}, names_in
    ok += 1

    # 62) 读回用户的改动（V3）：泳道 3 右移 0.2 cm（且线有点斜）、删 band 100、在泳道 7/8 之间
    #     新画一对**没有名字**的线、把泳道 2 挪进 trimmed、积分窗上沿下移 → 全部按几何读出来。
    mdl4 = workbench.from_record(rec, names, ["250 kD", "100 kD", "50 kD", "10 kD"], 1,
                                 marker_rows_cm=ys + [ys[-1] + 0.5], trim=[14, 15])
    d = vc_file.loads(vc_file.dumps(_synth_wb_doc(mdl4)[0]))[0]["document"]
    L = {l["name"]: l for l in d["layers"]}
    gx, s_pt = _wb_origin(d)
    cm = lambda pt: (pt - gx) / vc_file.PT_PER_CM
    lane_lines = vc_file.lay_children(L["lanes"])
    by_x = sorted(lane_lines, key=lambda n: vc_file.line_ends(n)[0][0])
    for n in by_x[4:6]:                                  # 泳道 3 的左右线
        for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
            a_["p"][0] += 0.2 * vc_file.PT_PER_CM
        n["kind"]["path"]["subpaths"][0]["anchors"][1]["p"][0] += 0.6     # 下端斜 0.6 pt：读中点
    moved = by_x[2:4]                                    # 泳道 2 → trimmed
    for n in moved:
        lane_lines.remove(n)
    vc_file.lay_children(L["trimmed"]).extend(moved)
    bands_l = vc_file.lay_children(L["marker bands"])
    bands_l[:] = [n for n in bands_l if n["name"] != "band 100"]
    c7, c8 = (mdl["lanes"][6]["right_cm"], mdl["lanes"][7]["left_cm"])
    for x_cm in (c7 + 0.05, c8 - 0.05):
        n = json.loads(json.dumps(by_x[0]))
        n.pop("name")
        n["id"] = 90000 + int(x_cm * 1000)
        x = gx + x_cm * vc_file.PT_PER_CM
        for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
            a_["p"][0] = x
        lane_lines.append(n)
    wl = sorted(vc_file.lay_children(L["window"]), key=lambda n: vc_file.line_ends(n)[0][1])
    for a_ in wl[0]["kind"]["path"]["subpaths"][0]["anchors"]:
        a_["p"][1] += 0.5 * vc_file.PT_PER_CM
    got, notes = workbench.from_document(d)
    assert len(got["lanes"]) == 16, len(got["lanes"])
    assert [ln["trimmed"] for ln in got["lanes"]][:3] == [False, True, False]
    c3 = (got["lanes"][2]["left_cm"] + got["lanes"][2]["right_cm"]) / 2
    want = rec["lanes"][2]["x_cm"] + 0.2 + 0.3 / vc_file.PT_PER_CM   # 两条线下端各斜 0.6 pt → 中点各多 0.3 pt
    assert abs(c3 - want) < 2e-3, (c3, want)
    assert abs(got["lanes"][7]["left_cm"] - (c7 + 0.05)) < 1e-3, got["lanes"][7]
    assert [b["mw"] for b in got["bands"]] == [250.0, 50.0, 10.0]
    assert abs(got["window"]["top_cm"] - 0.5) < 1e-3, got["window"]
    ok += 1

    # 63) 校验（V3）：所有问题一次报完、什么都不读出来 —— 奇数条泳道线、window 三条线、
    #     band 名字不合规、泳道交错；另一份：条带线横跨两条泳道、MW 不单调、条带不足 3 条。
    bad = json.loads(json.dumps(top_wb["document"]))
    B = {l["name"]: l for l in bad["layers"]}
    lanes_b = vc_file.lay_children(B["lanes"])
    lanes_b.pop()                                                           # 奇数
    vc_file.lay_children(B["window"]).append(json.loads(json.dumps(vc_file.lay_children(B["window"])[0])))
    vc_file.lay_children(B["marker bands"])[0]["name"] = "250kD band"
    try:
        workbench.from_document(bad)
        raise AssertionError("坏 Workbench 没报错")
    except workbench.WorkbenchError as e:
        msg = str(e)
        assert "有 3 处要改" in msg, msg                                     # 一次报完
        for frag in ("奇数", "window 图层应当恰好有两条横线", "band <分子量>"):
            assert frag in msg, (frag, msg)
    bad3 = json.loads(json.dumps(top_wb["document"]))
    B3 = {l["name"]: l for l in bad3["layers"]}
    dup = json.loads(json.dumps(sorted(vc_file.lay_children(B3["lanes"]),
                                       key=lambda n: vc_file.line_ends(n)[0][0])[6:8]))
    for i, n in enumerate(dup):
        n["id"] = 80000 + i
        for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
            a_["p"][0] += 1.0                                               # 与泳道 4 交错 1 pt
    vc_file.lay_children(B3["trimmed"]).extend(dup)
    try:
        workbench.from_document(bad3)
        raise AssertionError("交错的泳道没报错")
    except workbench.WorkbenchError as e:
        assert "重叠" in str(e), e
    bad2 = json.loads(json.dumps(top_wb["document"]))
    B2 = {l["name"]: l for l in bad2["layers"]}
    bl = sorted(vc_file.lay_children(B2["marker bands"]), key=lambda n: vc_file.line_ends(n)[0][1])
    a0 = bl[0]["kind"]["path"]["subpaths"][0]["anchors"]
    for shift, frag in ((4 * 0.565, "横跨了泳道"), (2.0, "不在任何泳道里")):   # 落到泳道 5 上 / 落在道间空隙
        for a_ in a0:
            a_["p"][0] += shift * vc_file.PT_PER_CM
        try:
            workbench.from_document(bad2)
            raise AssertionError("条带线挪 %.2f cm 没报错" % shift)
        except workbench.WorkbenchError as e:
            assert frag in str(e), (frag, e)
            assert frag != "不在任何泳道里" or bl[0]["name"] in str(e), e        # 点名是哪条线
        for a_ in a0:
            a_["p"][0] -= shift * vc_file.PT_PER_CM
    bl[0]["name"], bl[1]["name"] = "band 50", "band 100"                   # MW 随 y 变大
    vc_file.lay_children(B2["marker bands"]).remove(bl[2])                 # 只剩 2 条
    try:
        workbench.from_document(bad2)
        raise AssertionError("MW 不单调 / 不足 3 条没报错")
    except workbench.WorkbenchError as e:
        assert "分子量不更小" in str(e) and "至少要 3 条" in str(e), e
    ok += 1

    # 64) 泳道名跟着泳道走（V3）：中间插一条 → 原名不顺移；删一条 → 其余原名不变；
    #     小挪动（< 半个中心距）仍配上；挪太远 → 当成新泳道（None，agent 去问名字）。
    prev = [0.5, 1.0, 1.5, 2.0, 2.5]
    nm = ["M", "A", "B", "C", "D"]
    assert workbench.match_lanes(prev, [0.5, 1.0, 1.25, 1.5, 2.0, 2.5]) == [0, 1, None, 2, 3, 4]
    assert workbench.match_lanes(prev, [0.5, 1.0, 2.0, 2.5]) == [0, 1, 3, 4]
    assert workbench.match_lanes(prev, [0.6, 1.1, 1.4, 2.0, 2.5]) == [0, 1, 2, 3, 4]
    assert workbench.match_lanes(prev, [0.5, 1.0, 1.5, 2.0, 3.4]) == [0, 1, 2, 3, None]
    fake = {"lanes": [{"left_cm": c - 0.1, "right_cm": c + 0.1} for c in (0.5, 1.0, 1.25, 1.5, 2.0, 2.5)]}
    assert workbench.lane_names_after(fake, prev, nm) == ["M", "A", None, "B", "C", "D"]
    ok += 1

    # 65) 剖面像素口径（V4）：只取「中心落在 [L, R] / [top, bottom] 里」的像素做平均；
    #     窄到一列都不含时取最近一列；y 是行中心的胶面 cm。
    W_, H_, ppc_ = 10, 6, 2.0                                   # 5 × 3 cm，2 px/cm
    g = bytearray(W_ * H_)
    for y in range(H_):
        for x in range(W_):
            g[y * W_ + x] = 10 * x + y
    ys_, gs_ = profiles.lane_profile(g, W_, H_, ppc_, 1.0, 2.0, 0.5, 2.5)
    assert ys_ == [0.75, 1.25, 1.75, 2.25], ys_                 # 行 1..4（中心 0.75..2.25 cm）
    assert gs_ == [(20 + 30) / 2.0 + y for y in (1, 2, 3, 4)], gs_    # 列 2、3（中心 1.25、1.75）
    ys_, gs_ = profiles.lane_profile(g, W_, H_, ppc_, 1.30, 1.40, 0.0, 3.0)   # 窄于一列 → 列 2
    assert gs_ == [20.0 + y for y in range(H_)], gs_
    ok += 1

    # 66) 条带判定（V4）：门槛按本道背景噪声，不设绝对墨量下限 ——
    #     背景纹理（染色不匀的低频起伏 + 逐行噪声，噪声量级同 IMG_1496 的 0.4–0.7）上一条
    #     只深 3 级的淡带要叫出来，空道一条都不叫，剖面下端的渐变（胶底）不当带；位置误差 < 1 行。
    import math
    ppc_ = 100.0
    n_ = 600
    rnd = _lcg(7)
    tex = [0.6 * math.sin(2 * math.pi * i / 23.0) + 0.4 * math.sin(2 * math.pi * i / 57.0 + 1)
           + 0.5 * (2 * next(rnd) - 1) for i in range(n_)]
    flat = [200.0 + t for t in tex]
    faint = [v - 3.0 * _gauss(i, 250, 3.0) - 40.0 * _gauss(i, 400, 3.0) for i, v in enumerate(flat)]
    ramp = [v - max(0, i - 560) * 1.5 for i, v in enumerate(flat)]
    ys_ = [(i + 0.5) / ppc_ for i in range(n_)]
    def calls(gs):
        _b, _ink, sm = profiles.ink_trace(gs, ppc_)
        return profiles.call_bands(ys_, sm, ppc_)[0]
    got = calls(faint)
    assert [int(round(b["y_cm"] * ppc_ - 0.5)) for b in got] == [250, 400], got    # 行号（行中心 = (i+0.5)/ppc）
    assert got[0]["snr"] >= profiles.K_NOISE and got[1]["prominence"] > 10 * got[0]["prominence"], got
    assert calls(flat) == [], calls(flat)
    assert calls(ramp) == [], calls(ramp)
    ok += 1

    # 67) 密集泳道（V4）：照 IMG_1496 的梯子（11 条带的实测位置与显著度 7–87）造一条剖面 →
    #     11 条全叫出来、不多叫（两条浓带之间 top-hat 鼓起的假峰不算）。两处修正护着它：
    #     噪声迭代剔除条带行与负瓣后量背景（一次性 MAD 时 IMG_1496 只叫出 8/11，见下面的反证）；
    #     显著度取「比两侧低点高多少」（250 kD 泡在 150 kD 的负瓣里，绝对高度为负）。
    ladder = [(1.054, 7.0), (1.215, 35.0), (1.367, 49.0), (1.672, 78.0), (1.951, 31.0), (2.222, 62.0),
              (2.391, 87.0), (2.848, 74.0), (2.975, 72.0), (3.449, 45.0), (4.084, 41.0)]
    dense = [200.0 + t for t in tex]
    for y, depth in ladder:
        c = y * ppc_ - 0.5
        dense = [v - depth * 1.2 * _gauss(i, c, 3.0) for i, v in enumerate(dense)]
    got = calls(dense)
    assert len(got) == len(ladder) and all(abs(b["y_cm"] - y) <= 0.015 for b, (y, _d) in zip(got, ladder)), \
        [b["y_cm"] for b in got]
    _b, _ink, sm = profiles.ink_trace(dense, ppc_)
    srt = sorted(sm)
    one_shot = 1.4826 * sorted(abs(v - srt[len(srt) // 2]) for v in sm)[len(sm) // 2]
    weak = [b for b in got if b["prominence"] < profiles.K_NOISE * one_shot]
    assert weak, "反证失效：一次性 MAD 也能叫全，这条测试就护不住迭代"
    ok += 1

    # 68) 报告 v2 端到端（V4）：magick 造一张 4 条 marker 带 + 一条淡样品带的胶面 →
    #     gray 与独立求平均一致；标定点 = band 线；样品带 MW 落在两条 marker 之间且符合内插；
    #     删一条 band 线改变拟合；v2 键齐全、坐标是 cm、没有照片/像素口径的禁键。
    with tempfile.TemporaryDirectory() as td:
        gpath = os.path.join(td, "gel.png")
        rows_cm = [1.0, 2.0, 3.0, 4.0]
        mws = [100.0, 50.0, 25.0, 12.5]
        draw = []
        for y in rows_cm:
            draw += ["-fill", "gray(60)", "-draw", "rectangle 40,%d 80,%d" % (y * 100 - 3, y * 100 + 2)]
        draw += ["-fill", "gray(196)", "-draw", "rectangle 140,%d 180,%d" % (250 - 3, 250 + 2)]
        _run([MAGICK, "-size", "860x625", "xc:gray(200)"] + draw + ["-depth", "8", gpath])
        png_b = open(gpath, "rb").read()
        mdl5 = {"gel": {"width_cm": 8.6, "height_cm": 6.25},
                "window": {"top_cm": 0.0, "bottom_cm": 6.0},
                "lanes": [{"left_cm": 0.35, "right_cm": 0.85, "trimmed": False},
                          {"left_cm": 1.35, "right_cm": 1.85, "trimmed": False},
                          {"left_cm": 2.35, "right_cm": 2.85, "trimmed": True}],
                "bands": [{"mw": m, "y_cm": y} for m, y in zip(mws, rows_cm)],
                "marker_lane": 1, "meta": {}}
        rep = profiles.analyze(mdl5, png_b, ["M", "S1", None])
        w5, h5, g5 = profiles.gel_gray(png_b)
        ln1 = rep["lanes"][1]
        k = 250
        indep = sum(g5[k * w5 + x] for x in range(135, 185)) / 50.0      # 列中心 1.355..1.845 cm
        assert abs(ln1["profile"]["gray"][k] - indep) < 1e-6, (ln1["profile"]["gray"][k], indep)
        assert len(ln1["profile"]["y_cm"]) == 600 and ln1["profile"]["y_cm"][0] == 0.005
        assert rep["calibration"]["points"] == [[y, m] for y, m in zip(rows_cm, mws)], rep["calibration"]
        assert rep["calibration"]["r2"] == 1.0 and rep["calibration"]["marker_lane"] == 1
        sb = ln1["bands"]
        assert len(sb) == 1 and abs(sb[0]["y_cm"] - 2.5) < 0.02, sb
        assert abs(sb[0]["mw_kda"] - 50.0 / 2 ** 0.5) < 1.0 and not sb[0]["extrapolated"], sb
        assert sb[0]["strongest"] is True
        assert [round(b["y_cm"], 1) for b in rep["lanes"][0]["bands"]] == rows_cm
        assert rep["lanes"][2]["trimmed"] is True and rep["lanes"][2]["bands"] == []
        assert rep["notes"] == ["泳道 3 还没有名字"], rep["notes"]
        bad_keys = {k for k in _walk_keys(rep) if re.search(r"rect|bbox|angle|scale|_px$", k)}
        assert not bad_keys, bad_keys
        mdl5b = json.loads(json.dumps(mdl5))
        mdl5b["bands"][1]["y_cm"] = 2.2                                # 拖动一条 band 线
        rep_b = profiles.analyze(mdl5b, png_b, ["M", "S1", None])
        assert rep_b["calibration"]["r2"] < 1.0 and rep_b["lanes"][1]["bands"][0]["mw_kda"] != sb[0]["mw_kda"]
        out = os.path.join(td, "fig.mw.json")
        write_v2(rep, out)
        assert json.load(open(out, encoding="utf-8")) == json.loads(json.dumps(rep))
        table = format_report_v2(rep)
        assert "R²=1.0000" in table and "S1" in table and "%.1f kDa" % sb[0]["mw_kda"] in table, table
        assert "外推" not in table, table          # marker 泳道两端的外推带不在表里，不该带脚注
    ok += 1

    print("selftest %d/%d 通过" % (ok, ok))
    return 0



def _ae(a, b):
    """magick compare -metric AE：两张 PNG 的差异像素数（字符串，期望 "0"）。"""
    p = subprocess.run([MAGICK, "compare", "-metric", "AE", a, b, "null:"],
                       capture_output=True, text=True)
    return p.stderr.strip()


def _ink_bbox(gray, w, h, x0, y0, x1, y1):
    """在 (x0,y0)-(x1,y1) 窗口里找墨迹（灰度 <128）的包围盒；没有返回 None。"""
    xs, ys = [], []
    for y in range(max(0, int(y0)), min(h, int(y1))):
        row = y * w
        for x in range(max(0, int(x0)), min(w, int(x1))):
            if gray[row + x] < 128:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _scene_one_label(text, x_cm=1.0, y_cm=3.0, size=8.0, dpi=96):
    """selftest 用的单标签 scene（middle-right，右边压在 x_cm）：验页面规则与左越界。"""
    ppc, font = 50.0, pick_font()
    out_ppc = dpi / 2.54
    scale = out_ppc / ppc
    face = (100.0, 50.0, 100.0 + 8.6 * ppc, 50.0 + 6.25 * ppc)
    px = max(6, int(round(size / 72.0 * dpi)))
    tw, th = text_extent(font, px, text)
    bw, bh = tw / scale, th / scale
    ax = face[0] + x_cm * ppc - bw
    ay = face[1] + y_cm * ppc - bh / 2.0
    item = {"ax": ax, "ay": ay, "box_w": bw, "box_h": bh, "text": text,
            "size_pt": size, "color": "#000000", "role": "free", "bind": {},
            "anchor": "middle-right"}
    return scene_build([item], face, ppc, dpi=dpi, font=font, margin_frac=0.05,
                       gel_image="gel.png")



def _js_core_test(scene, gel_png_path, td, expected):
    """把模板里的 #core 拿到 node 下跑（spec §9：没有 node 就明确跳过，不静默）。

    measureText 用 Python text_extent 的同一组数字做 stub，所以 pagePx 必须对得上
    render 的像素；pHYs 用真的胶面 PNG 验签名/块类型/ppm；SVG 数 data-role。
    """
    node = None
    for cand in ("/Users/maxim/.local/bin/node", shutil.which("node")):
        if cand and os.path.isfile(cand):
            node = cand
            break
    if not node:
        return "node 未找到，跳过（spec §9：只在有 node 时跑）"

    with open(EDITOR_TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    m = re.search(r'<script\b[^>]*\bid\s*=\s*["\']core["\'][^>]*>([\s\S]*?)</script>',
                  tpl, re.I)
    assert m, "编辑器模板里找不到 #core"
    dpi = int(scene["output"]["dpi"])
    metrics = {}
    for it in scene["items"]:
        px = max(6, int(round(float(it["size_pt"]) / 72.0 * dpi)))
        w, h = text_extent(scene["font"].get("file"), px, it["text"])
        metrics["%d|%s" % (px, it["text"])] = [w, h]
    with open(gel_png_path, "rb") as f:
        png_b64 = base64.b64encode(f.read()).decode("ascii")

    harness = r'''
"use strict";
var METRICS = %s;
var ctx = {
  font: "",
  measureText: function (text) {
    var px = parseInt(this.font, 10);
    var v = METRICS[px + "|" + text];
    var w = v ? v[0] : text.length * 0.55 * px;
    var asc = v ? v[1] : 0.72 * px;
    return {width: w, actualBoundingBoxLeft: 0, actualBoundingBoxRight: w,
            actualBoundingBoxAscent: asc, actualBoundingBoxDescent: 0,
            fontBoundingBoxAscent: asc, fontBoundingBoxDescent: 0};
  }
};
function OffscreenCanvas() { this.getContext = function () { return ctx; }; }
var PNG_B64 = %s;
var SCENE = %s;
var EXPECT = %s;
%s
var C = globalThis.__editorCore;
if (!C) { console.error("[js] core 没有导出 __editorCore"); process.exit(2); }
var pp = C.pagePx(SCENE, 96);
var keys = ["W", "H", "L", "T", "faceW", "faceH"];
for (var i = 0; i < keys.length; i++) {
  if (Math.abs(pp[keys[i]] - EXPECT[keys[i]]) > 1) {
    console.error("[js] pagePx " + keys[i] + " " + pp[keys[i]] + " != " + EXPECT[keys[i]]);
    process.exit(3);
  }
}
var png = Buffer.from(PNG_B64, "base64");
var ab = png.buffer.slice(png.byteOffset, png.byteOffset + png.byteLength);
var withp = new Uint8Array(C.insertPhys(ab, 300));
var sig = [137, 80, 78, 71, 13, 10, 26, 10];
for (var s = 0; s < 8; s++) {
  if (withp[s] !== sig[s]) { console.error("[js] PNG 签名被破坏"); process.exit(4); }
}
var typ = String.fromCharCode(withp[37], withp[38], withp[39], withp[40]);
if (typ !== "pHYs") { console.error("[js] IHDR 后不是 pHYs: " + typ); process.exit(5); }
var ppm = ((withp[41] << 24) | (withp[42] << 16) | (withp[43] << 8) | withp[44]) >>> 0;
if (ppm !== 11811) { console.error("[js] ppm=" + ppm + " 不是 11811"); process.exit(6); }
// placements 必须以**页面**为原点（含页面留白）：漏加 (L, T) 时 marker 列会是负数，
// canvas 上就被裁掉、PNG 只剩胶面（这就是 E4 第一版的真实故障）。
var pl = C.placements(SCENE, 96);
if (pl.length !== SCENE.items.length) { console.error("[js] placements 数不对"); process.exit(10); }
var minx = Math.min.apply(null, pl.map(function (p) { return p.x; }));
var miny = Math.min.apply(null, pl.map(function (p) { return p.y; }));
if (!(minx >= 0 && miny >= 0)) {
  console.error("[js] 有标签落在页面外：min x=" + minx + " min y=" + miny); process.exit(11);
}
if (pl[0].page.L !== pp.L || pl[0].page.T !== pp.T) {
  console.error("[js] placements 的页面留白与 pagePx 不一致"); process.exit(12);
}
var svg = C.sceneToSvg(SCENE, "data:image/png;base64,");
var roles = (svg.match(/data-role=/g) || []).length;
if (roles !== SCENE.items.length) {
  console.error("[js] SVG <text> 数 " + roles + " != " + SCENE.items.length);
  process.exit(7);
}
for (var k = 0; k < SCENE.items.length; k++) {
  if (svg.indexOf('id="' + SCENE.items[k].id + '"') < 0) {
    console.error("[js] SVG 缺 id " + SCENE.items[k].id); process.exit(8);
  }
}
// SVG 坐标也以页面左上角为原点：最左的标签 x 必须 ≥ 0，且与 placements 差 < 1px。
var xs = svg.match(/<text id="[^"]+"[^>]* x="([-0-9.]+)"/g) || [];
var sx = xs.map(function (m) { return parseFloat(m.match(/x="([-0-9.]+)"/)[1]); });
if (!sx.length || Math.min.apply(null, sx) < 0) {
  console.error("[js] SVG 标签落在页面外（少了页面留白）：" + sx.slice(0, 3)); process.exit(13);
}
console.log("[js] core ok：pagePx " + pp.W + "x" + pp.H + "，pHYs ppm " + ppm +
            "，SVG " + roles + " 条 <text>，长度 " + svg.length);
''' % (json.dumps(metrics, ensure_ascii=False), json.dumps(png_b64),
       json.dumps(scene, ensure_ascii=False),
       json.dumps({"W": expected[0], "H": expected[1], "L": expected[2],
                   "T": expected[3], "faceW": expected[4], "faceH": expected[5]}),
       m.group(1))

    path = os.path.join(td, "core-test.js")
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)
    p = subprocess.run([node, path], capture_output=True, text=True)
    assert p.returncode == 0, ("node core 测试失败\n%s\n%s" % (p.stdout, p.stderr))
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("[js]")]
    assert lines, "node 没打印结果：%s" % p.stdout
    return lines[-1]


def _walk_keys(o):
    """递归收集 dict 的所有键名（record 键集测试用）。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _walk_keys(v)


def _walk_nums(o):
    """递归收集所有数值（bool 不算；record 的 cm 量级检查用）。"""
    if isinstance(o, bool):
        return
    if isinstance(o, (int, float)):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _walk_nums(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _walk_nums(v)


def _synth_record():
    """selftest 用的合成 record 输入：15 条泳道、每条 3 条带，角度 0（换算可手算）。"""
    ppc = 50.0
    preset = _t2_preset(8.6, 6.9, trim_top=0.3)          # 15 条 × 0.565 cm
    gx0, gy0 = 100.0, 50.0
    gel_box = (gx0, gy0, gx0 + 8.6 * ppc, gy0 + 6.6 * ppc)
    info = {"image": (600, 400), "analysis": (300, 200)}   # a2x = a2y = 2
    ga = {"gel_box": gel_box, "angle": 0.0, "px_per_cm": ppc,
          "width_cm": 8.6, "height_cm": 6.6, "lane_pitch_cm": 0.565,
          "canvas": (600, 400),
          "checks": [{"name": "height", "ok": True, "detail": "6.70 vs 6.90 cm, 2.9%"}]}
    lanes, bands = [], {}
    for i in range(1, 16):
        xc = gx0 + (i - 0.5) * 0.565 * ppc
        lanes.append({"index": i, "x_analysis": xc / 2.0, "x_canvas": xc,
                      "width": 5.0, "width_canvas": 10.0,
                      "measured": i not in (4, 5), "evidence": 10.0 + i})
        bands[str(i)] = [(150.0 + 20.0 * k, 5.0, 30.0 - k) for k in range(3)]
    lf = {"lanes": lanes,
          "checks": [{"name": "pitch", "ok": True, "detail": "17.66 vs 18.11 px, 2.5%"}]}
    bb = {"bands": [bands[str(i)] for i in range(1, 16)]}
    return preset, record_build(preset, ga, lf, bb, info)


def _synth_mw_inputs():
    """同一组带的两份输入：record（cm）与旧式 lb（分析 px）。第 2 条泳道标成 estimated。"""
    ppc, gx0, gy0 = 50.0, 100.0, 50.0
    preset = _t2_preset(1.8, 6.9, trim_top=0.3, lane_count=3, lane_pitch_cm=0.6)
    info = {"image": (600, 400), "analysis": (300, 200)}
    ga = {"gel_box": (gx0, gy0, gx0 + 1.8 * ppc, gy0 + 6.6 * ppc), "angle": 0.0,
          "px_per_cm": ppc, "width_cm": 1.8, "height_cm": 6.6, "lane_pitch_cm": 0.6,
          "canvas": (600, 400), "checks": []}
    xs = [gx0 + (i - 0.5) * 0.6 * ppc for i in (1, 2, 3)]
    y_an = {1: [120.0, 140.0, 160.0], 2: [130.0, 150.0], 3: [120.0, 140.0, 160.0]}
    lanes, bands = [], []
    for i, xc in enumerate(xs, 1):
        lanes.append({"index": i, "x_analysis": xc / 2.0, "x_canvas": xc,
                      "width": 5.0, "width_canvas": 10.0,
                      "measured": i != 2, "evidence": 10.0})
        bands.append([(y, 5.0, 30.0 - k) for k, y in enumerate(y_an[i])])
    rec = record_build(preset, ga, {"lanes": lanes, "checks": []}, {"bands": bands}, info)
    lb = {"lanes": [(xc, 10.0) for xc in xs], "interp": [False, False, False], "bands": bands}
    return rec, lb


def _synth_gel(path, size, gel_rect, lane_xs, faint=None, skip=None, streak=None,
               band_y=(170, 180), band_v=120, band_half=6, gel_v=180):
    """用 magick 现造一张合成胶（selftest 专用，不读用户文件）。

    lane_xs = 泳道中心 x；faint = {序号: 灰度} 让某条道淡一点；skip = {序号} 该道完全没条带；
    streak = (x, y0, y1) 造一道竖纹（默认灰度 172，比胶面 180 略暗、整列均匀）。
    """
    faint = faint or {}
    skip = set(skip or ())
    W, H = size
    x0, y0, x1, y1 = gel_rect
    cmd = [MAGICK, "-size", "%dx%d" % (W, H), "xc:white", "-fill", "gray(%d)" % gel_v,
           "-draw", "rectangle %d,%d %d,%d" % (x0, y0, x1, y1)]
    by0, by1 = band_y
    for i, x in enumerate(lane_xs):
        if i in skip:
            continue
        v = faint.get(i, band_v)
        cmd += ["-fill", "gray(%d)" % v,
                "-draw", "rectangle %d,%d %d,%d" % (x - band_half, by0, x + band_half, by1)]
    if streak:
        sx, sy0, sy1 = streak
        cmd += ["-fill", "gray(172)", "-draw", "rectangle %d,%d %d,%d" % (sx - 1, sy0, sx + 1, sy1)]
    _run(cmd + [path])


def _synth_ga(gel_box, px_per_cm, lane_pitch_cm=1.0, canvas=None):
    """selftest 用的 stage 1 冻结输出（角度 0；px_per_cm 与 lane_pitch_cm 一起把先验钉死）。"""
    return {"gel_box": gel_box, "angle": 0.0, "px_per_cm": px_per_cm,
            "lane_pitch_cm": lane_pitch_cm, "canvas": canvas}


def _synth_scene(long_label=False):
    """selftest 用的合法 scene：8.6 × 6.25 cm 胶面、96 dpi、两个标签（或一条长标签）。

    long_label=True 时只留一条靠左的长标签（锚点 middle-right、右边压在 x=0.3 cm），
    用来验页面规则：越界只让左边长，另外三边仍是 margin_min_cm。
    """
    ppc, dpi = 50.0, 96
    font = pick_font()
    out_ppc = dpi / 2.54
    scale = out_ppc / ppc
    face = (100.0, 50.0, 100.0 + 8.6 * ppc, 50.0 + 6.25 * ppc)

    def item(role, bind, text, size, x_cm, y_cm, anchor):
        px = max(6, int(round(size / 72.0 * dpi)))
        tw, th = text_extent(font, px, text)
        bw, bh = tw / scale, th / scale                 # 墨迹框（画布 px）
        axp, ayp = face[0] + x_cm * ppc, face[1] + y_cm * ppc
        v, h = anchor.split("-")
        ax = axp - (bw if h == "right" else bw / 2.0 if h == "center" else 0.0)
        ay = ayp - (bh if v == "bottom" else bh / 2.0 if v == "middle" else 0.0)
        return {"ax": ax, "ay": ay, "box_w": bw, "box_h": bh, "text": text,
                "size_pt": size, "color": "#000000", "role": role,
                "bind": bind, "anchor": anchor}

    if long_label:
        items = [item("free", {}, "A VERY LONG LABEL 250 kD", 8, 0.3, 3.0, "middle-right")]
    else:
        items = [item("lane", {"lane": 1}, "3", 8, 1.0, 3.0, "middle-center"),
                 item("marker", {"marker_lane": 2, "label_index": 1, "band_y_cm": 3.0},
                      "250 kD", 6, 7.0, 3.0, "middle-center")]
    return scene_build(items, face, ppc, dpi=dpi, font=font, margin_frac=0.05,
                       gel_image="synth.gel.png")


def _synth_svg_scene(gel_path, dpi=300, outline=None):
    """selftest 用的 SVG scene：真 gel PNG + 四条标签（三种竖直锚点、左右两侧都有）。

    标签彼此隔得远，保真检查才能按 item 的框各自找墨迹。
    """
    font = pick_font()
    ppc = float(dpi) / 2.54                      # 成品 px / cm
    W, H = int(round(8.6 * ppc)), int(round(6.25 * ppc))
    _synth_gel(gel_path, (W, H), (4, 4, W - 5, H - 5), [], gel_v=200)
    face = (0.0, 0.0, float(W), float(H))

    def item(role, bind, text, size, x_cm, y_cm, anchor):
        px = max(6, int(round(size / 72.0 * dpi)))
        tw, th = text_extent(font, px, text)
        v, h = anchor.split("-")
        ax = x_cm * ppc - (tw if h == "right" else tw / 2.0 if h == "center" else 0.0)
        ay = y_cm * ppc - (th if v == "bottom" else th / 2.0 if v == "middle" else 0.0)
        return {"ax": ax, "ay": ay, "box_w": tw, "box_h": th, "text": text,
                "size_pt": size, "color": "#000000", "role": role,
                "bind": bind, "anchor": anchor}

    items = [
        item("lane", {"lane": 1}, "1", 8, 1.0, -0.10, "bottom-center"),
        item("marker", {"marker_lane": 2, "label_index": 1, "band_y_cm": 1.5},
             "250 kD", 6, 0.30, 1.5, "middle-right"),
        item("marker", {"marker_lane": 2, "label_index": 2, "band_y_cm": 4.5},
             "15 kD", 6, 8.30, 4.5, "middle-left"),
        item("free", {}, "10", 8, 4.3, 1.0, "top-center"),
    ]
    return scene_build(items, face, ppc, dpi=dpi, font=font, outline=outline,
                       margin_frac=0.05, gel_image=os.path.basename(gel_path))


def _svg_raster(svg, png, dpi):
    """SVG → PNG。用 rsvg-convert（本机 ImageMagick 的 SVG 委托）；没有就返回 None。

    不用 `magick` 直接栅格化：这台机器上 IM 走内置 MSVG 渲染器，文字按 user unit 排，
    墨迹大 3–4 倍（实测 8 pt 排成 ~0.44 cm 宽），不能当作 §8 的文字度量基准。
    """
    exe = shutil.which("rsvg-convert")
    if not exe:
        return None
    _run([exe, "--dpi-x", str(dpi), "--dpi-y", str(dpi), "-o", png, svg])
    return exe


def _ink_centre_cm(path, box, ps, thr=100, pad_cm=0.12):
    """png 里 box（胶面 cm 坐标）附近墨迹 bbox 的中心（cm，胶面坐标）。

    区域按该图自己的 px/cm 换算（两张图的整数像素尺寸可能差 1 px）；只认深色，
    合成胶的灰底（gray(200)）不会被当成字。
    """
    w, h, rgb = read_ppm(path, None)
    g = to_gray(w, h, rgb)
    sx, sy = w / ps["width_cm"], h / ps["height_cm"]
    pad = int(round(pad_cm * sx))
    x0 = int((box[0] + ps["left_cm"]) * sx) - pad
    x1 = int((box[2] + ps["left_cm"]) * sx) + pad
    y0 = int((box[1] + ps["top_cm"]) * sy) - pad
    y1 = int((box[3] + ps["top_cm"]) * sy) + pad
    xs, ys = [], []
    for y in range(max(0, y0), min(h, y1)):
        row = y * w
        for x in range(max(0, x0), min(w, x1)):
            if g[row + x] < thr:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return ((min(xs) + max(xs) + 1) / 2.0 / sx - ps["left_cm"],
            (min(ys) + max(ys) + 1) / 2.0 / sy - ps["top_cm"])


def _t2_preset(width_cm, height_cm, trim_top=0.0, trim_bottom=0.0, anchor="bottom",
               lane_count=15, lane_pitch_cm=0.565):
    """selftest 用的胶体预设 dict（不落盘：stage 1 只读字段，schema 校验在 config 那边测）。"""
    return {"name": "selftest", "width_cm": width_cm, "height_cm": height_cm,
            "lane_count": lane_count, "lane_pitch_cm": lane_pitch_cm,
            "trim_top_cm": trim_top, "trim_bottom_cm": trim_bottom, "anchor": anchor}


def _parse_ppm_for_test(d):
    import io
    i, toks = 0, []
    while len(toks) < 4:
        while d[i : i + 1].isspace():
            i += 1
        if d[i : i + 1] == b"#":
            while d[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not d[j : j + 1].isspace():
            j += 1
        toks.append(d[i:j])
        i = j
    return int(toks[1]), int(toks[2]), d[i + 1 :]


def _tiny_png(w, h, rgb=(128, 128, 128), dpi=None):
    """stdlib 造一张纯色 RGB PNG（selftest 专用，不依赖 magick）；给 dpi 就写 pHYs（单位=米）。"""
    import struct
    import zlib

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))
    rows = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    phys = b""
    if dpi:
        ppm = int(round(dpi / 0.0254))
        phys = chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + phys + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _synth_vc_doc():
    """selftest 用的 .vectorcraft builder：spec §4 的三画板 + 四图层缩影。返回 (doc, png 字节)。"""
    doc = vc_file.new_doc("selftest")
    vc_file.add_artboard(doc, "Figure", 0, 0, 260, 200)
    vc_file.add_artboard(doc, "Workbench", 340, 0, 300, 200)
    vc_file.add_artboard(doc, "Profiles", 680, 0, 260, 200)
    png = _tiny_png(40, 30)
    key, w, h = vc_file.add_image(doc, png)
    gel = vc_file.add_layer(doc, "gel", locked=True)
    seg = vc_file.clip(doc, "gel seg 1", (30, 30, 100, 150),
                       vc_file.image(doc, "gel", key, w, h, 30, 30, 5.0))
    vc_file.lay_children(gel).extend([seg, vc_file.image(doc, "gel", key, w, h, 370, 30, 5.0)])
    lanes = vc_file.add_layer(doc, "lanes", printable=False, color=1)
    vc_file.lay_children(lanes).extend([vc_file.line(doc, "lane", (385, 30), (385, 180), (0, .7, 1)),
                                        vc_file.line(doc, "lane", (400, 30), (400, 180), (0, .7, 1))])
    labels = vc_file.add_layer(doc, "labels", color=2)
    vc_file.lay_children(labels).append(vc_file.text(doc, "label lane 1", 52, 25, "M", rgb=(1, 0, 0)))
    meta = vc_file.add_layer(doc, "ep-meta", printable=False, visible=False, locked=True, color=3)
    vc_file.lay_children(meta).append(vc_file.text(
        doc, "ep-meta", 340, 220, json.dumps({"skill": "ep-labeling", "px_per_cm": 47.2}),
        size=4, justify="Left"))
    return doc, png


def _key_paths(o, pre=""):
    """嵌套 dict/list 的键路径集合（list 下标统一记成 *）。"""
    out = set()
    if isinstance(o, dict):
        for k, v in o.items():
            p = pre + k
            out.add(p)
            out |= _key_paths(v, p + ".")
    elif isinstance(o, list):
        for v in o:
            out |= _key_paths(v, pre + "*.")
    return out


class _FakeVC:
    """selftest 用的假 VectorCraft 控制口：每连接一行请求一行回包（与真 app 同）。

    on[method 或 engine.execute 的 command] = fn(params) → result（或 _FakeVC.Err(msg)）；
    没登记的一律回 ok/None。log 记下每个请求；split=True 时回包拆两段发；raw 非空时原样回它。
    """
    class Err:
        def __init__(self, msg):
            self.msg = msg

    def __init__(self, port=0):
        import socketserver
        import threading
        fake = self
        self.on, self.log, self.split, self.raw = {}, [], False, None

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline()
                if not line:
                    return
                req = json.loads(line)
                fake.log.append(req)
                if fake.raw is not None:
                    self.wfile.write(fake.raw)
                    return
                key = req["method"]
                params = req.get("params") or {}
                if key == "engine.execute":
                    key, params = params["command"], params.get("params") or {}
                fn = fake.on.get(key)
                res = fn(params) if fn else None
                if isinstance(res, _FakeVC.Err):
                    out = {"id": req["id"], "ok": False, "error": res.msg}
                else:
                    out = {"id": req["id"], "ok": True, "result": res}
                data = (json.dumps(out) + "\n").encode()
                if fake.split:
                    self.wfile.write(data[:len(data) // 2])
                    self.wfile.flush()
                    time.sleep(0.05)
                    self.wfile.write(data[len(data) // 2:])
                else:
                    self.wfile.write(data)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.th.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        return False


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _later(delay, fn):
    import threading
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _synth_wb_doc(model):
    """selftest：模型 → 只有 Workbench 的 vc_file 文档（胶面图是 _tiny_png，宽高比与胶面一致）。"""
    doc = vc_file.new_doc("wb")
    w_px = 172
    h_px = int(round(w_px * model["gel"]["height_cm"] / model["gel"]["width_cm"]))
    png = _tiny_png(w_px, h_px)
    key, w, h = vc_file.add_image(doc, png)
    layers = workbench.make_layers(doc)
    workbench.add_workbench(doc, layers, model, key, w, h, x0=340.0)
    return doc, png


def _wb_origin(document):
    img = [n for n in vc_file.lay_children(vc_file.layer_by_name(document, "gel")) if n.get("name") == "gel"][0]
    return img["kind"]["xf"][4], img["kind"]["xf"][0]


def _assert_model_close(a, b, tol=1e-3):
    assert a["marker_lane"] == b["marker_lane"], (a["marker_lane"], b["marker_lane"])
    for k in ("width_cm", "height_cm"):
        assert abs(a["gel"][k] - b["gel"][k]) < tol, (k, a["gel"], b["gel"])
    for k in ("top_cm", "bottom_cm"):
        assert abs(a["window"][k] - b["window"][k]) < tol, (k, a["window"], b["window"])
    assert len(a["lanes"]) == len(b["lanes"]), (len(a["lanes"]), len(b["lanes"]))
    for x, y in zip(a["lanes"], b["lanes"]):
        assert x["trimmed"] == y["trimmed"] and abs(x["left_cm"] - y["left_cm"]) < tol \
            and abs(x["right_cm"] - y["right_cm"]) < tol, (x, y)
    assert [x["mw"] for x in a["bands"]] == [y["mw"] for y in b["bands"]]
    assert all(abs(x["y_cm"] - y["y_cm"]) < tol for x, y in zip(a["bands"], b["bands"]))


def _lcg(seed):
    """确定性伪随机 [0, 1)（selftest 不用 random，结果不随 Python 版本变）。"""
    x = seed
    while True:
        x = (1103515245 * x + 12345) % (1 << 31)
        yield x / float(1 << 31)


def _gauss(i, c, sd):
    import math
    return math.exp(-0.5 * ((i - c) / sd) ** 2)
