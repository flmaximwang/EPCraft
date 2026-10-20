#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gel_photo.py —— 凝胶电泳照片：认胶面 → 正畸裁切 → 按两个 JSON 标注 → 出 PNG（dpi 可调）

子命令
    probe   <图>                          只探测：报胶框/角度/泳道 x/条带行；--debug 出一张覆盖图
    render  <图> --out 出.png [配置...]    出成品：裁切+正畸（+缩放+dpi）→ 写字 → PNG
    doctor                                环境自检
    selftest                              不读用户文件的算法自检

依赖：系统 python3（纯 stdlib）+ ImageMagick 7（`magick`）。像素只在分析尺度上读，成图交给 magick。
本文件只放 CLI 与 probe/render 编排；实现分在同目录模块：
    magick_io    magick 外呼 / PPM        gel_detect   认胶面          lane_detect  泳道与条带行
    geometry     旋转与位置口径           text_layout  字体与写字      config       配置读取
    debug_overlay 覆盖图                  env_check    doctor          selftest     自检
本文件不带任何数据路径：预设与胶体尺寸由调用方用 --markers / --gel-size 显式给出。

坐标约定
    - 全程用「自动定向（-auto-orient）后」的原图像素坐标。
    - 分析用缩略图（默认长边 700）找胶；所有几何量按比例回到原图。
    - 角度是 magick 的 `-rotate` 语义（正数 = 顺时针），与 geometry.rotate_pt() 同一套矩阵（selftest 实测锚定）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import as_entry, load_cfg, load_gel_size, load_markers, parse_gel_cm      # noqa: E402
from debug_overlay import draw_debug                                                 # noqa: E402
from env_check import cmd_doctor                                                     # noqa: E402
from gel_detect import detect_gel                                                    # noqa: E402
from geometry import (lane_pitch, load_dims_after, pos_or_frac, rotate_pt,           # noqa: E402
                      rotated_rect_bbox)
from lane_detect import fat_rows, group_rows, lanes_and_bands                        # noqa: E402
from magick_io import MAGICK, _run                                                   # noqa: E402
from mw_report import format_report, marker_pairs, report as mw_report_build   # noqa: E402
from selftest import selftest                                                        # noqa: E402
from text_layout import annotate_cmd, ink_height, pick_font, text_extent             # noqa: E402


def _rect_arg(s):
    if not s:
        return None
    v = [float(t) for t in s.replace(",", " ").split()]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("--rect 要 4 个数：x0,y0,x1,y1（原图像素）")
    return tuple(v)


def cmd_probe(args):
    info = detect_gel(args.image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("cross_cue"):
        print("两条判据的胶框（分析尺度）: dark=%s / chroma=%s" % (
            info["cross_cue"].get("dark"), info["cross_cue"].get("chroma")))
    ang = args.angle if args.angle is not None else info["angle_vertical"]
    lb = lanes_and_bands(args.image, info["bbox_analysis"], ang, args.longest,
                         cue=args.cue, thr=args.ink_thr)
    print("角度: 竖边拟合 %.3f°；本次用 %.3f°" % (info["angle_vertical"], ang))
    print("判据：胶内中位数 %s；条带行阈值 %.1f（严格）· 泳道阈值 %.1f（宽松，淡带也算）" % (
        lb["gel_median"], lb["band_thr"], lb["lane_thr"]))
    print("间距估计 %.1f px（量自第 %s 行：条带最密的一行）；噪声候选丢掉 %d 条；差值对不上整数格未补 %s" % (
        lb["pitch"] or 0, lb.get("pitch_row"), lb["dropped"], lb["skipped"] or "无"))
    print("泳道数 %d（实测 %d + 补位 %d），中心 x（分析尺度）: %s" % (
        len(lb["lanes"]), lb["measured"], len(lb["lanes"]) - lb["measured"],
        [c for c, _ in lb["lanes"]]))
    if len(lb["lanes"]) > lb["measured"]:
        print("  补位（无像素证据，按相邻间距均分）: %s" % (
            [c for (c, _), it in zip(lb["lanes"], lb["interp"]) if it]))
    for i, ((c, wid), bands) in enumerate(zip(lb["lanes"], lb["bands"]), 1):
        print("  泳道 %2d x=%7.1f 宽=%3d 条带 %2d 行: %s" % (
            i, c, wid, len(bands), [int(b[0]) for b in bands]))
    if args.debug:
        draw_debug(args.image, info, lb, args.debug)
        print("debug 覆盖图:", args.debug, "（红框=胶面，红/橙线=泳道，绿=细栅格探到的条带行，"
              "左侧数字=分析尺度 y 标尺；手动标定照这个 y 写 markers.y_px）")
    return 0


def cmd_render(args):
    info = detect_gel(args.image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
    ang = args.angle if args.angle is not None else round(info["angle_vertical"], 3)
    if args.no_rotate:
        ang = 0.0
    (W0, H0), (W1, H1) = load_dims_after(args.image, ang)

    # 两个配置：字段表见 references/photo-labeling-conventions.md §1
    if not args.markers:
        raise RuntimeError("要 --markers <json>（单个配置，或 data/markers.json 这种预设库 + --preset）")
    mk, mk_labels, mk_note = load_markers(args.markers, args.preset)
    print("[i] marker 配置：%s" % mk_note)
    ln = ln_labels = None
    if args.lanes:
        ln, ln_labels = load_cfg(args.lanes, "泳道配置")
    font = args.font or pick_font(mk.get("font") or (ln or {}).get("font"))
    dpi = args.dpi
    if args.gel_cm:
        gel = parse_gel_cm(args.gel_cm)
    elif args.gel_size:
        gel = load_gel_size(args.gel_size)
    else:
        raise RuntimeError("要 --gel-size <json>（如 data/gel_size.json）或 --gel-cm 宽x高")

    def pt2px(v):
        """pt（物理尺寸，dpi 无关）→ 成品像素。字号/间距用 pt，才不会「改 dpi 就改字与胶的相对大小」。"""
        return float(v) / 72.0 * dpi
    print("[i] 胶体预设 %s：宽 %.2f cm × 高 %.2f cm（%s）；输出 %d dpi" % (
        gel.get("name", "命令行"), gel["width_cm"], gel["height_cm"], gel.get("_source", ""), dpi))

    gel_bb, _ = rotated_rect_bbox(info["rect_orig"], ang, (W0, H0), (W1, H1))
    a2o = info["image"][0] / float(info["analysis"][0])
    ymid = (info["bbox_analysis"][1] + info["bbox_analysis"][3]) / 2.0

    def rot_pt(x, y):
        """原图坐标 → 旋转后画布坐标（与 magick -rotate 同一套矩阵）。"""
        rx, ry = rotate_pt(x, y, W0 / 2.0, H0 / 2.0, ang)
        return rx + (W1 - W0) / 2.0, ry + (H1 - H0) / 2.0

    lb = lanes_and_bands(args.image, info["bbox_analysis"], ang, args.longest,
                         cue=args.cue, thr=args.ink_thr,
                         want=len(ln_labels) if ln_labels else None)
    lane_x = [rot_pt(c * a2o, ymid * a2o)[0] for c, _ in lb["lanes"]]
    lane_w = [wd * a2o for _, wd in lb["lanes"]]
    print("[i] 泳道 %d 条（实测 %d + 补位 %d），间距 %.1f px(分析) ; %s" % (
        len(lane_x), lb["measured"], len(lane_x) - lb["measured"], lb["pitch"] or 0,
        font or "magick 默认字体"))

    # 定标（cm / 旋转画布 px）：①默认「整块胶宽 = width_cm」；
    # ②照片是**裁过**的、只含 N 条泳道时（用户说了就 --gel-lanes N），①的前提不成立
    #   —— 改用**泳道间距**定标：实测每条占多少 px ↔ 预设的 lane_pitch_cm。
    #   可见宽度于是按泳道数算出来（N × 间距），不再假装它是整块胶。
    if args.gel_lanes and gel.get("lane_pitch_cm") and len(lane_x) >= 2:
        if int(args.gel_lanes) != len(lane_x):
            print("[!] 你说 %d 条泳道，探测到 %d 条 —— 先核一下泳道有没有并/漏；"
                  "定标用的是探测到的间距" % (int(args.gel_lanes), len(lane_x)))
        pitch_px = (lane_x[-1] - lane_x[0]) / (len(lane_x) - 1)
        cm_px = float(gel["lane_pitch_cm"]) / pitch_px
        print("[i] 按泳道定标：%d 条 × %.3f cm = 胶面宽 %.2f cm（裁过的照片；"
              "整块预设 %.2f cm 不适用）" % (len(lane_x), gel["lane_pitch_cm"],
                                        len(lane_x) * gel["lane_pitch_cm"], gel["width_cm"]))
    else:
        cm_px = gel["width_cm"] / (gel_bb[2] - gel_bb[0])
    # 成品 PNG 就是**实际尺寸**：1 px = 1/dpi 英寸。
    # 量纲：cm_px 是「cm / 画布 px」，所以 px_out/画布px = (px_out/cm) × (cm/画布px) —— **乘**。
    scale = (dpi / 2.54) * cm_px

    if not (1e-4 < scale < 1e3):
        raise RuntimeError("定标算出来不合理（scale=%.6g px/画布px）—— cm_px 量纲写反了？"
                           "预设 %.2f cm / 探测宽 %d px / 泳道 %s"
                           % (scale, gel["width_cm"], gel_bb[2] - gel_bb[0], args.gel_lanes or "—"))
    # 上下缘再裁掉一段（探测框上缘含上样孔/梳齿；那一段常常不想入图）。
    # 用同一个 cm_px 换算，裁上下不动宽度，所以定标不受影响。
    tt = args.trim_top_cm if args.trim_top_cm is not None else float(gel.get("trim_top_cm", 0.0) or 0.0)
    tb = args.trim_bottom_cm if args.trim_bottom_cm is not None else float(gel.get("trim_bottom_cm", 0.0) or 0.0)
    if tt or tb:
        h_before = (gel_bb[3] - gel_bb[1]) * cm_px
        gel_bb = (gel_bb[0], gel_bb[1] + tt / cm_px, gel_bb[2], gel_bb[3] - tb / cm_px)
        print("[i] 上/下缘各裁 %.2f / %.2f cm：胶面 %.2f cm → %.2f cm（探测框实测）" % (
            tt, tb, h_before, (gel_bb[3] - gel_bb[1]) * cm_px))

    implied_h = (gel_bb[3] - gel_bb[1]) * cm_px
    if abs(implied_h - gel["height_cm"]) > 0.10 * gel["height_cm"]:
        print("[!] 探测框的比例推出来高度 %.2f cm，预设写 %.2f cm（差 %.0f%%）—— "
              "按宽度定标不受影响；要按你的数就用 --gel-cm，或调 --margin-frac / --rect。"
              % (implied_h, gel["height_cm"], abs(implied_h - gel["height_cm"]) / gel["height_cm"] * 100))

    # ---- 泳道名（gap = 文字底边离胶面顶边，align 相对泳道中心）
    items = []
    if ln_labels:
        size = float(ln.get("size", 8))            # pt（dpi 无关）
        gap = pt2px(ln.get("gap", 0))
        align = ln.get("align", "center")
        color = ln.get("color", "#000000")
        xf = ln.get("x_frac")
        if xf:
            if len(xf) != len(ln_labels):
                raise RuntimeError("lanes.x_frac 有 %d 个，labels 有 %d 个" % (len(xf), len(ln_labels)))
            xs = [pos_or_frac(f, gel_bb[0], gel_bb[2]) for f in xf]
        else:
            if len(ln_labels) > len(lane_x):
                raise RuntimeError("lanes.labels 有 %d 条，只探到 %d 条泳道（缺的用 lanes.x_frac 显式给位置）"
                                   % (len(ln_labels), len(lane_x)))
            xs = lane_x[: len(ln_labels)]
        for e, x in zip(ln_labels, xs):
            en = as_entry(e, ln)
            t = en.get("text", "")
            if not t:
                continue
            s = pt2px(en.get("size", size))
            tw, th = text_extent(font, round(s), t)
            tw, th = tw / scale, th / scale
            ax = x - tw / 2.0 if align == "center" else (x if align == "left" else x - tw)
            # 口径：box **底边**落在胶顶之上 gap（
            # 而 -annotate 的 y 是 box **顶边**，所以这里减掉整个 box 高）。
            ay = gel_bb[1] - gap / scale - th
            items.append((ax, ay, t, s, en.get("color", color)))

    # ---- marker 分子量（marker_lane / marker_lane_x / lane_width /
    #      lane_edge=inner / gap = 文字与泳道**带边**的间距；左右按泳道区中线判）
    if mk_labels:
        lw = float(mk["lane_width"]) if mk.get("lane_width") else lane_pitch(lane_x)
        if mk.get("marker_lane_x") is not None:
            mx = pos_or_frac(mk["marker_lane_x"], gel_bb[0], gel_bb[2])
        else:
            idx = int(mk.get("marker_lane", 1))
            if idx == 0:
                mx = lane_x[0] - lw
            elif 1 <= idx <= len(lane_x):
                mx = lane_x[idx - 1]
            else:
                raise RuntimeError("markers 的 marker_lane 超出泳道数量（探到 %d 条）" % len(lane_x))
        mid = (min(lane_x) + max(lane_x)) / 2.0
        left_side = mx < mid
        edge = mx if mk.get("lane_edge") == "inner" else (mx - lw / 2.0 if left_side else mx + lw / 2.0)
        # 整列**不压在胶上**：按泳道带边算出的锚点若还在胶面内，就推到胶面之外（gap 从胶边起算）。
        # 左/右按（泳道区中线），只是把这一列挪进白底留白里。
        edge = min(edge, gel_bb[0]) if left_side else max(edge, gel_bb[2])
        gap = pt2px(mk.get("gap", 4))
        size = float(mk.get("size", 8))                # pt
        print("[i] 泳道带宽 ≈%.1f px(分析)（%s）；marker 泳道 x=%.1f（泳道区中线 %.1f 的%s边）"
              "→ 带边 %.1f → %s对齐，锚点离带边 %g px（成品像素）" % (
                  lw / a2o, "来自 markers.lane_width" if mk.get("lane_width") else
                  "相邻泳道中心距的中位数", mx, mid, "左" if left_side else "右", edge,
                  "右" if left_side else "左", float(mk.get("gap", 4))))
        idx = 0
        if mk.get("marker_lane_x") is None:
            idx = int(mk.get("marker_lane", 1)) - 1
            if idx < 0:
                idx = 0
        elif mk.get("marker_lane") is not None:
            idx = int(mk["marker_lane"]) - 1
        else:
            idx = max(range(len(lb["bands"])), key=lambda i: len(lb["bands"][i]))
            print("[i] marker_lane 未给 → 自动选条带最多的第 %d 条（%d 条带）"
                  % (idx + 1, len(lb["bands"][idx])))
        idx = max(0, min(idx, len(lb["bands"]) - 1))
        # 一条标签可以盖 n 条带（默认 1）：分子量太近、并着跑的那对（21/20 kD）写成
        # {"text": "21/20 kD", "rows": 2}，配对时吃掉 2 条带、标签落在它们中间 —— 条目数
        # 就不再要求等于**条带数**，而是等于**带数之和**，这样「拒绝硬配」这道闸仍然有效。
        span = [max(1, int(as_entry(e, mk).get("rows", 1))) for e in mk_labels]
        need_rows = sum(span)
        bs = lb["bands"][idx]
        rows = [b[0] for b in bs]
        ths = sorted(b[1] for b in bs)
        med_th = ths[len(ths) // 2] if ths else 0.0
        fat = fat_rows(bs)
        print("[i] marker 用第 %d 条泳道（x=%.1f，细栅格扫到 %d 条带：%s）"
              % (idx + 1, lane_x[idx], len(rows), [round(r, 1) for r in rows]))
        if fat and not any(k >= 2 for k in span):      # 已用 rows:2 声明过并带就不再唠叨
            mm_apx = gel["width_cm"] * 10.0 / max(1e-6, (gel_bb[2] - gel_bb[0]) / a2o)   # 分析 px → mm
            sep_mm = max(0.0, (max(b[1] for b in bs) - med_th)) * mm_apx
            print("[!] 其中 %s（行 y × 厚/中位数）明显偏厚 —— 很可能是**两条分子量接近的带并成了一条**"
                  "（Tris-Tricine 的 21/20 kD 就是这样）。\n"
                  "    估这一对挨着 ≈%.1f mm，而 %g pt 的字高 ≈%.1f mm —— 同侧竖直方向放不下两行字，"
                  "\n    换 dpi 也没用（字号和间距按同一比例缩放）。所以：\n"
                  "    ① 合成一条标签（推荐）：labels 里那一对写成 \"21/20 kD\"，labels 与 y_px 一起去掉一条，"
                  "\n       条目数与条带数一对上，配对就自动正确；\n"
                  "    ② 两条都标：得把一个标签挪到梯子**另一侧**再拉条引线（要的话说一声）。"
                  % (", ".join("%.1f ×%.1f" % f for f in fat), sep_mm, size, size / 72.0 * 25.4))
        yf, yp = mk.get("y_frac"), mk.get("y_px")
        if yp:
            if len(yp) != need_rows:
                raise RuntimeError("markers.y_px 有 %d 个，labels 需要 %d 个（rows 字段求和）"
                                   % (len(yp), need_rows))
            if mk.get("snap", False):       # 默认不吸附：现在的自动行在密集条带上有 ~0.7 mm 系统偏差，
                tol = 0.25 * (lb.get("row_spacing") or 8.0)   # 吸附会把人在图上读出来的准值拉偏
                rows = [min(rows, key=lambda r: abs(r - float(v)))
                        if abs(min(rows, key=lambda r: abs(r - float(v))) - float(v)) <= tol else float(v)
                        for v in yp]
            else:
                rows = [float(v) for v in yp]
            mv = [round(b - a, 1) for a, b in zip(yp, rows) if abs(a - b) > 0.6]
            print("[i] marker 行用 markers.y_px（分析尺度 y，来自 debug 图的标尺）；吸附到最近条带峰：%s"
                  % ("无移动" if not mv else "移动 %s" % mv))
        elif yf:
            if len(yf) != need_rows:
                raise RuntimeError("markers.y_frac 有 %d 个，labels 需要 %d 个（rows 字段求和）"
                                   % (len(yf), need_rows))
            gy0, gy1 = info["bbox_analysis"][1], info["bbox_analysis"][3]
            rows = [gy0 + f * (gy1 - gy0) for f in yf]
        elif len(rows) != need_rows:
            hint = ("自动探到 %d 条带、配置有 %d 个分子量；这条梯子的条带在像素上分不开，"
                    "按位置配对会整体错位，所以默认不出图。\n"
                    "  ① 手动标定（推荐）：`probe --debug debug.png` 出的图左侧有 y 标尺，照着把每条带的 y\n"
                    "     写进 markers.json 的 \"y_px\"（数组，从上到下、与 labels 一一对应，给大概值即可，\n"
                    "     脚本会吸附到最近的条带峰），再跑一次。\n"
                    "  ② 换对预设：这管 marker 的条带数要与 labels 一致。\n"
                    "  ③ 数对不上且只差 1：多半是有一对分子量接近的带（21/20 kD）并着跑，\n"
                    "     把那一对写成一条标签并在它上面加 \"rows\": 2（它吃 2 条带、落在两者中间）；\n"
                    "     把那一对合成一条标签（labels 里写 \"20/21 kD\"）即可，别硬凑。\n"
                    "  ④ 硬配（可能错位）：加 --pair-force。"
                    % (len(rows), need_rows))
            if not args.pair_force:
                raise RuntimeError(hint)
            print("!! --pair-force：按位置硬配前 %d 个\n%s" % (min(len(rows), len(mk_labels)), hint))
            rows = rows[: len(mk_labels)]
        # 分组：每条标签吃掉 span[i] 条带、落在它们中间（{"rows": 2} 的并带标签就是这么来的）
        eff = group_rows(rows, span)
        # 叠字检查要用**墨迹**高比行距（不是框高：框里含字体留白，拿它比会把放得下的也报出来）
        if len(eff) >= 2:
            to_out = a2o * scale
            ink = ink_height(font, int(round(pt2px(size))))
            tight = [(round(eff[i], 1), round(eff[i + 1] - eff[i], 1))
                     for i in range(len(eff) - 1)
                     if (eff[i + 1] - eff[i]) * to_out < 0.85 * ink]
            if tight:
                gap_out = min((eff[i + 1] - eff[i]) * to_out for i in range(len(eff) - 1))
                print("[!] 有 %d 处相邻标签会叠字：%s（上行 y, 行距分析 px）"
                      % (len(tight), ", ".join("%.1f(+%.1f)" % t for t in tight)))
                print("    墨迹高 %d px、最近的行距只有 %.0f px（%d dpi）→ 两行都不叠的话字号得 ≤ %.1f pt"
                      % (ink, gap_out, dpi, gap_out * 72.0 / dpi))
                print("    更省事：把那一对写成一条标签，并给它加 \"rows\": 2（吃 2 条带、落在两者中间）。")
        _, my = rot_pt(info["bbox_analysis"][0] * a2o, rows[0] * a2o) if rows else (0, 0)
        for row, e in zip(eff, mk_labels):
            en = as_entry(e, mk)
            t = en.get("text", "")
            if not t:
                continue
            s = pt2px(en.get("size", size))
            tw, th = text_extent(font, round(s), t)
            tw, th = tw / scale, th / scale
            _, yy = rot_pt(info["bbox_analysis"][0] * a2o, row * a2o)
            ax = (edge - gap / scale - tw) if left_side else (edge + gap / scale)
            # 口径：box **竖直中心**对齐条带行（
            # -annotate 的 y 是 box 顶边，所以上移半个 box 高）。
            ay = yy - th / 2.0
            items.append((ax, ay, t, s, en.get("color", mk.get("color", "#000000"))))

    # ---- 外扩留白（按文字真实包围盒）→ 裁切 → 写字
    base_m = args.margin_frac * max(gel_bb[2] - gel_bb[0], gel_bb[3] - gel_bb[1])
    pad = max(6.0, base_m * 0.4)
    need = {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0}
    for ax, ay, t, s, _c in items:
        tw, th = text_extent(font, round(s), t)
        tw, th = tw / scale, th / scale
        need["l"] = max(need["l"], gel_bb[0] - ax)
        need["r"] = max(need["r"], ax + tw - gel_bb[2])
        need["t"] = max(need["t"], gel_bb[1] - ay)          # ay = box 顶边
        need["b"] = max(need["b"], (ay + th) - gel_bb[3])   # box 底边 = ay + th
    L = base_m + max(0.0, need["l"] - base_m + pad)
    R = base_m + max(0.0, need["r"] - base_m + pad)
    T = base_m + max(0.0, need["t"] - base_m + pad)
    B = base_m + max(0.0, need["b"] - base_m + pad)

    # 严格按胶面裁切 → 贴到**白底**画布上 → 文字只写在白底留白里。
    # 好处：① 胶面不多带一个像素；② 上缘的加样孔/灯箱背景不再压在文字下面（字全在白底上）。
    gx0, gy0 = max(0, int(round(gel_bb[0]))), max(0, int(round(gel_bb[1])))
    gx1, gy1 = min(W1, int(round(gel_bb[2]))), min(H1, int(round(gel_bb[3])))
    gw, gh = int(round((gx1 - gx0) * scale)), int(round((gy1 - gy0) * scale))
    sx, sy = gw / float(gx1 - gx0), gh / float(gy1 - gy0)     # 裁切→成品的真实比例（免得有亚像素漂）
    # 注意单位：L/R/T/B 是在 gel_bb（胶面坐标）里算的，要乘比例才是成品像素
    L, R = int(round(L * sx)), int(round(R * sx))
    T, B = int(round(T * sy)), int(round(B * sy))
    W, H = gw + L + R, gh + T + B
    # 白底用**新建画布 + composite**（不是 -extent：实测 IM7 的 -extent 带 gravity 时偏移量被忽略，
    # 图会贴在左上角、只往右下补白 —— 边距全错）。
    base = [MAGICK, "-size", "%dx%d" % (W, H), "xc:white", "(", args.image, "-auto-orient"]
    if abs(ang) > 1e-6:
        base += ["-rotate", "%.4f" % ang]
    base += ["-crop", "%dx%d+%d+%d" % (gx1 - gx0, gy1 - gy0, gx0, gy0), "+repage",
             "-resize", "%dx%d!" % (gw, gh),                    # 严格贴合胶面
             "+profile", "*", ")"]   # 注意别用 -strip：IM7 里它会把后面设的 dpi 一起丢掉（实测）
    base += ["-gravity", "northwest", "-geometry", "+%d+%d" % (L, T), "-composite"]
    out_items = [(L + (ax - gx0) * sx, T + (ay - gy0) * sy, t, s, c) for ax, ay, t, s, c in items]
    if args.debug:
        for ox, oy, t, s, c in out_items:
            print("    [item] %-8s x0=%7.1f y0=%7.1f (pt=%.1f)" % (t, ox, oy, s))
    cmd = annotate_cmd(base, out_items, font, None if args.no_outline else args.outline)
    cmd += ["-units", "PixelsPerInch", "-density", str(dpi), args.out]
    _run(cmd)
    if ln_labels and not args.no_mw and len(rows) == sum(span):
        mset = {int(k) - 1 for k in (ln or {}).get("marker_lanes", [])} | {idx}
        rep = mw_report_build(lb, [as_entry(e, ln).get("text", "") for e in ln_labels], mset, idx,
                              marker_pairs(mk_labels, span, rows, as_entry), mk_labels, span, as_entry,
                              args.mw_method)
        if "marker_lanes" not in (ln or {}):
            rep["notes"].append("lanes.json 没有 marker_lanes：其余 marker 泳道会被当成样品报出来")
        print(format_report(rep))
        mw_path = os.path.splitext(args.out)[0] + ".mw.json"
        with open(mw_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print("[MW] 已写 %s" % mw_path)
    if args.debug:
        draw_debug(args.image, info, lb, args.debug, cfg_rows=rows if mk_labels else None)
        print("debug 覆盖图:", args.debug, "（红框=胶面，红/绿线=泳道/条带行，品红=配置指定的 marker 行）")
    gel_w_px, gel_h_px = (gel_bb[2] - gel_bb[0]) * scale, (gel_bb[3] - gel_bb[1]) * scale
    print("输出 %s（%d×%d px, %d dpi；%d 条文字）= 胶面 %d×%d + 白底留白 L%d R%d T%d B%d" % (
        args.out, W, H, dpi, len(out_items), gw, gh, L, R, T, B))
    print("[i] 实际尺寸：胶面 %.2f×%.2f cm（%d×%d px）· 成品 %.2f×%.2f cm（含白底留白）" % (
        gw / dpi * 2.54, gh / dpi * 2.54, gw, gh, W / dpi * 2.54, H / dpi * 2.54))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="凝胶电泳照片 → 裁切正畸 → 配置标注 → PNG")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_detect(a):
        a.add_argument("--longest", type=int, default=700, help="分析缩略图长边（默认 700）")
        a.add_argument("--cue", choices=("dark", "chroma", "union"), default="dark",
                       help="认胶的判据：dark=比背景暗（通用）/ chroma=蓝度（偏蓝光下的染色胶）/ union")
        a.add_argument("--ink-thr", type=float, default=None, help="dark 判据的阈值（默认 max(6, 背景//20)）")
        a.add_argument("--rect", help="跳过自动找胶，直接给原图像素矩形 x0,y0,x1,y1")

    p = sub.add_parser("probe", help="只探测：胶框/角度/泳道/条带")
    p.add_argument("image")
    p.add_argument("--angle", type=float, default=None, help="强制用这个角度（度，正=顺时针）")
    p.add_argument("--debug", help="出一张带检测结果的覆盖图")
    add_detect(p)
    p.set_defaults(func=cmd_probe)

    r = sub.add_parser("render", help="出成品 PNG")
    r.add_argument("image")
    r.add_argument("--out", required=True)
    r.add_argument("--markers", help="marker 配置 JSON；单个配置，或预设库（如 data/markers.json，配合 --preset）")
    r.add_argument("--preset", help="从预设库里选一个：1 起的序号，或 _preset 描述里的关键词")
    r.add_argument("--lanes", help="泳道配置 JSON")
    r.add_argument("--dpi", type=int, default=96, help="成品 PNG 的 dpi（默认 96；像素数 = 胶面 cm/2.54×dpi）")
    r.add_argument("--pair-force", action="store_true",
                   help="条带数与配置对不上时仍按位置硬配（可能整体错位；默认拒绝出图）")
    r.add_argument("--gel-size", help="胶体尺寸 JSON（如 data/gel_size.json）")
    r.add_argument("--gel-cm", help="直接给胶体尺寸 宽x高（cm），如 8.6x6.9，压过 --gel-size")
    r.add_argument("--gel-lanes", type=int, default=None,
                   help="照片是裁过的、只含 N 条泳道时给 N：改按--gel-size 里的 lane_pitch_cm 的泳道间距定标")
    r.add_argument("--trim-top-cm", type=float, default=None,
                   help="从胶面上缘再裁掉多少 cm（探测框含上样孔/梳齿那一段；默认读--gel-size 里的 trim_top_cm）")
    r.add_argument("--trim-bottom-cm", type=float, default=None,
                   help="从胶面下缘再裁掉多少 cm（默认读--gel-size 里的 trim_bottom_cm）")
    r.add_argument("--angle", type=float, default=None)
    r.add_argument("--no-rotate", action="store_true")
    r.add_argument("--margin-frac", type=float, default=0.05, help="四周外扩（占胶面长边比例，默认 0.05）")
    r.add_argument("--font", help="字体文件路径（默认 Arial；配置里的 font 只在是路径时才用）")
    r.add_argument("--outline", default=None,
                   help="文字白边颜色（默认不描边：成品是白底；要压在胶上时才给 white）")
    r.add_argument("--no-outline", action="store_true", help="不要白边")
    r.add_argument("--mw-method", choices=("logfit", "interp"), default="logfit",
                   help="样品带分子量的标定方法：logfit=log10(MW)~y 直线（会外推）/ interp=marker 带之间内插（不外推）")
    r.add_argument("--no-mw", action="store_true", help="不输出分子量报告")
    r.add_argument("--debug", help="再出一张检测覆盖图")
    add_detect(r)
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("selftest")
    s.set_defaults(func=lambda a: selftest())

    d = sub.add_parser("doctor", help="环境自检：python / magick / 字体 / 数据文件")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
