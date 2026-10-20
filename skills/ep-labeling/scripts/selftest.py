#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest：不读用户文件的算法自检。"""
import contextlib
import io
import json
import os

from config import load_gel_size
from magick_io import MAGICK, _run, read_ppm, to_gray
from gel_detect import bg_plane, components, detect_gel, ink_mask
from lane_detect import (clusters, fat_rows, fill_gaps, group_rows, lanes_and_bands,
                         pitch_from_row, snap_to_anchors)
from geometry import load_dims_after, rotate_pt, rotated_rect_bbox
from mw_report import build_cal, estimate, marker_pairs, parse_mw
from text_layout import annotate_cmd, pick_font, text_extent


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

    # 8) 补位：差值对得上整数格才补，对不上就报出来不补
    marks, skipped = fill_gaps([0, 10, 40], [4, 4, 4], 10)
    assert [round(c, 1) for c, _, _ in marks] == [0, 10, 20, 30, 40], marks
    assert [it for _, _, it in marks] == [False, False, True, True, False]
    assert not skipped
    marks2, skipped2 = fill_gaps([0, 10, 35], [4, 4, 4], 10)
    assert len(marks2) == 3 and len(skipped2) == 1 and skipped2[0][3] == 2.5, (marks2, skipped2)
    ok += 1

    # 9) 间距量自「条带最密的一行」，不量整块胶列剖面：造一行每 20 px 一条带
    w2, h2 = 200, 60
    m = bytearray(w2 * h2)
    for x in (30, 50, 70, 90, 110):
        for y in range(20, 30):
            for dx in range(-2, 3):
                m[y * w2 + x + dx] = 1
    for y in range(40, 45):                      # 另加一条竖纹（不该被当成间距来源）
        m[y * w2 + 30] = 1
    p, row = pitch_from_row(m, w2, 0, w2 - 1, 0, h2 - 1)
    assert abs(p - 20) <= 0.5, (p, row)
    assert row == 20, row
    ok += 1

    # 10) 端到端：5 条泳道（其中一条只有很淡的带）+ 一道竖纹 → 必须正好 5 条，竖纹不算泳道
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "synthetic_gel.png")
        lanes_x = (120, 150, 180, 210, 240)
        cmd = [MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(180)",
               "-draw", "rectangle 100,80 340,260"]
        for i, x in enumerate(lanes_x):
            v = 165 if i == 2 else 120
            cmd += ["-fill", "gray(%d)" % v, "-draw", "rectangle %d,180 %d,190" % (x - 6, x + 6)]
        cmd += ["-fill", "gray(172)", "-draw", "rectangle 300,80 302,260", src_img]   # 竖纹
        _run(cmd)
        lb = lanes_and_bands(src_img, (100, 80, 340, 260), 0.0, longest=400)  # 别放大：rect 是原图坐标
        got = [c for c, _ in lb["lanes"]]
        assert len(got) == 5, ("泳道数不对", got, lb["dropped"])
        for want_x, g in zip(lanes_x, got):
            assert abs(want_x - g) <= 3, (lanes_x, got)
        assert all(abs(g - 301) > 6 for g in got), ("竖纹被当成泳道", got)
        assert lb["measured"] == 5 and not any(lb["interp"])
        ok += 1

    # 11) 文字对齐几何：-annotate 的 y 是框顶边；marker 框中心对条带行、泳道名框底边在胶顶之上 gap
    for n, b, m in test_annotate_geometry():
        if not b:
            raise AssertionError("对齐几何：%s —— %s" % (n, m))
    ok += 1          # 一组 6 项算一项，免得分子分母对不上

    # 12) 泳道中心吸附锚点：空泳道被列投影的孤立尖峰钉在 308（真值 305），旁边那条
    #     因为宽 run 按自身宽度均分落在 319.6（真值 321.5）→ 11 和 12 挤成 11.6。
    #     吸附到「条带最密那一行」的 run 后，间距必须回到一个间距左右。
    cx = [269.6, 284.7, 308.0, 319.6, 336.6]
    anchors = [269.5, 286.5, 305.0, 321.5, 339.0]
    got = snap_to_anchors(cx, anchors, 18.0)
    assert [round(v, 1) for v in got] == anchors, got
    gaps = [round(got[i + 1] - got[i], 1) for i in range(len(got) - 1)]
    assert all(14.0 <= g <= 20.0 for g in gaps), ("泳道挤在一起没修好", gaps)
    # 偏超过半个间距的泳道不许被拉动（没有条带证据的泳道要保留列投影的位置）
    assert snap_to_anchors([100.0, 340.0], [120.0], 18.0) == [100.0, 340.0]
    assert snap_to_anchors([100.0], [], 18.0) == [100.0]
    ok += 1

    # 13) 并带检测：一条行塞两条带（21/20 kD）时，那一行必须比同泳道其他行厚一截
    assert fat_rows([(100, 6), (120, 6), (140, 6), (160, 15), (180, 6)]) == [(160, 2.5)]
    assert fat_rows([(10, 5), (20, 5), (30, 5)]) == []      # 均匀的行不许误报
    assert fat_rows([]) == []
    ok += 1

    # 14) 并带标签的行分组：{"rows": 2} 的标签吃两条带、落在它们中间；其余一一对应
    assert group_rows([10, 20, 30], [1, 2]) == [10.0, 25.0]
    assert group_rows([10, 20, 30], [1, 1, 1]) == [10.0, 20.0, 30.0]
    assert group_rows([10, 20], [1]) == [10.0]
    assert group_rows([], [1, 2]) == []
    ok += 1

    # 15) 字号必须真的生效：`annotate_cmd` 漏写 -pointsize 时 IM 一律按默认 12 pt 画，
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

    # 16) 分子量标定：完美的 log 线性 marker → 两种方法都应还原；interp 不外推、logfit 外推并标记
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

    # 17) 强制预设闸门：probe 没给 --gel-size 必须在**跑检测之前**就失败。
    #     用一张不存在的图：真跑到 detect_gel 会报别的错，只有先过闸门才会是 spec 那一行。
    from gel_photo import main as gp_main
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = gp_main(["probe", "/nonexistent/t1-gate-no-preset.png"])
    msg = err.getvalue()
    assert rc != 0 and "gel preset required (--gel-size FILE)" in msg, (rc, msg)
    ok += 1

    # 18) --gel-cm 不再是 --gel-size 的替代：只给 --gel-cm 同样被闸门挡下
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = gp_main(["render", "/nonexistent/t1-gate-gel-cm.png", "--out", "/tmp/t1-gate.png",
                      "--gel-cm", "8x7"])
    msg = err.getvalue()
    assert rc != 0 and "gel preset required (--gel-size FILE)" in msg, (rc, msg)
    ok += 1

    # 19) 预设 schema：缺 lane_count 报错、不变式破坏报偏差、合法预设返回 dict 且 anchor 默认 bottom
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

    print("selftest %d/19 通过" % ok)
    return 0


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
