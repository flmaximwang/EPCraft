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
from gel_area import compute, estimate_pitch_px
from magick_io import MAGICK, _run, read_ppm, to_gray
from gel_detect import bg_plane, components, detect_gel, ink_mask
from lane_select import layout as lane_layout, parse_keep, strips as lane_strips
from geometry import load_dims_after, rotate_pt, rotated_rect_bbox
from mw_report import build_cal, estimate, format_report_v2, marker_pairs, parse_mw, write_v2
import vc_file
import vc_control
import workbench
import profiles
import well_grid
import figure


def test_import_direction():
    """模块不许 import 管线里更靠后的模块（stage 1 → 种子 → 剖面 → 模型 → 文档 → 执行 → CLI）。

    用 ast 解析真正的 import 语句（不 grep 文本：注释/文档里提到模块名不算依赖）。
    返回违规列表，每条写明「哪个模块 import 了哪个」。
    """
    scripts = os.path.dirname(os.path.abspath(__file__))
    # 管线：stage 1（gel_area）→ 泳道种子（well_grid）→ 剖面/条带（profiles）→ 模型（workbench）
    #       → 文档与 sync 计划（figure）→ 控制口执行（sync_exec）→ CLI（gel_photo）。只许依赖前面的。
    later = ["well_grid", "profiles", "workbench", "figure", "sync_exec", "gel_photo"]
    rules = {"gel_area": set(later), "gel_detect": set(later)}
    for i, mod in enumerate(later):
        rules[mod] = set(later[i + 1:])
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

    # 17) 分子量：只在**相邻两条 marker 带之间**插值（禁止全局拟合）。
    #     marker 点上的值必须严格等于标签值；区间外不给数字；弯曲的梯子按局部斜率走，不被一条直线抹平。
    assert parse_mw("21/20 kD") == [21.0, 20.0] and parse_mw("250 kD") == [250.0] and parse_mw("M") == []
    ents = [{"text": "100 kD"}, {"text": "21/20 kD", "rows": 2}, {"text": "10 kD"}]
    pr = marker_pairs(ents, [1, 2, 1], [10, 20, 24, 30], lambda e, d: dict(e))
    assert pr == [(10, 100.0), (20, 21.0), (24, 20.0), (30, 10.0)], pr
    cal = build_cal([(y, 10 ** (2.0 - 0.05 * (y - 10))) for y in (10, 20, 30, 40)])
    assert "fit" not in cal, cal                                   # 标定里没有任何拟合曲线
    for y in (10, 20, 30, 40):                                     # 标记点上 = 标签值
        mw, ex = estimate(y, 100.0, [(100.0, cal)])
        assert abs(mw / 10 ** (2.0 - 0.05 * (y - 10)) - 1) < 1e-12 and not ex, (y, mw, ex)
    mw, ex = estimate(25, 100.0, [(100.0, cal)])                   # 两点之间 = 两点的 log 线性
    assert abs(mw - 10 ** (2.0 - 0.75)) < 1e-9 and not ex, (mw, ex)
    mw, ex = estimate(50, 100.0, [(100.0, cal)])                   # 范围外：不给数字
    assert mw is None and ex
    curve = build_cal([(1.0, 250.0), (2.0, 100.0), (4.0, 30.0), (5.0, 10.0)])   # 弯曲的梯子
    mw, _ = estimate(3.0, 100.0, [(100.0, curve)])
    assert abs(mw / (100.0 * 30.0) ** 0.5 - 1) < 1e-9, mw          # 100↔30 之间走它们自己的斜率
    mw, _ = estimate(4.5, 100.0, [(100.0, curve)])
    assert abs(mw / (30.0 * 10.0) ** 0.5 - 1) < 1e-9, mw           # 30↔10 之间同理
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

    # 32) --trim-lanes 语法：逗号/区间、去重升序；越界/空/反向区间/乱写都报错
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
        assert lab == {"text": "M", "x": 52.0, "y": 25.0, "angle": 0.0, "font": "Arial", "size": 8.0,
                       "color": "#ff0000", "justify": "Center"}, lab
        meta = vc_file.text_info(vc_file.lay_children(vc_file.layer_by_name(d, "ep-meta"))[0])
        assert json.loads(meta["text"]) == {"skill": "ep-craft", "px_per_cm": 47.2}, meta
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
             ("artboard", d["artboards"][0], tpl["artboard"]),
             ("polyline", vc_file.polyline(vc_file.new_doc("p"), "c", [(0, 0), (1, 1), (2, 0)]), tpl["polyline"])]
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

    # 54a) JPEG 内嵌（crop 画板的照片预览）：magick 造基线（SOF0）与渐进（SOF2）两张 JPEG →
    #      add_image 读出的像素尺寸对；write → read 字节不变、images[key].mime 仍是 image/jpeg；
    #      非 JPEG 字节 / 不认识的 mime 要报错。
    with tempfile.TemporaryDirectory() as td:
        jd = vc_file.new_doc("jpeg")
        jlay = vc_file.add_layer(jd, "photo")
        jpgs = {}
        for tag, extra in (("base", []), ("prog", ["-interlace", "JPEG"])):
            jpg = _run([MAGICK, "-size", "37x23", "gradient:red-blue"] + extra + ["-quality", "85", "jpg:-"])
            assert jpg[:2] == b"\xff\xd8", tag
            key, w, h = vc_file.add_image(jd, jpg, "image/jpeg")
            assert (w, h) == (37, 23), (tag, w, h)
            jpgs[key] = jpg
            vc_file.lay_children(jlay).append(vc_file.image(jd, tag, key, w, h, 0, 0, 0.5))
        assert vc_file.jpeg_size(jpgs[list(jpgs)[1]]) == (37, 23)
        top_j, blobs_j = vc_file.read(vc_file.write(jd, os.path.join(td, "j.vectorcraft")))
        assert blobs_j == jpgs, "JPEG 字节往返变了"
        assert {k: v["mime"] for k, v in top_j["images"].items()} == {k: "image/jpeg" for k in jpgs}, top_j["images"]
        assert raises(lambda: vc_file.add_image(jd, b"\x89PNG\r\n\x1a\n", "image/jpeg"), "不是 JPEG")
        assert raises(lambda: vc_file.add_image(jd, jpg, "image/gif"), "只收 PNG / JPEG")
    ok += 1

    # 54b) 矩形对象（crop 框）：按 TL TR BR BL 在 0° 与逆时针 17° 各写一个 → 读回四角不变（≤ 1e-6 pt），
    #      live.w/h/xf 与锚点一致（xf 把 (0,0)(w,0)(w,h)(0,h) 映到四角，与 app 实测
    #      [cos, −sin, sin, cos, TL] 同式）；绿色 0.75 pt 描边、无填充、不是剪切路径；字段 ⊇ 模板的矩形路径；
    #      不是矩形 / 镜像顺序 → 报错。
    import math
    tpl = vc_file._template(vc_file.TEMPLATE)
    tpl_rect = next(c for c in tpl["clip"]["kind"]["children"] if c["kind"].get("clipping"))
    rd = vc_file.new_doc("rect")
    rlay = vc_file.add_layer(rd, "crop", printable=False)
    want = {}
    for deg in (0.0, 17.0):
        t = math.radians(deg)
        cx, cy, w, h = 120.0, 80.0, 200.0, 100.0
        ux, uy, vx, vy = math.cos(t), -math.sin(t), math.sin(t), math.cos(t)
        tl = (cx - ux * w / 2 - vx * h / 2, cy - uy * w / 2 - vy * h / 2)
        cs = [(tl[0] + ux * u + vx * v, tl[1] + uy * u + vy * v) for u, v in ((0, 0), (w, 0), (w, h), (0, h))]
        want["r%g" % deg] = (cs, deg, w, h)
        vc_file.lay_children(rlay).append(vc_file.rect(rd, "r%g" % deg, cs, (0, 0.75, 0.25), 0.75))
    top_r, _b = vc_file.loads(vc_file.dumps(rd))
    for n in vc_file.lay_children(vc_file.layer_by_name(top_r["document"], "crop")):
        cs, deg, w, h = want[n["name"]]
        got = vc_file.rect_corners(n)
        assert max(math.hypot(g[0] - c[0], g[1] - c[1]) for g, c in zip(got, cs)) <= 1e-6, (n["name"], got, cs)
        lv = n["kind"]["live"]
        assert lv["shape"] == "rectangle" and abs(lv["w"] - w) < 1e-9 and abs(lv["h"] - h) < 1e-9, lv
        a, b, c, d, e, f = lv["xf"]
        for (u, v), g in zip(((0, 0), (lv["w"], 0), (lv["w"], lv["h"]), (0, lv["h"])), got):
            assert math.hypot(a * u + c * v + e - g[0], b * u + d * v + f - g[1]) <= 1e-6, (n["name"], lv)
        assert abs(vc_file.angle_of_xf(lv["xf"]) - deg) < 1e-6, (deg, lv["xf"])
        assert n["kind"]["path"]["subpaths"][0]["closed"] is True and "clipping" not in n["kind"]
        app = {it["kind"]: it for it in n["appearance"]["items"]}
        assert app["fill"]["paint"] == {"type": "none"}, app
        assert app["stroke"]["paint"]["color"] == {"model": "rgb", "r": 0.0, "g": 0.75, "b": 0.25}
        assert app["stroke"]["width"] == 0.75, app
        miss = _key_paths(tpl_rect) - _key_paths(n) - {"kind.clipping"}
        assert not miss, ("rect 比模板少了字段", sorted(miss))
    sq = [(0, 0), (10, 0), (10, 5), (0, 5)]
    assert raises(lambda: vc_file.rect(rd, "x", [(0, 0), (10, 0), (11, 5), (0, 5)]), "不是矩形")
    assert raises(lambda: vc_file.rect(rd, "x", [(0, 0), (0, 5), (10, 5), (10, 0)]), "镜像")
    assert raises(lambda: vc_file.rect(rd, "x", sq[:3]), "4 个角")
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
        with c.undo_group("ep-craft sync"):
            c.execute("text.setText", {"id": 1, "text": "A"})
        assert cmds() == [("edit.undoGroup.begin", {"label": "ep-craft sync"}),
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

    # 60) Workbench 建模（V3）：record → 模型。相邻泳道首尾相接，分隔线在两中心的中点；最外两条在首/末道中心 ± 宽/2，
    #     marker 条带线按标签配 MW（rows:2 的并带出两条线），trim 进 trimmed，默认窗下沿让开 3%；
    #     条带数与标签行数对不上 → 拒绝；标签里没数字 → 拒绝。
    _preset, rec = _synth_record()
    names = ["M"] + [str(i) for i in range(1, 15)]
    mdl = workbench.from_record(rec, names, ["250 kD", "100 kD", "50 kD"], 1, trim=[14, 15],
                                meta={"skill": "ep-craft"})
    assert len(mdl["lanes"]) == 15 and mdl["marker_lane"] == 1
    assert [ln["trimmed"] for ln in mdl["lanes"]][-3:] == [False, True, True]
    xs_r = [src["x_cm"] for src in rec["lanes"]]
    for a_, b_ in zip(mdl["lanes"], mdl["lanes"][1:]):
        assert a_["right_cm"] == b_["left_cm"], (a_, b_)
    for i, ln in enumerate(mdl["lanes"][:-1]):
        assert abs(ln["right_cm"] - (xs_r[i] + xs_r[i + 1]) / 2) < 1e-3, (i, ln)
    assert abs(mdl["lanes"][0]["left_cm"] - (xs_r[0] - rec["lanes"][0]["width_cm"] / 2)) < 1e-3
    assert abs(mdl["lanes"][-1]["right_cm"] - (xs_r[-1] + rec["lanes"][-1]["width_cm"] / 2)) < 1e-3
    ys = [b["y_cm"] for b in rec["bands"]["1"]]
    assert [(b["mw"], b["y_cm"]) for b in mdl["bands"]] == list(zip([250.0, 100.0, 50.0], ys)), mdl["bands"]
    assert mdl["window"] == {"top_cm": workbench.TOP_GUARD_CM, "bottom_cm": round(6.6 * 0.97, 4)}, mdl["window"]
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

    # 62) 读回用户的改动（V3）：泳道 3 的两条分隔线右移 0.2 cm（且线有点斜）、删 band 100、在泳道 7 正中
    #     新画一条**没有名字**的线（劈成两道）、在 trimmed 里给泳道 2 放一条 trim 标记、积分窗上沿下移
    #     → 全部按几何读出来。
    mdl4 = workbench.from_record(rec, names, ["250 kD", "100 kD", "50 kD", "10 kD"], 1,
                                 marker_rows_cm=ys + [ys[-1] + 0.5], trim=[14, 15])
    d = vc_file.loads(vc_file.dumps(_synth_wb_doc(mdl4)[0]))[0]["document"]
    L = {l["name"]: l for l in d["layers"]}
    gx, s_pt = _wb_origin(d)
    cm = lambda pt: (pt - gx) / vc_file.PT_PER_CM
    lane_lines = vc_file.lay_children(L["lanes"])
    assert len(lane_lines) == 16                         # 15 道 = 16 条分隔线
    assert len(vc_file.lay_children(L["trimmed"])) == 2  # 泳道 14、15 各一条 trim 标记
    by_x = sorted(lane_lines, key=lambda n: vc_file.line_ends(n)[0][0])
    for n in by_x[2:4]:                                  # 泳道 3 的两条分隔线（也是泳道 2 的右线、泳道 4 的左线）
        for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
            a_["p"][0] += 0.2 * vc_file.PT_PER_CM
        n["kind"]["path"]["subpaths"][0]["anchors"][1]["p"][0] += 0.6     # 下端斜 0.6 pt：读中点
    mark = json.loads(json.dumps(vc_file.lay_children(L["trimmed"])[0]))   # 泳道 2 → trimmed
    mark["id"] = 91000
    dx = (mdl4["lanes"][1]["left_cm"] + mdl4["lanes"][1]["right_cm"]) / 2 * vc_file.PT_PER_CM + gx \
        - sum(p_[0] for p_ in vc_file.line_ends(mark)) / 2
    for a_ in mark["kind"]["path"]["subpaths"][0]["anchors"]:
        a_["p"][0] += dx
    vc_file.lay_children(L["trimmed"]).append(mark)
    bands_l = vc_file.lay_children(L["marker bands"])
    bands_l[:] = [n for n in bands_l if n["name"] != "band 100"]
    x_new = (mdl4["lanes"][6]["left_cm"] + mdl4["lanes"][6]["right_cm"]) / 2
    n = json.loads(json.dumps(by_x[0]))
    n.pop("name")
    n["id"] = 90000
    for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
        a_["p"][0] = gx + x_new * vc_file.PT_PER_CM
    lane_lines.append(n)
    wl = sorted(vc_file.lay_children(L["window"]), key=lambda n: vc_file.line_ends(n)[0][1])
    for a_ in wl[0]["kind"]["path"]["subpaths"][0]["anchors"]:
        a_["p"][1] += 0.5 * vc_file.PT_PER_CM
    got, notes = workbench.from_document(d)
    assert len(got["lanes"]) == 16, len(got["lanes"])
    assert [i for i, ln in enumerate(got["lanes"], 1) if ln["trimmed"]] == [2, 15, 16], got["lanes"]
    c3 = (got["lanes"][2]["left_cm"] + got["lanes"][2]["right_cm"]) / 2
    c3_0 = (mdl4["lanes"][2]["left_cm"] + mdl4["lanes"][2]["right_cm"]) / 2
    want = c3_0 + 0.2 + 0.3 / vc_file.PT_PER_CM          # 两条线下端各斜 0.6 pt → 中点各多 0.3 pt
    assert abs(c3 - want) < 2e-3, (c3, want)
    assert got["lanes"][1]["right_cm"] == got["lanes"][2]["left_cm"]           # 共用的那条线
    assert abs(got["lanes"][6]["right_cm"] - x_new) < 1e-3 and abs(got["lanes"][7]["left_cm"] - x_new) < 1e-3
    assert [b["mw"] for b in got["bands"]] == [250.0, 50.0, 10.0]
    assert abs(got["window"]["top_cm"] - (workbench.TOP_GUARD_CM + 0.5)) < 1e-3, got["window"]
    ok += 1

    # 63) 校验（V3）：所有问题一次报完、什么都不读出来 —— 删掉最右的分隔线（泳道 15 的 trim 标记落空）、
    #     window 三条线、band 名字不合规；trimmed 里放竖线（旧习惯：把线挪进去）；
    #     另一份：条带线横跨两条泳道、MW 不单调、条带不足 3 条。
    bad = json.loads(json.dumps(top_wb["document"]))
    B = {l["name"]: l for l in bad["layers"]}
    lanes_b = vc_file.lay_children(B["lanes"])
    lanes_b.remove(max(lanes_b, key=lambda n: vc_file.line_ends(n)[0][0]))  # 最右那条
    vc_file.lay_children(B["window"]).append(json.loads(json.dumps(vc_file.lay_children(B["window"])[0])))
    vc_file.lay_children(B["marker bands"])[0]["name"] = "250kD band"
    try:
        workbench.from_document(bad)
        raise AssertionError("坏 Workbench 没报错")
    except workbench.WorkbenchError as e:
        msg = str(e)
        assert "有 3 处要改" in msg, msg                                     # 一次报完
        for frag in ("不在任何泳道里", "window 图层应当恰好有两条横线", "band <分子量>"):
            assert frag in msg, (frag, msg)
    bad3 = json.loads(json.dumps(top_wb["document"]))
    B3 = {l["name"]: l for l in bad3["layers"]}
    moved = sorted(vc_file.lay_children(B3["lanes"]), key=lambda n: vc_file.line_ends(n)[0][0])[6:8]
    for n in moved:                                                         # 旧习惯：把泳道的线挪进 trimmed
        vc_file.lay_children(B3["lanes"]).remove(n)
        vc_file.lay_children(B3["trimmed"]).append(n)
    try:
        workbench.from_document(bad3)
        raise AssertionError("trimmed 里的竖线没报错")
    except workbench.WorkbenchError as e:
        assert "trimmed 图层里有竖线" in str(e), e
    bad2 = json.loads(json.dumps(top_wb["document"]))
    B2 = {l["name"]: l for l in bad2["layers"]}
    bl = sorted(vc_file.lay_children(B2["marker bands"]), key=lambda n: vc_file.line_ends(n)[0][1])
    a0 = bl[0]["kind"]["path"]["subpaths"][0]["anchors"]
    for shift, frag in ((4 * 0.565, "横跨了泳道"), (-2.0, "不在任何泳道里")):  # 落到泳道 5 上 / 落到最左分隔线外
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

    # 64) 泳道名按**序号**跟着泳道走（V11）：第 i 格的标签属于第 i 格，不做位置/容差配对 ——
    #     拖线、调宽窄、把某条线拖到胶边都不改名字；中间插一条 → 后面的名字整体后移一格，
    #     多出来的最后一格是 None；删一条 → 后面的名字前移一格，尾巴的名字消失。
    prev = [0.5, 1.0, 1.5, 2.0, 2.5]
    nm = ["M", "A", "B", "C", "D"]
    assert workbench.match_lanes(prev, [0.5, 1.0, 1.25, 1.5, 2.0, 2.5]) == [0, 1, 2, 3, 4, None]
    assert workbench.match_lanes(prev, [0.5, 1.0, 2.0, 2.5]) == [0, 1, 2, 3]
    assert workbench.match_lanes(prev, [0.6, 1.1, 1.4, 2.0, 2.5]) == [0, 1, 2, 3, 4]
    assert workbench.match_lanes(prev, [0.5, 1.0, 1.5, 2.0, 3.4]) == [0, 1, 2, 3, 4]
    fake = {"lanes": [{"left_cm": c - 0.1, "right_cm": c + 0.1} for c in (0.5, 1.0, 1.25, 1.5, 2.0, 2.5)]}
    assert workbench.lane_names_after(fake, prev, nm) == ["M", "A", "B", "C", "D", None]
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
        return profiles.call_bands(ys_, gs, sm, ppc_)[0]
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
        assert rep["calibration"]["method"] == "interp" and rep["calibration"]["marker_lane"] == 1
        assert "r2" not in rep["calibration"] and "fit" not in rep["calibration"], rep["calibration"]
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
        assert rep_b["lanes"][1]["bands"][0]["mw_kda"] != sb[0]["mw_kda"]      # 拖线后插值跟着变
        out = os.path.join(td, "fig.mw.json")
        write_v2(rep, out)
        assert json.load(open(out, encoding="utf-8")) == json.loads(json.dumps(rep))
        table = format_report_v2(rep)
        assert "interp" in table and "R²" not in table and "S1" in table and \
            "%.1f kDa" % sb[0]["mw_kda"] in table, table
        assert "外推" not in table, table          # 插值不外推：范围外的带写「超出 marker 范围」，没有脚注
    ok += 1

    # 69) 泳道种子 = 等距梳子、按加样孔定相（V8）：合成胶面顶上画 15 个 U 形孔（间距 0.52 cm、
    #     首孔 0.70 cm、两侧各留无孔边距）→ 间距与每道中心误差 < 1 px；预设间距错 9%（0.565，
    #     DGC 预设的老值）也照样找到 0.52；胶面上没有孔 → 退回「预设间距、整排居中」并说明。
    with tempfile.TemporaryDirectory() as td:
        wells = os.path.join(td, "wells.png")
        _synth_wells(wells, first_cm=0.70, pitch_cm=0.52)
        w9, h9, g9 = profiles.gel_gray(open(wells, "rb").read())
        for prior in (0.52, 0.565):
            sd = well_grid.seed(g9, w9, h9, 100.0, 15, prior)
            assert sd["source"] == "wells" and sd["depth"] >= well_grid.MIN_WELL_DEPTH, sd
            assert abs(sd["pitch_cm"] - 0.52) <= 0.004, (prior, sd["pitch_cm"])
            cs = [(a + b) / 2 for a, b in sd["lanes"]]
            assert max(abs(c - (0.70 + 0.52 * k)) for k, c in enumerate(cs)) <= 0.01, (prior, cs)
            assert all(abs((b - a) - well_grid.WIDTH_FRAC * sd["pitch_cm"]) < 1e-3 for a, b in sd["lanes"])
        blank = os.path.join(td, "blank.png")
        _run([MAGICK, "-size", "860x625", "xc:gray(200)", "-seed", "7", "-attenuate", "0.25",
              "+noise", "Gaussian", "-depth", "8", blank])
        wb_, hb_, gb_ = profiles.gel_gray(open(blank, "rb").read())
        sd = well_grid.seed(gb_, wb_, hb_, 100.0, 15, 0.52)
        assert sd["source"] == "even" and "看不清加样孔" in sd["note"], sd
        cs = [(a + b) / 2 for a, b in sd["lanes"]]
        assert abs((cs[0] + cs[-1]) / 2 - 4.3) < 1e-3 and abs(sd["pitch_cm"] - 0.52) < 1e-9, cs
    ok += 1

    # 70) from_image（V8）：只用胶面图建 Workbench —— 泳道来自孔，marker 线来自 marker 泳道剖面上
    #     叫出的峰（不再用老 stage 3 的行；那些行在 DGC 照片上一律偏下 0.13–0.5 mm）；
    #     叫出的带数与标签行数不符 → 拒绝，并把叫出来的 y 列给用户看。
    with tempfile.TemporaryDirectory() as td:
        gel9 = os.path.join(td, "gel.png")
        rows_cm = [1.0, 1.6, 2.3, 3.1]
        _synth_wells(gel9, first_cm=0.70, pitch_cm=0.52, ladder_rows_cm=rows_cm)
        png9 = open(gel9, "rb").read()
        names9 = ["M"] + [""] * 14
        m9, sd9 = workbench.from_image(png9, 8.6, 6.25, 15, 0.565, names9, ["100", "50", "25", "10"], 1,
                                       meta={"skill": "ep-craft"})
        assert sd9["source"] == "wells" and m9["meta"]["seed"]["source"] == "wells", sd9
        assert [b["mw"] for b in m9["bands"]] == [100.0, 50.0, 25.0, 10.0]
        assert all(abs(b["y_cm"] - y) < 0.02 for b, y in zip(m9["bands"], rows_cm)), m9["bands"]
        assert m9["marker_lane"] == 1 and len(m9["lanes"]) == 15
        try:
            workbench.from_image(png9, 8.6, 6.25, 15, 0.52, names9, ["100", "50", "25", "10", "5"], 1)
            raise AssertionError("带数不符没拒绝")
        except workbench.WorkbenchError as e:
            assert "叫出 4 条带" in str(e) and "3.1" in str(e), e              # 把叫出来的 y 列给用户
    ok += 1

    # 71) 一维排字 spread（V5）：保持顺序、相邻中心距 ≥ 半尺寸之和 + gap、孤立的不动、
    #     挤在一起的一簇以目标均值为中心对称展开（总位移最小）。
    got = figure.spread([0.0, 1.0, 2.0, 10.0], [3.0] * 4)
    assert got == [-3.0, 1.0, 5.0, 10.0], got
    got = figure.spread([5.0, 5.0], [2.0, 4.0], gap=0.0)
    assert abs((got[1] - got[0]) - 3.0) < 1e-9 and abs(sum(got) / 2 - 5.0) < 1e-9, got
    rnd = _lcg(3)
    ts = sorted(10 * next(rnd) for _ in range(30))
    got = figure.spread(ts, [1.0] * 30, gap=0.5)
    assert all(b - a >= 1.5 - 1e-9 for a, b in zip(got, got[1:])), got
    ok += 1

    # 72) render 出的文档（V5）：画板顺序 Figure/Workbench/Profiles，Figure 在 Workbench 下面不重叠；
    #     每条有名字的泳道一条标签、居中在泳道上；marker 标签不互相压、各贴在自己的带旁；
    #     页边 ≥ 最小留白；plan_sync 在刚生成的文档上 → 0 个操作（幂等）；两种 Profiles 版式都能出。
    with tempfile.TemporaryDirectory() as td:
        gp = os.path.join(td, "gel.png")
        rows5 = [1.0, 1.2, 1.4, 2.3, 3.1]
        _synth_wells(gp, first_cm=0.70, pitch_cm=0.52, ladder_rows_cm=rows5)
        png5 = open(gp, "rb").read()
        names5 = ["M", "A", "B"] + [""] * 9 + ["X", "Y", "Z"]
        mk5 = ["100 kD", "70 kD", "50 kD", "35 kD", "10 kD"]
        m5, _sd = workbench.from_image(png5, 8.6, 6.25, 15, 0.52, names5, mk5, 1, meta={"skill": "ep-craft"})
        rep5 = profiles.analyze(m5, png5, names5)
        for lay in ("strips", "stack"):
            doc5 = figure.build_document(m5, rep5, png5, "t", layout=lay)
            top5, _b = vc_file.loads(vc_file.dumps(doc5))
            d5 = top5["document"]
            assert [a["name"] for a in d5["artboards"]] == ["Figure", "Workbench", "Profiles"]
            fig_r, wb_r = d5["artboards"][0]["rect"], d5["artboards"][1]["rect"]
            assert fig_r["y0"] >= wb_r["y1"] + figure.BOARD_GAP - 1e-6, (fig_r, wb_r)
            ops5, _meta = figure.plan_sync(d5, m5, rep5, layout=lay)
            assert ops5 == [], ops5[:5]
        labs = {n["name"]: vc_file.text_info(n) for n in vc_file.lay_children(vc_file.layer_by_name(d5, "labels"))}
        lane_labs = sorted(k for k in labs if k.startswith("label lane "))
        assert lane_labs == sorted("label lane %d" % i for i in (1, 2, 3, 13, 14, 15)), lane_labs
        seg = vc_file.lay_children(vc_file.layer_by_name(d5, "gel"))
        seg = [n for n in seg if n["name"] == "gel seg 1"][0]
        gx = seg["kind"]["children"][0]["kind"]["path"]["subpaths"][0]["anchors"][0]["p"][0]
        for i in (1, 2, 13):
            ln = m5["lanes"][i - 1]
            want_x = gx + (ln["left_cm"] + ln["right_cm"]) / 2 * vc_file.PT_PER_CM
            assert abs(labs["label lane %d" % i]["x"] - want_x) < 0.01, (i, labs["label lane %d" % i]["x"], want_x)
        mys = sorted(v["y"] for k, v in labs.items() if k.startswith("label band "))
        assert len(mys) == 5 and all(b - a >= figure.CAP * figure.MARKER_SIZE + 1.0 - 1e-6 for a, b in zip(mys, mys[1:])), mys
        assert labs["label band 10"]["text"] == "10 kD" and labs["label band 10"]["justify"] == "Right"
        assert gx - fig_r["x0"] >= figure.MARGIN_MIN_CM * vc_file.PT_PER_CM - 1e-6
    ok += 1

    # 72a) render 的 Crop 画板（crop-artboard spec §3/§5）：2401×1601 的合成照片（预览 2000×1334，横竖比例
    #      差一点点 —— 框照样是真矩形）、框转 3° → 画板 3 叫 Crop、右边贴 −BOARD_GAP；photo 层锁定不打印、
    #      crop 层不锁不打印；照片 image/jpeg、等比到 0.5 % 以内；框四角 = crop_layout 的 frame_pt；
    #      read_frame 读回 frame_px（≤ 0.01 px）无错；render 完第一次 sync = 0 个操作（ep-meta 带着 crop）；
    #      crop=None 仍是三画板、没有 photo / crop 层、没有 JPEG。
    import crop as crop_geom
    with tempfile.TemporaryDirectory() as td:
        ph = os.path.join(td, "photo.jpg")
        _run([MAGICK, "-size", "2401x1601", "xc:gray(210)", "-fill", "gray(90)",
              "-draw", "rectangle 400,300 1900,1300", "-quality", "90", ph])
        size0 = (2401, 1601)
        W5, H5 = m5["gel"]["width_cm"], m5["gel"]["height_cm"]
        gw = 1500.0
        box = (500.0, 300.0, 500.0 + gw, 300.0 + gw * H5 / W5)
        frame_c = crop_geom.frame_from_stage1(box, 3.0, size0, load_dims_after(ph, 3.0)[1])
        jpeg, psize, _k = crop_geom.preview(ph)
        assert psize == (2000, 1334), psize
        m5c = copy.deepcopy(m5)
        m5c["meta"]["crop"] = {"photo_size": list(size0), "photo_sha1": crop_geom.photo_sha1(ph),
                               "frame_px": frame_c, "stage1": {"cue": "dark"}}
        spec = {"jpeg": jpeg, "preview_size": psize, "photo_size": size0, "frame_px": frame_c}
        top_c, blobs_c = vc_file.loads(vc_file.dumps(figure.build_document(m5c, rep5, png5, "t", crop=spec)))
    d_c = top_c["document"]
    assert [a["name"] for a in d_c["artboards"]] == ["Figure", "Workbench", "Profiles", "Crop"]
    r_c = d_c["artboards"][3]["rect"]
    assert abs(r_c["x1"] + figure.BOARD_GAP) < 1e-6 and r_c["y0"] == 0.0, r_c
    assert vc_file.layer_flags(vc_file.layer_by_name(d_c, "photo")) == (True, True, False)
    assert vc_file.layer_flags(vc_file.layer_by_name(d_c, "crop")) == (True, False, False)
    ph_n = vc_file.lay_children(vc_file.layer_by_name(d_c, "photo"))
    assert len(ph_n) == 1 and ph_n[0]["name"] == "photo" and ph_n[0]["kind"]["type"] == "image"
    assert top_c["images"][ph_n[0]["kind"]["key"]]["mime"] == "image/jpeg"
    assert blobs_c[ph_n[0]["kind"]["key"]] == jpeg
    xf_c = ph_n[0]["kind"]["xf"]
    assert xf_c[1] == xf_c[2] == 0.0 and abs(xf_c[0] - xf_c[3]) / xf_c[0] < 0.005, xf_c
    fr_n = vc_file.lay_children(vc_file.layer_by_name(d_c, "crop"))
    assert len(fr_n) == 1 and fr_n[0]["name"] == "crop" and fr_n[0]["kind"]["live"]["shape"] == "rectangle"
    lo_c = figure.crop_layout(frame_c, size0, psize, W5)
    assert all(abs(a - b) < 1e-6 for p, q in zip(vc_file.rect_corners(fr_n[0]), lo_c["frame_pt"]) for a, b in zip(p, q))
    st_c = next(it for it in fr_n[0]["appearance"]["items"] if it["kind"] == "stroke")
    assert st_c["width"] == figure.CROP_W and st_c["paint"]["color"]["g"] == 0.75
    assert abs(math.hypot(lo_c["frame_pt"][1][0] - lo_c["frame_pt"][0][0],
                          lo_c["frame_pt"][1][1] - lo_c["frame_pt"][0][1]) - W5 * vc_file.PT_PER_CM) < 1e-6
    back_c, _n = workbench.from_document(d_c)
    assert back_c["meta"]["crop"]["frame_px"] == frame_c
    got_c, errs_c = crop_geom.read_frame(d_c, back_c["meta"], H5 / W5)
    assert not errs_c and max(abs(a - b) for p, q in zip(got_c, frame_c) for a, b in zip(p, q)) <= 0.01, (errs_c, got_c)
    assert crop_geom.frame_delta(frame_c, got_c, W5) is None
    ops_c, _meta = figure.plan_sync(d_c, back_c, profiles.analyze(back_c, png5, [None] * len(back_c["lanes"])))
    assert ops_c == [], ops_c[:4]
    assert figure.plan_sync(d_c, m5c, rep5)[0] == []
    d_n = vc_file.loads(vc_file.dumps(figure.build_document(copy.deepcopy(m5), rep5, png5, "t")))[0]
    assert [a["name"] for a in d_n["document"]["artboards"]] == ["Figure", "Workbench", "Profiles"]
    assert [l["name"] for l in d_n["document"]["layers"]] == [n for n, *_r in workbench.LAYERS if n not in ("photo", "crop")]
    assert {v["mime"] for v in d_n["images"].values()} == {"image/png"}
    assert crop_geom.read_frame(d_n["document"], {}, H5 / W5) == (None, [])
    ok += 1

    # 73) sync 往返（V5，spec §11 的脚本版，不需要 app）：在 render 的文档上模拟用户 ——
    #     泳道 3 的两条分隔线右移 0.2 cm、删 band 50 线、在泳道 4 里偏右加一条没名字的线（劈出新泳道 5）、
    #     在 trimmed 里给泳道 14 放一条 trim 标记（青色，sync 后变灰）、
    #     把泳道 2 的标签改成 "WT" 并改成红色 → 读回 → plan_sync → apply_ops_json：
    #     "WT" 文字与颜色原样、跟着泳道走（改名为新泳道号）并重新居中；新泳道得到 "?" 标签；
    #     trimmed 的泳道标签删掉、Figure 拆成两段（中间有拼缝）、trimmed 的线变灰；50 kD 标签随线删除；
    #     任何操作都不写标签文字/样式；再跑一次 plan_sync → 0 个操作。
    d6 = json.loads(json.dumps(d5))
    top6 = {"format": "vectorcraft", "version": 4, "document": d6, "images": top5["images"]}
    L6 = {l["name"]: l for l in d6["layers"]}
    gx_wb, _s = _wb_origin(d6)
    PTc = vc_file.PT_PER_CM
    lines6 = sorted(vc_file.lay_children(L6["lanes"]), key=lambda n: vc_file.line_ends(n)[0][0])
    assert len(lines6) == 16
    for n in lines6[2:4]:
        for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
            a_["p"][0] += 0.2 * PTc
    c14 = (m5["lanes"][13]["left_cm"] + m5["lanes"][13]["right_cm"]) / 2   # 原泳道 14（"Y"）→ trimmed：中间一段被拿掉
    mark = json.loads(json.dumps(lines6[0]))
    mark["id"], mark["name"] = 70100, "trim"
    for a_, dx in zip(mark["kind"]["path"]["subpaths"][0]["anchors"], (-0.1, 0.1)):
        a_["p"] = [gx_wb + (c14 + dx) * PTc, 20.0]
    vc_file.lay_children(L6["trimmed"]).append(mark)
    x_split = (m5["lanes"][3]["left_cm"] + m5["lanes"][3]["right_cm"]) / 2 + 0.1
    n = json.loads(json.dumps(lines6[0]))
    n.pop("name")
    n["id"] = 70000
    for a_ in n["kind"]["path"]["subpaths"][0]["anchors"]:
        a_["p"][0] = gx_wb + x_split * PTc
    vc_file.lay_children(L6["lanes"]).append(n)
    bl6 = vc_file.lay_children(L6["marker bands"])
    bl6[:] = [n for n in bl6 if n["name"] != "band 50"]
    wt = [n for n in vc_file.lay_children(L6["labels"]) if n["name"] == "label lane 2"][0]
    wt["kind"]["runs"][0]["text"] = "WT"
    wt["kind"]["runs"][0]["style"]["fill"] = {"type": "solid", "color": {"model": "rgb", "r": 1.0, "g": 0.0, "b": 0.0}}
    wt_style = json.loads(json.dumps(wt["kind"]["runs"][0]["style"]))
    m6, _notes = workbench.from_document(d6)
    assert len(m6["lanes"]) == 16 and m6["lanes"][14]["trimmed"] and m6["marker_lane"] == 1
    rep6 = profiles.analyze(m6, png5, [None] * 16)
    ops6, meta6 = figure.plan_sync(d6, m6, rep6, layout="stack")
    label_ids = {n["id"] for n in vc_file.lay_children(L6["labels"])}
    for op in ops6:
        if op.get("id") in label_ids:
            assert op["op"] in ("move", "rename", "delete"), op           # 标签只动位置/名字/删
    assert {"op": "rename", "id": wt["id"], "name": "label lane 2"} not in ops6   # 泳道 2 还是 2（新道在 4/5 之间）
    figure.apply_ops_json(top6, ops6)
    L7 = {l["name"]: l for l in d6["layers"]}
    labs7 = {n["name"]: n for n in vc_file.lay_children(L7["labels"])}
    assert labs7["label lane 2"]["kind"]["runs"][0]["text"] == "WT"
    assert labs7["label lane 2"]["kind"]["runs"][0]["style"] == wt_style, "用户改的样式被动了"
    assert vc_file.text_info(labs7["label lane 16"])["text"] == "?", "多出来的最后一格没得到 ? 标签"
    assert vc_file.text_info(labs7["label lane 13"])["text"] == "X"   # 名字按序号留在第 13 格
    assert vc_file.text_info(labs7["label lane 14"])["text"] == "Y"
    assert "label lane 15" not in labs7, "trimmed 的泳道标签没删"
    assert "label band 50" not in labs7 and "label band 35" in labs7
    assert meta6["lane_names"][1] == "WT" and meta6["lane_names"][15] is None
    segs7 = sorted(n["name"] for n in vc_file.lay_children(L7["gel"]) if n["name"].startswith("gel seg"))
    assert segs7 == ["gel seg 1", "gel seg 2"], segs7
    for n in vc_file.lay_children(L7["trimmed"]):
        st = [it for it in n["appearance"]["items"] if it["kind"] == "stroke"][0]["paint"]["color"]
        assert abs(st["r"] - figure.GREY[0]) < 1e-6, st
    m7, _n = workbench.from_document(d6)
    rep7 = profiles.analyze(m7, png5, [None] * 16)
    ops7, _meta = figure.plan_sync(d6, m7, rep7, layout="stack")
    assert ops7 == [], ops7[:6]
    c2 = vc_file.text_info(labs7["label lane 3"])["x"]
    seg1 = [n for n in vc_file.lay_children(L7["gel"]) if n["name"] == "gel seg 1"][0]
    fx = seg1["kind"]["children"][0]["kind"]["path"]["subpaths"][0]["anchors"][0]["p"][0]
    assert abs(c2 - (fx + workbench.centres(m7)[2] * PTc)) < 0.01, "改过的泳道 3 标签没重新居中"
    ok += 1

    # 74) 去掉的输出与参数（V6）：--keep-lanes / --svg / --no-editor / --debug / --record 与 export 子命令
    #     都报「已经去掉了：用什么代替」并非 0 退出（不静默忽略）；孔距核对只在偏差 > 10% 时警告。
    from gel_photo import main as gp_main, _check_pitch
    for extra in (["--keep-lanes", "1-3"], ["--svg"], ["--no-editor"], ["--debug", "d.png"], ["--record", "r.json"]):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = gp_main(["render", "/nonexistent/x.jpg", "--out", "/tmp/x.vectorcraft",
                          "--gel-size", "/nonexistent/g.json"] + extra)
        assert rc == 2 and "已经去掉了" in buf.getvalue() and extra[0] in buf.getvalue(), (extra, buf.getvalue())
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = gp_main(["export", "fig.scene.json", "--png", "x.png"])
    assert rc == 2 and "sync" in buf.getvalue(), buf.getvalue()
    for pitch, warn in ((0.515, False), (0.40, True)):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _check_pitch({"source": "wells", "pitch_cm": pitch}, {"lane_pitch_cm": 0.52})
        assert ("[!] scale" in buf.getvalue()) == warn, (pitch, buf.getvalue())
    ok += 1

    # 76) render → 第一次 sync = 0 个操作（V7 实测出的回归）：胶面图是整像素（8.6×6.25 cm 在 300 dpi 下是
    #     1016×738 px，高其实是 6.2469 cm）。sync 的模型是从文档**读回**的（高按图量），所以 render 的模型
    #     也必须按图的宽高比定高，否则刚 render 完的文档第一次 sync 就会去改 Figure 画板和剪切框。
    with tempfile.TemporaryDirectory() as td:
        gp = os.path.join(td, "gel.png")
        _run([MAGICK, "-size", "1016x738", "xc:gray(200)", "-seed", "5", "-attenuate", "0.25", "+noise", "Gaussian",
              "-fill", "gray(80)"] + sum((["-draw", "rectangle 80,%d 115,%d" % (y, y + 5)] for y in (150, 230, 330, 460)), [])
             + ["-fill", "none", "-stroke", "gray(70)", "-strokewidth", "2"]
             + sum((["-draw", "polyline %.1f,0 %.1f,8 %.1f,8 %.1f,0" % (c - 21, c - 21, c + 21, c + 21)]
                    for c in [97.6 + 61.4 * k for k in range(15)]), [])
             + ["-depth", "8", gp])
        png8 = open(gp, "rb").read()
        m8, _sd = workbench.from_image(png8, 8.6, 6.25, 15, 0.52, ["M"] + [""] * 14, ["100", "50", "25", "10"], 1,
                                       marker_rows_cm=[1.29, 1.97, 2.82, 3.92],   # 本测只验高的口径，不验叫带
                                       meta={"skill": "ep-craft"})
        assert abs(m8["gel"]["height_cm"] - 738 * 8.6 / 1016) < 1e-9, m8["gel"]
        rep8 = profiles.analyze(m8, png8, ["M"] + [""] * 14)
        d8 = vc_file.loads(vc_file.dumps(figure.build_document(m8, rep8, png8, "t")))[0]["document"]
        back8, _n = workbench.from_document(d8)                       # sync 的口径：模型从文档读回
        ops8, _meta = figure.plan_sync(d8, back8, profiles.analyze(back8, png8, [None] * 15))
        assert ops8 == [], ops8[:4]
    ok += 1

    # 77) 改名 / 新画的 band 线也有 Figure 标签（spec §2.6「改名就是改值」）：band 35 改成 band 36 →
    #     35 的标签删掉、新建 "36 kD"；新画一条 band 8 → 新建 "8 kD"；标定点随之变化；再 sync 一次 0 操作。
    d9 = json.loads(json.dumps(d5))
    top9 = {"format": "vectorcraft", "version": 4, "document": d9, "images": top5["images"]}
    L9 = {l["name"]: l for l in d9["layers"]}
    bl9 = vc_file.lay_children(L9["marker bands"])
    [n for n in bl9 if n["name"] == "band 35"][0]["name"] = "band 36"
    n8 = json.loads(json.dumps(bl9[-1]))
    n8["name"], n8["id"] = "band 8", 60001
    for a_ in n8["kind"]["path"]["subpaths"][0]["anchors"]:
        a_["p"][1] += 0.5 * vc_file.PT_PER_CM
    bl9.append(n8)
    m9b, _n = workbench.from_document(d9)
    assert [b["mw"] for b in m9b["bands"]][-3:] == [36.0, 10.0, 8.0], m9b["bands"]
    rep9 = profiles.analyze(m9b, png5, [None] * 15)
    ops9, _meta = figure.plan_sync(d9, m9b, rep9)
    figure.apply_ops_json(top9, ops9)
    labs9 = {n["name"]: vc_file.text_info(n)["text"] for n in vc_file.lay_children(L9["labels"])}
    assert "label band 35" not in labs9 and labs9.get("label band 36") == "36 kD" and labs9.get("label band 8") == "8 kD", labs9
    m9c, _n = workbench.from_document(d9)
    assert figure.plan_sync(d9, m9c, profiles.analyze(m9c, png5, [None] * 15))[0] == []
    ok += 1

    # 75+) 活文档执行器（V7，sync_exec；假控制口，不开 app）：每种 op 的命令序列、锁定层先解锁后锁回、
    #      新对象起名、剪切组、失败 → 撤销组 cancel 并抛原错、收尾恢复当前层与默认颜色。
    import selftest_sync
    ok += selftest_sync.run()
    import selftest_crop
    ok += selftest_crop.run()

    # 78) 标签角度（V9）：角度是逆时针度（90 = 从下往上读），写进点文字的 xf（绕锚点）；读回同值；
    #     旋转后按**墨迹框**摆位与量页边（竖排的泳道名框底离胶顶 4 pt、框中线=泳道中心，页顶留白
    #     装得下整条字）；marker 标签也能竖排（框内边离胶 4 pt）；render 完 plan_sync = 0（幂等）；
    #     sync 读到用户转过的标签 → 只挪位置、不改角度，并把角度记进 ep-meta。
    import types
    import text_layout
    with tempfile.TemporaryDirectory() as td:
        # (a) 框几何 + xf 往返
        b0 = text_layout.box_offsets(20.0, 8.0, 0.0, "Center")
        assert all(abs(a - b) < 1e-9 for a, b in zip(b0, (-10.0, -text_layout.CAP * 8, 10.0, 0.0))), b0
        b90 = text_layout.box_offsets(20.0, 8.0, 90.0, "Center")
        assert all(abs(a - b) < 1e-9 for a, b in
                   zip(b90, (-text_layout.CAP * 8, -10.0, 0.0, 10.0))), b90        # 字往上走、框在锚点左边
        bl = text_layout.box_offsets(20.0, 8.0, 90.0, "Left")
        assert abs(bl[1] + 20.0) < 1e-9 and abs(bl[3]) < 1e-9, bl                  # 左对齐从锚点往上
        assert abs(text_layout.anchor_for(b90, centre_x=100.0, bottom=50.0)[0] - (100.0 + text_layout.CAP * 4)) < 1e-9
        assert text_layout.window_deg(450.0) == 90.0 and text_layout.window_deg(-270.0) == 90.0
        for bad in ("x", float("nan")):
            try:
                text_layout.window_deg(bad)
                raise AssertionError("非法角度没报错：%r" % (bad,))
            except RuntimeError as e:
                assert "角度" in str(e), e
        doc_rot = vc_file.new_doc("t")
        vc_file.add_layer(doc_rot, "labels")
        n_rot = vc_file.text(doc_rot, "t", 40, 60, "ABCD", size=20, justify="Left", angle=90)
        assert [round(v, 6) for v in n_rot["kind"]["xf"]] == [0.0, -1.0, 1.0, 0.0, 40.0, 60.0], n_rot["kind"]["xf"]
        assert vc_file.text_info(n_rot)["angle"] == 90.0
        n_rot["kind"]["xf"] = vc_file.rot_xf(40, 60, -30)
        assert abs(vc_file.text_info(n_rot)["angle"] + 30.0) < 1e-9

        # (b) render：泳道名与 marker 标签都竖排
        gp_c = os.path.join(td, "gel.png")
        rows_c = [1.0, 1.2, 1.4, 2.3, 3.1]
        _synth_wells(gp_c, first_cm=0.70, pitch_cm=0.52, ladder_rows_cm=rows_c)
        png_c = open(gp_c, "rb").read()
        names_c = ["M", "A", "B"] + [""] * 9 + ["X", "Y", "Z"]
        mk_c = ["100 kD", "70 kD", "50 kD", "35 kD", "10 kD"]
        m_c, _sd = workbench.from_image(png_c, 8.6, 6.25, 15, 0.52, names_c, mk_c, 1,
                                        meta={"skill": "ep-craft", "label_angle": {"lane": 90.0, "marker": 90.0}})
        rep_c = profiles.analyze(m_c, png_c, names_c)
        d_c = vc_file.loads(vc_file.dumps(figure.build_document(m_c, rep_c, png_c, "t")))[0]["document"]
        lab_c = {n["name"]: vc_file.text_info(n) for n in vc_file.lay_children(vc_file.layer_by_name(d_c, "labels"))}
        assert lab_c["label lane 2"]["angle"] == 90.0 and lab_c["label band 100"]["angle"] == 90.0, lab_c
        seg_c = [n for n in vc_file.lay_children(vc_file.layer_by_name(d_c, "gel")) if n["name"] == "gel seg 1"][0]
        gx_c, gy_c = (seg_c["kind"]["children"][0]["kind"]["path"]["subpaths"][0]["anchors"][0]["p"])
        w_c = figure.measure("A", figure.LANE_SIZE)[0]
        bc = text_layout.box_offsets(w_c, figure.LANE_SIZE, 90.0, "Center")
        ln_c = m_c["lanes"][1]
        cx_c = (ln_c["left_cm"] + ln_c["right_cm"]) / 2.0 * vc_file.PT_PER_CM
        assert abs(lab_c["label lane 2"]["y"] + bc[3] - (gy_c - figure.LANE_GAP_PT)) < 0.01, "竖排泳道名的框底不在胶顶上方 4 pt"
        assert abs(lab_c["label lane 2"]["x"] + (bc[0] + bc[2]) / 2.0 - (gx_c + cx_c)) < 0.01, "竖排泳道名没在泳道中心"
        fig_c = d_c["artboards"][0]["rect"]
        assert gy_c - fig_c["y0"] >= w_c + figure.LANE_GAP_PT + figure.PAD_CM * vc_file.PT_PER_CM - 1e-6, "页顶没装下竖排的字"
        wm_c = figure.measure("100 kD", figure.MARKER_SIZE)[0]
        bm_c = text_layout.box_offsets(wm_c, figure.MARKER_SIZE, 90.0, "Right")
        assert abs(lab_c["label band 100"]["x"] + bm_c[2] - (gx_c - figure.MARKER_GAP_PT)) < 0.01, "竖排 marker 标签的内边不在胶左 4 pt"
        assert fig_c["x0"] <= gx_c - figure.MARKER_GAP_PT + bm_c[0] + 1e-6, "竖排 marker 标签越出页面左边"
        assert fig_c["y0"] <= lab_c["label band 100"]["y"] + bm_c[1] + 1e-6, "竖排 marker 标签越出页面上边"
        assert figure.plan_sync(d_c, m_c, rep_c)[0] == [], "竖排 render 完第一次 sync 却有改动"

        # (c) sync：用户在 app 里把 label lane 2 转 90° → 只挪位置（不改角度），角度记进 ep-meta
        m0, _sd = workbench.from_image(png_c, 8.6, 6.25, 15, 0.52, names_c, mk_c, 1, meta={"skill": "ep-craft"})
        rep0 = profiles.analyze(m0, png_c, names_c)
        d_u = vc_file.loads(vc_file.dumps(figure.build_document(m0, rep0, png_c, "t")))[0]["document"]
        top_u = {"format": "vectorcraft", "version": 4, "document": d_u, "images": {}}
        lab_u = vc_file.lay_children(vc_file.layer_by_name(d_u, "labels"))
        n2 = [n for n in lab_u if n["name"] == "label lane 2"][0]
        n2["kind"]["xf"] = vc_file.rot_xf(n2["kind"]["xf"][4], n2["kind"]["xf"][5], 90.0)
        m_u, _notes = workbench.from_document(d_u)
        rep_u = profiles.analyze(m_u, png_c, [None] * 15)
        ops_u, meta_u = figure.plan_sync(d_u, m_u, rep_u)
        touched = [op for op in ops_u if op.get("id") == n2["id"]]
        assert touched and all(op["op"] == "move" for op in touched), touched
        assert abs(meta_u["lane_angles"][1] - 90.0) < 1e-9 and abs(meta_u["lane_angles"][0]) < 1e-9, meta_u["lane_angles"]
        figure.apply_ops_json(top_u, ops_u)
        assert vc_file.text_info(n2)["angle"] == 90.0, "sync 把用户转的角度改回去了"
        seg_u = [n for n in vc_file.lay_children(vc_file.layer_by_name(d_u, "gel")) if n["name"] == "gel seg 1"][0]
        gxu, gyu = (seg_u["kind"]["children"][0]["kind"]["path"]["subpaths"][0]["anchors"][0]["p"])
        bu = text_layout.box_offsets(w_c, figure.LANE_SIZE, 90.0, "Center")
        assert abs(n2["kind"]["xf"][5] + bu[3] - (gyu - figure.LANE_GAP_PT)) < 0.01, "转过的标签没按竖排重新落位"
        m_u2, _n = workbench.from_document(d_u)
        assert figure.plan_sync(d_u, m_u2, profiles.analyze(m_u2, png_c, [None] * 15))[0] == [], "第二次 sync 不幂等"

        # (d) 配置入口：lanes.json 每项 {text, angle}；markers.json 每项 angle
        import gel_photo
        p_lanes = os.path.join(td, "lanes.json")
        with open(p_lanes, "w", encoding="utf-8") as f:
            json.dump({"labels": ["M", {"text": "A", "angle": 90}, ""], "marker_lanes": [1, 2]}, f)
        p_mk = os.path.join(td, "mk.json")
        with open(p_mk, "w", encoding="utf-8") as f:
            json.dump({"labels": [{"text": "100 kD", "angle": -90}, "10 kD"]}, f)
        assert gel_photo._lanes_and_markers(types.SimpleNamespace(markers=p_mk, preset=None, lanes=p_lanes), 15)[:2] == \
            (["M", "A", ""] + [""] * 12, [None, 90.0, None] + [None] * 12)
        m_cfg, _sd = workbench.from_image(png_c, 8.6, 6.25, 15, 0.52, ["M", "A"] + [""] * 13,
                                          [{"text": "100 kD", "angle": -90}, "10 kD", "5 kD"], 1,
                                          marker_rows_cm=rows_c[:3], meta={"skill": "ep-craft"})
        assert m_cfg["meta"]["marker_labels"][0]["angle"] == -90.0 and "angle" not in m_cfg["meta"]["marker_labels"][1], \
            m_cfg["meta"]["marker_labels"]
    ok += 1

    # 80) 泳道匹配 = 从左往右数（V11，Issue #1 的最终口径）：第 i 条新泳道就是第 i 条旧泳道，不看距离、
    #     不设容差 —— 把最外侧那条线拖到胶边（中心 0.660 → 0.3949，正好半个孔距）名字不变；
    #     删一条 → 后面的名字前移；多一条 → 尾巴是 None；`tol` 参数已无效。
    prev15 = [0.660 + 0.520 * k for k in range(15)]
    assert workbench.match_lanes(prev15, prev15) == list(range(15))
    moved = [0.3949] + prev15[1:]                          # 第 1 条线拖到胶边：中心 0.660 → 0.3949
    assert workbench.match_lanes(prev15, moved) == list(range(15)), "拖边把名字弄丢了"
    assert workbench.match_lanes(prev15, prev15[1:]) == list(range(14))            # 删第 1 道 → 前移
    assert workbench.match_lanes(prev15, prev15 + [prev15[-1] + 0.52]) == list(range(15)) + [None]
    assert workbench.match_lanes([], [1.0, 2.0]) == [None, None]
    assert workbench.match_lanes([0.0, 1.0], []) == []
    assert workbench.match_lanes([0.0, 1.0], [0.90], tol=0.05) == [0]             # tol 不再起作用
    names15 = ["M"] + ["L%d" % i for i in range(1, 15)]
    fake_m = {"lanes": [{"left_cm": 0.0, "right_cm": 0.7898, "trimmed": False}]
                        + [{"left_cm": prev15[i] - 0.26, "right_cm": prev15[i] + 0.26, "trimmed": False}
                           for i in range(1, 15)]}
    assert workbench.lane_names_after(fake_m, prev15, names15) == names15, "拖边后名字没留住"
    ok += 1

    print("selftest %d/%d 通过" % (ok, ok))
    return 0


def _walk_keys(o):
    """递归收集 dict 的所有键名（record 键集测试用）。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _walk_keys(v)


def _synth_record():
    """selftest 用的「探测记录」形状的输入（workbench.from_record 吃它）：15 条泳道、每条 3 条带，胶面 cm。

    老 record 模块已随 stage 2/3 一起去掉；from_record 仍是「给定泳道几何 + marker 行」的建模入口。
    """
    preset = _t2_preset(8.6, 6.6)
    lanes = [{"index": i, "x_cm": round((i - 0.5) * 0.565, 4), "width_cm": 0.2} for i in range(1, 16)]
    bands = {str(i): [{"y_cm": y} for y in (2.0, 2.8, 3.6)] for i in range(1, 16)}
    return preset, {"gel": {"width_cm": 8.6, "height_cm": 6.6}, "lanes": lanes, "bands": bands}


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
        doc, "ep-meta", 340, 220, json.dumps({"skill": "ep-craft", "px_per_cm": 47.2}),
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


def _synth_wells(path, first_cm, pitch_cm, n=15, ladder_rows_cm=()):
    """selftest 用的胶面图（8.6 × 6.25 cm，100 px/cm）：顶上 n 个 U 形加样孔轮廓（孔宽 0.36 cm），
    可选在第 1 道画一列 marker 带；整张加固定种子的高斯纹理。"""
    draw = ["-fill", "none", "-stroke", "gray(70)", "-strokewidth", "2"]
    for k in range(n):
        c = (first_cm + k * pitch_cm) * 100
        a, b = c - 18, c + 18
        draw += ["-draw", "polyline %.1f,0 %.1f,8 %.1f,8 %.1f,0" % (a, a, b, b)]
    for y in ladder_rows_cm:
        c = first_cm * 100
        draw += ["-stroke", "none", "-fill", "gray(80)",
                 "-draw", "rectangle %.1f,%d %.1f,%d" % (c - 15, y * 100 - 3, c + 15, y * 100 + 2)]
    # 加一层固定种子的纹理：纯色合成图的噪声为 0，会让 top-hat 在带间鼓出的假峰也过门槛（真胶不会这样）
    _run([MAGICK, "-size", "860x625", "xc:gray(200)"] + draw
         + ["-seed", "7", "-attenuate", "0.25", "+noise", "Gaussian", "-depth", "8", path])
