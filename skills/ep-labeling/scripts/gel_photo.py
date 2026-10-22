#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gel_photo.py —— 凝胶电泳照片：认胶面 → 正畸裁切 → 按两个 JSON 标注 → 出 PNG（dpi 可调）

子命令
    probe   <图>                          只探测：报胶框/角度/泳道 x/条带行；--debug 出一张覆盖图
    render  <图> --out 出.png [配置...]    出成品：裁切+正畸（+缩放+dpi）→ 写字 → PNG
    export  <scene.json|fig.html> --png 出.png [--svg 出.svg] [--html 出.html]
                                          只读 scene 出图（不跑检测）
    doctor                                环境自检
    selftest                              不读用户文件的算法自检

依赖：系统 python3（纯 stdlib）+ ImageMagick 7（`magick`）。像素只在分析尺度上读，成图交给 magick。
本文件只放 CLI 与 probe/render 编排；实现分在同目录模块：
    magick_io    magick 外呼 / PPM        gel_detect   认胶面          gel_area     stage 1 胶面区域
    lane_fit     stage 2 泳道格点         band_fit     stage 3 条带     geometry    旋转与位置口径
    text_layout  字体与写字               config       配置读取        debug_overlay 覆盖图
    scene        版式数据（cm/pt）        scene_png    scene → PNG（唯一的画字路径）
    scene_svg    scene → SVG             env_check    doctor          selftest     自检
    vc_file      .vectorcraft 读写         vc_control   VectorCraft 控制口
    workbench    Workbench 模型（建、画、读回、校验、泳道名匹配）
    profiles     泳道剖面、条带、标定、MW 报告 v2
    well_grid    泳道种子：按加样孔定相的等距梳子
    figure       Figure / Profiles 画板、整份文档、sync 增量计划（plan_sync）
本文件不带任何数据路径：预设与胶体尺寸由调用方用 --markers / --gel-size 显式给出。

坐标约定
    - 全程用「自动定向（-auto-orient）后」的原图像素坐标。
    - 分析用缩略图（默认长边 700）找胶；所有几何量按比例回到原图。
    - 角度是 magick 的 `-rotate` 语义（正数 = 顺时针），与 geometry.rotate_pt() 同一套矩阵（selftest 实测锚定）。
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import as_entry, load_cfg, load_gel_size, load_markers, parse_gel_cm      # noqa: E402
from band_fit import find_bands, fat_rows, group_rows                                # noqa: E402
from debug_overlay import draw_debug                                                 # noqa: E402
from editor import DEFAULT_TEMPLATE as EDITOR_TEMPLATE                     # noqa: E402
from editor import extract as editor_extract, write_html as editor_write_html  # noqa: E402
from env_check import cmd_doctor                                                     # noqa: E402
from gel_area import compute, cross_check_scale                                      # noqa: E402
from gel_detect import detect_gel                                                    # noqa: E402
from geometry import lane_pitch, load_dims_after, pos_or_frac, rotate_pt             # noqa: E402
from lane_fit import find_lanes                                                      # noqa: E402
from lane_select import (cropped_out_markers, layout as lane_layout,                # noqa: E402
                         nearest_marker, parse_keep, strips as lane_strips)
from magick_io import MAGICK, _run                                                   # noqa: E402
from mw_report import format_report, report as mw_report_build                       # noqa: E402
from record import (bands_from_rows as record_bands_from_rows,                   # noqa: E402
                    build as record_build, write as record_write)
from scene import (build as scene_build, load as scene_load,                   # noqa: E402
                   write as scene_write)
from scene_png import write_png as scene_png_write                                     # noqa: E402
from scene_svg import write_svg as scene_svg_write                                     # noqa: E402
from selftest import selftest                                                        # noqa: E402
from text_layout import ink_height, pick_font, text_extent                           # noqa: E402


def _rect_arg(s):
    if not s:
        return None
    v = [float(t) for t in s.replace(",", " ").split()]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("--rect 要 4 个数：x0,y0,x1,y1（原图像素）")
    return tuple(v)


def _print_stage1(ga, preset, gel_lanes=None, show_checks=True):
    """stage 1 的一行汇报（定标 / 定形 / 交叉核对）。失败项 compute 已经打过 [!]，这里只汇总通过项。"""
    tt = float(preset.get("trim_top_cm", 0.0) or 0.0)
    tb = float(preset.get("trim_bottom_cm", 0.0) or 0.0)
    if ga["source"]["px_per_cm"] == "lane_pitch":
        cal = ("%d 条 × %.3f cm = %.2f cm（裁过的照片；整块预设 %.2f cm 不适用）"
               % (int(gel_lanes), float(preset["lane_pitch_cm"]), ga["width_cm"],
                  float(preset["width_cm"])))
    else:
        cal = "整块宽 %.2f cm" % ga["width_cm"]
    print("[i] 胶面（stage 1）：定标 = %s → %.2f px/cm；高 %.2f cm = 预设 %.2f − trim 上/下 %.2f/%.2f，"
          "anchor=%s（框来自 %s）" % (cal, ga["px_per_cm"], ga["height_cm"], float(preset["height_cm"]),
                                      tt, tb, preset.get("anchor", "bottom"), ga["source"]["box"]))
    if show_checks:
        _print_checks([c for c in ga["checks"] if c["ok"]])


def _print_checks(checks):
    """一行汇总交叉核对：通过的打 ok，没通过的打 WARN（[!] 细节在各 stage 已经打过）。"""
    if not checks:
        return
    print("[i] checks: %s" % " · ".join(
        "%s %s (%s)" % (c["name"], "ok" if c["ok"] else "WARN", c["detail"]) for c in checks))


def _crop_record(rec_full, keep, crop, strips4, total_w_cm):
    """整块胶的 record → 只含保留泳道的 record（x_cm 映射到拼接版式）。

    rec_full 仍是 MW 定标的输入（marker 泳道可能已被裁掉）；本函数只改**写盘/打印**的那份。
    """
    by_lane = {}
    for s in strips4:
        for k in s["lanes"]:
            by_lane[int(k)] = s
    kept = set(int(k) for k in keep)
    lanes = []
    for ln in rec_full["lanes"]:
        k = int(ln["index"])
        if k not in kept:
            continue
        s = by_lane[k]
        d = dict(ln)
        d["x_cm"] = round(s["dst_x0_cm"] + (float(ln["x_cm"]) - s["src_x0_cm"]), 4)
        lanes.append(d)
    rec = dict(rec_full)
    rec["gel"] = {"width_cm": round(float(total_w_cm), 4),
                  "height_cm": rec_full["gel"]["height_cm"]}
    rec["lanes"] = lanes
    rec["bands"] = {k: v for k, v in rec_full["bands"].items() if int(k) in kept}
    rec["crop"] = crop
    return rec


def run_stages(image, preset, args, gel_lanes=None):
    """串行管线只写一遍：stage 1 胶面 → stage 2 泳道 → stage 3 条带。

    每一级只读上一级的冻结输出（spec §2 决策 3）；probe 与 render 都只调用本函数一次，
    不再各自复制三段。返回 (info, ang, ga, lf, bb)，不打印 stage 表（调用方决定怎么汇报）。

    stage 1 的尺度核对在拿到 stage 2 的拟合 pitch 后**重跑**（spec §4.4）：用泳道间距定标
    vs 用宽度定标做交叉核对，替换掉 compute 里那条只用廉价估计的 scale 记录（只留一条）。
    """
    info = detect_gel(image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
    ang = args.angle if args.angle is not None else round(info["angle_vertical"], 3)
    if getattr(args, "no_rotate", False):
        ang = 0.0
    # CLI 的 trim 覆盖并进预设（render 有这两个参数；probe 上 getattr 给 None）。
    for key in ("trim_top_cm", "trim_bottom_cm"):
        v = getattr(args, key, None)
        if v is not None:
            preset[key] = v

    # ---- stage 1：胶面冻结（形状由预设决定；不再用条带/泳道反推）
    ga = compute(info, preset, ang, gel_lanes=gel_lanes)
    if gel_lanes and int(gel_lanes) != int(preset["lane_count"]):
        print("[i] 裁过的照片：按可见 %d 条泳道（整块预设 %d 条）"
              % (int(gel_lanes), int(preset["lane_count"])))

    # ---- stage 2：先验驱动的泳道格点（只读 stage 1 的冻结输出）
    lane_count = int(gel_lanes or preset["lane_count"])
    lf = find_lanes(image, ga, lane_count, longest=args.longest)

    # ---- stage 3：只读冻结的泳道 x 求条带（条带结果永不回流到 stage 2）
    bb = find_bands(image, lf["box_analysis"], [ln["x_analysis"] for ln in lf["lanes"]],
                    longest=args.longest, cue=args.cue, thr=args.ink_thr)

    # ---- stage 1 的尺度核对重跑：pitch 是 stage 2 的拟合值（分析 px）→ 旋转画布 px。
    a2x = info["image"][0] / float(info["analysis"][0])
    chk = cross_check_scale(ga["px_per_cm"], lf["pitch"] * a2x,
                            float(preset["lane_pitch_cm"]))
    ga["checks"] = [chk if c["name"] == "scale" else c for c in ga["checks"]]
    if not any(c["name"] == "scale" for c in ga["checks"]):
        ga["checks"].append(chk)
    return info, ang, ga, lf, bb


def cmd_probe(args):
    # T1：预设是强制先验。没有 --gel-size 时在检测之前就退出。
    if not args.gel_size:
        raise RuntimeError("gel preset required (--gel-size FILE)")
    gel = load_gel_size(args.gel_size)
    print("[i] 胶体预设 %s：%d 条泳道 × %.3f cm = %.2f cm；宽 %.2f × 高 %.2f cm（%s）" % (
        gel.get("name", "—"), int(gel["lane_count"]), float(gel["lane_pitch_cm"]),
        int(gel["lane_count"]) * float(gel["lane_pitch_cm"]), float(gel["width_cm"]),
        float(gel["height_cm"]), gel.get("_source", "")))
    preset = dict(gel)
    # T6：probe 不裁切（record 仍是整块胶），但 --keep-lanes 的语法与范围照旧校验。
    if getattr(args, "keep_lanes", None):
        keep_chk = parse_keep(args.keep_lanes, int(args.gel_lanes or preset["lane_count"]))
        print("[i] --keep-lanes 校验通过：%s（probe 不裁切）" % ",".join(map(str, keep_chk)))
    info, ang, ga, lf, bb = run_stages(args.image, preset, args, gel_lanes=args.gel_lanes)
    print("角度: 竖边拟合 %.3f°；本次用 %.3f°" % (info["angle_vertical"], ang))

    # ---- stage 1：胶面冻结（形状由预设决定；不再用条带/泳道反推）
    #       checks 统一放到 stage 3 之后打一行（spec 的打印顺序：1 行 → stage 2 表 → stage 3 表 → checks → record）。
    _print_stage1(ga, preset, args.gel_lanes, show_checks=False)

    # ---- stage 2：先验驱动的泳道格点（只读 stage 1 的冻结输出）
    print("[i] 泳道格点（stage 2）：%d 条，拟合间距 %.2f px（先验 %.2f px），总证据 %.0f" % (
        len(lf["lanes"]), lf["pitch"], lf["pitch_prior"], lf["evidence"]))
    for ln in lf["lanes"]:
        print("  %2d x=%7.2f %-9s evidence=%6.2f 宽=%5.2f" % (
            ln["index"], ln["x_analysis"], "measured" if ln["measured"] else "estimated",
            ln["evidence"], ln["width"]))

    # ---- stage 3：给定冻结的泳道求条带（条带结果不回流到 stage 2）
    print("[i] 条带（stage 3）：胶内中位数 %s；条带行阈值 %.1f（严格）· 泳道阈值 %.1f（仅记录）"
          % (bb["gel_median"], bb["band_thr"], bb["lane_thr"]))
    for i, bands in enumerate(bb["bands"], 1):
        print("  泳道 %2d 条带 %2d 行: %s" % (i, len(bands), [int(b[0]) for b in bands]))

    # ---- 交叉核对汇总（stage 1 + stage 2），然后打印 record（唯一的 in-context JSON）
    _print_checks(list(ga["checks"]) + list(lf["checks"]))
    rec = record_build(preset, ga, lf, bb, info)
    if args.record:
        record_write(rec, args.record)
        print("[i] record 已写 %s" % args.record)
    print(json.dumps(rec, ensure_ascii=False, indent=2))

    if args.debug:
        # 覆盖图要的是分析尺度坐标（draw_debug 会乘回原图），所以另拷一份
        lb_dbg = {"lanes": [(ln["x_analysis"], ln["width"]) for ln in lf["lanes"]],
                  "interp": [not ln["measured"] for ln in lf["lanes"]],
                  "bands": bb["bands"]}
        draw_debug(args.image, info, lb_dbg, args.debug, box_analysis=lf["box_analysis"])
        print("debug 覆盖图:", args.debug, "（红框=按预设定形的胶面，红/橙线=实测/估计泳道，"
              "绿=细栅格探到的条带行，左侧数字=分析尺度 y 标尺；手动标定照这个 y 写 markers.y_px）")
    return 0


def cmd_render(args):
    # T1：预设是强制先验。--gel-cm 只是预设里的宽高覆盖，不能单独替代 --gel-size。
    if not args.gel_size:
        raise RuntimeError("gel preset required (--gel-size FILE)")
    gel = load_gel_size(args.gel_size)
    if args.gel_cm:
        gel = parse_gel_cm(args.gel_cm, gel)

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

    def pt2px(v):
        """pt（物理尺寸，dpi 无关）→ 成品像素。字号/间距用 pt，才不会「改 dpi 就改字与胶的相对大小」。"""
        return float(v) / 72.0 * dpi
    print("[i] 胶体预设 %s：宽 %.2f cm × 高 %.2f cm（%s）；输出 %d dpi" % (
        gel.get("name", "命令行"), gel["width_cm"], gel["height_cm"], gel.get("_source", ""), dpi))

    # ---- 串行管线：stage 1 胶面 → stage 2 泳道 → stage 3 条带（与 probe 共用一个编排）
    preset = dict(gel)
    info, ang, ga, lf, bb = run_stages(args.image, preset, args, gel_lanes=args.gel_lanes)
    (W0, H0), (W1, H1) = load_dims_after(args.image, ang)
    a2o = info["image"][0] / float(info["analysis"][0])
    gel_bb = ga["gel_box"]
    _print_stage1(ga, preset, args.gel_lanes)
    # ---- 探测记录：stage 1–3 的冻结输出一次换算成裁切后胶面的 cm（MW 报告也读它）
    #      rec_full = **整块胶**（所有泳道、未裁切坐标）；stage 4 之后另建 rec（只含保留泳道）。
    rec_full = record_build(preset, ga, lf, bb, info)
    # 真正的写盘放在 MW 之后 —— 手动标定（y_px / y_frac）会覆盖主标定泳道的条带行，
    # 那份覆盖必须也进 record，否则磁盘上的 record 与报告用的数不一致。

    def rot_pt(x, y):
        """原图坐标 → 旋转后画布坐标（与 magick -rotate 同一套矩阵）。"""
        rx, ry = rotate_pt(x, y, W0 / 2.0, H0 / 2.0, ang)
        return rx + (W1 - W0) / 2.0, ry + (H1 - H0) / 2.0

    # ---- stage 2 的冻结输出：画布坐标的泳道 x / 宽（MW、文字都只读这一份）
    lane_x = [ln["x_canvas"] for ln in lf["lanes"]]
    lane_w = [ln["width_canvas"] for ln in lf["lanes"]]
    print("[i] 泳道 %d 条（实测 %d + 估计 %d），间距 %.2f px(分析) ; %s" % (
        len(lane_x), sum(1 for ln in lf["lanes"] if ln["measured"]),
        sum(1 for ln in lf["lanes"] if not ln["measured"]), lf["pitch"],
        font or "magick 默认字体"))

    # ---- stage 3 的冻结输出：条带行（条带永不回流到泳道）
    # lb 仍是出图用的过渡 dict（文字/调试要画布 px）；MW 报告已改吃上面的 record。
    lb = {"lanes": [(ln["x_canvas"], ln["width_canvas"]) for ln in lf["lanes"]],
          "interp": [not ln["measured"] for ln in lf["lanes"]],
          "bands": bb["bands"],
          "pitch": lf["pitch"],
          "row_spacing": bb["row_spacing"],
          "measured": sum(1 for ln in lf["lanes"] if ln["measured"])}
    # debug_overlay 仍用 lb 的 estimated 标志画橙线。
    # 成品 PNG 就是**实际尺寸**：1 px = 1/dpi 英寸。
    # 量纲：px_per_cm 是「画布 px / cm」，所以 px_out/画布px = (px_out/cm) / (画布px/cm) —— **除**。
    cm_px = 1.0 / ga["px_per_cm"]
    scale = (dpi / 2.54) / ga["px_per_cm"]

    if not (1e-4 < scale < 1e3):
        raise RuntimeError("定标算出来不合理（scale=%.6g px/画布px）—— px_per_cm 量纲写反了？"
                           "预设 %.2f cm / 探测宽 %d px / 泳道 %s"
                           % (scale, ga["width_cm"], gel_bb[2] - gel_bb[0], args.gel_lanes or "—"))

    # ---- stage 4：泳道选择（只影响出图；探测、定标、MW 标定仍用整块胶）
    ppc = float(ga["px_per_cm"])
    lane_x_cm = [(x - gel_bb[0]) / ppc for x in lane_x]
    keep = parse_keep(args.keep_lanes, len(lane_x)) if args.keep_lanes else None
    marker_set = {int(v) for v in ((ln or {}).get("marker_lanes") or [])}
    if mk.get("marker_lane") is not None:
        marker_set.add(int(mk["marker_lane"]))
    marker_set = {m for m in marker_set if 1 <= m <= len(lane_x)}
    strips4 = crop = None
    total_w_cm = float(ga["width_cm"])
    if keep is not None:
        if keep == list(range(1, len(lane_x) + 1)):
            print("[i] --keep-lanes 选了全部 %d 条 = 不裁（与全保留逐像素一致）" % len(lane_x))
            keep = None
        else:
            # marker 泳道全被裁掉：脚本不替用户决定，列出来 + 两个选项（spec §6b）
            if not (set(keep) & marker_set):
                if args.marker_policy is None:
                    cut = cropped_out_markers(keep, marker_set)
                    raise RuntimeError(
                        "--keep-lanes %s 会把 marker 泳道 %s 全部裁掉 —— 脚本不替你决定。二选一重跑：\n"
                        "  --marker-policy calibrate-only   marker 只用于定标/分子量，不画 marker 标签\n"
                        "  --marker-policy keep            把离保留泳道最近的 marker 泳道加进选择"
                        % (",".join(map(str, keep)), ", ".join(map(str, cut)) or "（无）"))
                if args.marker_policy == "keep":
                    nm = nearest_marker(marker_set, keep, lane_x_cm)
                    if nm is None:
                        raise RuntimeError("--marker-policy keep：没有可加的 marker 泳道（marker_lanes=%s）"
                                           % sorted(marker_set))
                    keep = sorted(set(keep) | {nm})
                    print("[i] marker-policy keep：加回最近的 marker 泳道 %d → keep=%s"
                          % (nm, ",".join(map(str, keep))))
            strips4 = lane_strips(keep, lane_x_cm, ga["width_cm"])
            strips4, total_w_cm = lane_layout(strips4, args.splice_gap_cm)
            crop = {"keep_lanes": keep,
                    "marker_policy": args.marker_policy,
                    "splice_gap_cm": float(args.splice_gap_cm) if len(strips4) >= 2 else 0.0,
                    "strips": strips4}
            print("[i] 泳道选择（stage 4）：保留 %s → %d 段；成品胶面宽 %.3f cm（gap %.2f cm）" % (
                ",".join(map(str, keep)), len(strips4), total_w_cm, crop["splice_gap_cm"]))
            for s in strips4:
                print("    段 %s：src %.3f–%.3f cm（宽 %.3f）→ dst %.3f cm" % (
                    ",".join(map(str, s["lanes"])), s["src_x0_cm"], s["src_x1_cm"],
                    s["src_x1_cm"] - s["src_x0_cm"], s["dst_x0_cm"]))
            if len(strips4) >= 2:
                print("[!] 拼接：非相邻泳道由同一块胶拼接而成 —— 图注必须声明（保留原始泳道号）")

    strip_of = {}
    for s in (strips4 or []):
        for k in s["lanes"]:
            strip_of[int(k)] = s

    def lane_out_cm(k, x_canvas):
        """某条（原始泳道号, 画布 x）→ 成品版式 cm；该道被裁掉返回 None。"""
        s = strip_of.get(int(k))
        if s is None:
            return None
        cm = (x_canvas - gel_bb[0]) / ppc
        return s["dst_x0_cm"] + (cm - s["src_x0_cm"])

    def x_out_cm(x_canvas):
        """任意画布 x → 成品版式 cm；落在所有保留段之外返回 None（marker 列可能被裁掉）。"""
        if strips4 is None:
            return (x_canvas - gel_bb[0]) / ppc
        cm = (x_canvas - gel_bb[0]) / ppc
        for s in strips4:
            if s["src_x0_cm"] - 1e-9 <= cm <= s["src_x1_cm"] + 1e-9:
                return s["dst_x0_cm"] + (cm - s["src_x0_cm"])
        return None

    def to_face_x(cm):
        """成品版式 cm → 画布坐标口径（后段仍按原来的 L+(ax-gx0)·sx 换算）。"""
        return gel_bb[0] + float(cm) * ppc

    face_bb = (gel_bb[0], gel_bb[1], gel_bb[0] + total_w_cm * ppc, gel_bb[3]) \
        if strips4 else gel_bb
    rec = _crop_record(rec_full, keep, crop, strips4, total_w_cm) if strips4 else rec_full

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
        for i, (e, x) in enumerate(zip(ln_labels, xs), 1):
            en = as_entry(e, ln)
            t = en.get("text", "")
            if not t:
                continue
            if strips4 is not None:
                if i not in set(keep or ()):
                    continue                  # 被裁掉的泳道不画名字
                cm = lane_out_cm(i, x)
                if cm is None:
                    continue
                x = to_face_x(cm)             # x 映射到拼接版式（原始号 → 原标签）
            s = pt2px(en.get("size", size))
            tw, th = text_extent(font, round(s), t)
            tw, th = tw / scale, th / scale
            ax = x - tw / 2.0 if align == "center" else (x if align == "left" else x - tw)
            # 口径：box **底边**落在胶顶之上 gap（
            # 而 -annotate 的 y 是 box **顶边**，所以这里减掉整个 box 高）。
            ay = gel_bb[1] - gap / scale - th
            items.append({"ax": ax, "ay": ay, "box_w": tw, "box_h": th,
                          "text": t, "size_px": s, "size_pt": float(en.get("size", size)),
                          "color": en.get("color", color), "role": "lane",
                          "bind": {"lane": i},
                          "anchor": {"center": "bottom-center", "left": "bottom-left",
                                     "right": "bottom-right"}[align]})

    # ---- marker 分子量（marker_lane / marker_lane_x / lane_width /
    #      lane_edge=inner / gap = 文字与泳道**带边**的间距；左右按泳道区中线判）
    rows, idx, span = [], 0, []          # 只有 mk_labels 时才会被真正赋值；先初始化，后面 record 覆盖要用
    if mk_labels:
        lw = float(mk["lane_width"]) if mk.get("lane_width") else lane_pitch(lane_x)
        if mk.get("marker_lane_x") is not None:
            mx = pos_or_frac(mk["marker_lane_x"], gel_bb[0], gel_bb[2])
            m_lane = int(mk["marker_lane"]) if mk.get("marker_lane") is not None else None
        else:
            idx = int(mk.get("marker_lane", 1))
            if idx == 0:
                mx = lane_x[0] - lw
            elif 1 <= idx <= len(lane_x):
                mx = lane_x[idx - 1]
            else:
                raise RuntimeError("markers 的 marker_lane 超出泳道数量（探到 %d 条）" % len(lane_x))
            m_lane = idx if idx >= 1 else None
        # stage 4：marker 标签的 x 也要映射；calibrate-only 或 marker 泳道被裁掉时不画。
        left_side, edge = True, gel_bb[0]
        draw_marker = True
        if strips4 is not None:
            if args.marker_policy == "calibrate-only":
                draw_marker = False
                print("[i] marker-policy calibrate-only：不画 marker 标签（MW 标定仍用全部 marker 泳道）")
            else:
                mo = lane_out_cm(m_lane, mx) if m_lane is not None else x_out_cm(mx)
                if mo is None:
                    draw_marker = False
                    print("[i] marker 泳道不在保留段里：不画 marker 标签（标定/MW 不受影响）")
                else:
                    mx = to_face_x(mo)
                    mid = to_face_x(total_w_cm / 2.0)
                    left_side = mo < total_w_cm / 2.0
                    edge = mx if mk.get("lane_edge") == "inner" else (
                        mx - lw / 2.0 if left_side else mx + lw / 2.0)
                    edge = min(edge, to_face_x(0.0)) if left_side else max(edge, to_face_x(total_w_cm))
        if strips4 is None:
            mid = (min(lane_x) + max(lane_x)) / 2.0
            left_side = mx < mid
            edge = mx if mk.get("lane_edge") == "inner" else (
                mx - lw / 2.0 if left_side else mx + lw / 2.0)
        # 整列**不压在胶上**：按泳道带边算出的锚点若还在胶面内，就推到胶面之外（gap 从胶边起算）。
        # 左/右按（泳道区中线），只是把这一列挪进白底留白里。
        if strips4 is None:
            edge = min(edge, gel_bb[0]) if left_side else max(edge, gel_bb[2])
        gap = pt2px(mk.get("gap", 4))
        size = float(mk.get("size", 8))                # pt
        if strips4 is None:
            print("[i] 泳道带宽 ≈%.1f px(分析)（%s）；marker 泳道 x=%.1f（泳道区中线 %.1f 的%s边）"
                  "→ 带边 %.1f → %s对齐，锚点离带边 %g px（成品像素）" % (
                      lw / a2o, "来自 markers.lane_width" if mk.get("lane_width") else
                      "相邻泳道中心距的中位数", mx, mid, "左" if left_side else "右", edge,
                      "右" if left_side else "左", float(mk.get("gap", 4))))
        else:
            if draw_marker:
                print("[i] marker 列：成品版式带边 %.3f cm（胶面宽 %.3f cm）→ %s对齐" % (
                    (edge - gel_bb[0]) / ppc, total_w_cm, "右" if left_side else "左"))
            else:
                print("[i] marker 列：本轮不画（成品胶面宽 %.3f cm）" % total_w_cm)
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
            mm_apx = ga["width_cm"] * 10.0 / max(1e-6, (gel_bb[2] - gel_bb[0]) / a2o)   # 分析 px → mm
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
            gy0, gy1 = lf["box_analysis"][1], lf["box_analysis"][3]
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
        for j, (row, e) in enumerate(zip(eff, mk_labels) if draw_marker else [], 1):
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
            items.append({"ax": ax, "ay": ay, "box_w": tw, "box_h": th,
                          "text": t, "size_px": s, "size_pt": float(en.get("size", size)),
                          "color": en.get("color", mk.get("color", "#000000")),
                          "role": "marker",
                          "bind": {"marker_lane": idx + 1, "label_index": j,
                                   "band_y_cm": round((yy - gel_bb[1]) / ppc, 4)},
                          "anchor": "middle-right" if left_side else "middle-left"})

    # ---- 外扩留白（按文字真实包围盒）→ 裁切 → 写字
    fb = face_bb if strips4 is not None else gel_bb
    base_m = args.margin_frac * max(fb[2] - fb[0], fb[3] - fb[1])
    pad = max(6.0, base_m * 0.4)
    need = {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0}
    for it in items:
        ax, ay, tw, th = it["ax"], it["ay"], it["box_w"], it["box_h"]
        need["l"] = max(need["l"], fb[0] - ax)
        need["r"] = max(need["r"], ax + tw - fb[2])
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
    out_ppc = dpi / 2.54

    tmp_face = None
    if strips4 is None:
        # 全保留：与 T5 逐像素一致（不建临时文件、不加白缝）
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
        out_items = [(L + (it["ax"] - gx0) * sx, T + (it["ay"] - gy0) * sy,
                      it["text"], it["size_px"], it["color"]) for it in items]
        face_w_px, face_h_px = gw, gh
    else:
        # ---- stage 4：先落一张**整块胶面**（crop → resize，与全保留同一条命令），再按段水平裁切拼接。
        #      每段只做 -crop + composite：不拉伸、不上下位移、不调色。
        sx = out_ppc / ppc                          # 画布 px → 成品 px（版式 cm 用物理换算）
        L, R = int(round(L * sx)), int(round(R * sx))
        T, B = int(round(T * sy)), int(round(B * sy))
        face_w_px = int(round(total_w_cm * out_ppc))
        face_h_px = gh
        W, H = L + face_w_px + R, gh + T + B
        fd, tmp_face = tempfile.mkstemp(prefix="gel-face-", suffix=".png")
        os.close(fd)
        try:
            face = [MAGICK, args.image, "-auto-orient"]
            if abs(ang) > 1e-6:
                face += ["-rotate", "%.4f" % ang]
            face += ["-crop", "%dx%d+%d+%d" % (gx1 - gx0, gy1 - gy0, gx0, gy0), "+repage",
                     "-resize", "%dx%d!" % (gw, gh), "+profile", "*", tmp_face]
            _run(face)

            src_ppc = gw / float(ga["width_cm"])     # 整块胶面临时图的 px/cm
            base = [MAGICK, "-size", "%dx%d" % (W, H), "xc:white"]
            placed = []
            for s in strips4:
                w = int(round((s["src_x1_cm"] - s["src_x0_cm"]) * out_ppc))
                xo = L + int(round(s["dst_x0_cm"] * out_ppc))
                xs = int(round(s["src_x0_cm"] * src_ppc))
                xs = max(0, min(xs, gw - w))         # 亚像素取整的兜底：只挪 ≤1 px，不缩放
                base += ["(", tmp_face, "-crop", "%dx%d+%d+0" % (w, gh, xs), "+repage", ")",
                         "-gravity", "northwest", "-geometry", "+%d+%d" % (xo, T), "-composite"]
                placed.append((xo, w))
            if len(placed) >= 2:
                # 缝正中一条 1 px 黑线（只在胶面高度内）；只有一段时没有缝、没有线。
                draws = []
                for (x0, w0), (x1, _w1) in zip(placed, placed[1:]):
                    xm = (x0 + w0 + x1) // 2
                    draws.append("line %d,%d %d,%d" % (xm, T, xm, T + gh - 1))
                base += ["-fill", "black", "-stroke", "none",
                         "-draw", " ".join(draws)]
            out_items = [(L + (it["ax"] - gx0) * sx, T + (it["ay"] - gy0) * sy,
                          it["text"], it["size_px"], it["color"]) for it in items]
            # 验收用：每段在成品里的实际像素宽（cm = px / (dpi/2.54)）
            print("[i] 拼接版式：%d 段，成品胶面 %d×%d px（%.3f×%.3f cm）" % (
                len(placed), face_w_px, face_h_px, face_w_px / out_ppc, face_h_px / out_ppc))
            gaps = [b[0] - (a[0] + a[1]) for a, b in zip(placed, placed[1:])]
            print("    段宽 px：%s；缝 %s px%s" % (
                ", ".join(str(w) for _x, w in placed),
                "/".join(str(g) for g in gaps) if gaps else "0",
                "（缝心 1 px 黑线）" if len(placed) >= 2 else "（无白缝）"))
        except Exception:
            try:
                os.unlink(tmp_face)
            except OSError:
                pass
            raise

    # ---- 胶面 PNG（无字，spec §3）：直接取成品画布的 gel 区域 —— 同一批 magick 操作
    #      （裁切/正畸/拼接/缝线），只是不加字，所以它必然与成品里的胶面区域逐像素一致。
    stem = os.path.splitext(args.out)[0]
    gel_path = stem + ".gel.png"
    scene_path = stem + ".scene.json"
    gel_cmd = list(base) + ["-crop", "%dx%d+%d+%d" % (face_w_px, face_h_px, L, T), "+repage",
                           "-units", "PixelsPerInch", "-density", str(dpi), gel_path]
    _run(gel_cmd)

    # ---- 版式冻结成数据（spec §4）：先建 scene，再交给唯一的 PNG writer 画字。
    #      锚点 cm 仍按**成品 px** 反算（out_items / out_origin=(L,T)），
    #      这样 writer 从 cm 回到 px 时不会出现亚像素漂移（硬要求：fig.png 逐像素不变）。
    scene = scene_build(items, face_bb, ppc, dpi=dpi, font=font,
                        outline=None if args.no_outline else args.outline,
                        margin_frac=args.margin_frac,
                        gel_image=os.path.basename(gel_path),
                        record=args.record, written=date.today().isoformat(),
                        face_px=(face_w_px, face_h_px), out_items=out_items,
                        out_origin=(L, T))
    scene_write(scene, scene_path)
    print("[i] scene 已写 %s；胶面图 %s" % (scene_path, gel_path))

    if args.svg:
        # spec §3/§5：--svg 时在同一 stem 再写一份独立 SVG（胶面已内嵌，不引用外部文件）。
        svg_path = stem + ".svg"
        si = scene_svg_write(scene, svg_path, gel_path=gel_path, dpi=dpi)
        print("[i] SVG 已写 %s（%d 条文字；页面 %.2f×%.2f cm；%.1f KB）" % (
            svg_path, si["n_items"], si["page_cm"][0], si["page_cm"][1], si["bytes"] / 1024.0))

    # 编辑器（spec §3/§6）：自包含 HTML，胶面 base64 + scene JSON。批量跑可 --no-editor 关掉。
    if not args.no_editor:
        html_path = stem + ".html"
        editor_write_html(EDITOR_TEMPLATE, scene, gel_path, html_path)
        print("[i] 编辑器已写 %s（离线可编辑；--no-editor 可关）" % html_path)

    # ---- 成品 PNG：只读 scene（页面/胶面/每条文字都从版式数据来）
    res = scene_png_write(scene, args.out, gel_path=gel_path, font_path=font)
    W, H, L, T = res["width_px"], res["height_px"], res["left_px"], res["top_px"]
    face_w_px, face_h_px = res["face_w_px"], res["face_h_px"]

    if tmp_face:
        try:
            os.unlink(tmp_face)
        except OSError:
            pass

    if args.debug:
        for ox, oy, t, s, c in res["out_items"]:
            print("    [item] %-8s x0=%7.1f y0=%7.1f (pt=%.1f)" % (t, ox, oy, s))
    # 手动标定（y_px / y_frac）的行必须进 record：MW 只读 record，
    # 否则人会以为标定生效了、报告其实用的自动行。--no-mw 时也要写对。
    if mk_labels and (mk.get("y_px") or mk.get("y_frac")) and rows:
        new_rows = record_bands_from_rows(lf["lanes"][idx]["x_analysis"], rows,
                                          bb["bands"][idx], ga, info)
        rec_full["bands"][str(idx + 1)] = new_rows
        if strips4 is not None and str(idx + 1) in rec["bands"]:
            rec["bands"][str(idx + 1)] = new_rows
    if ln_labels and mk_labels and not args.no_mw and len(rows) == sum(span):
        mset = {int(k) - 1 for k in (ln or {}).get("marker_lanes", [])} | {idx}
        # 标定用**整块胶**的 record（marker 泳道可能已被裁掉）；报样品只报保留泳道。
        rep = mw_report_build(rec_full, [as_entry(e, ln).get("text", "") for e in ln_labels],
                              mset, idx, mk_labels, span, as_entry, args.mw_method)
        if "marker_lanes" not in (ln or {}):
            rep["notes"].append("lanes.json 没有 marker_lanes：其余 marker 泳道会被当成样品报出来")
        if strips4 is not None:
            kept = set(keep or ())
            rep["lanes"] = [l for l in rep["lanes"] if int(l["lane"]) in kept]

            def _note_lane(n):
                m = re.match(r"泳道 (\d+)（", n)
                return int(m.group(1)) if m else None
            rep["notes"] = [n for n in rep["notes"]
                            if _note_lane(n) is None or _note_lane(n) in kept]
        print(format_report(rep))
        mw_path = os.path.splitext(args.out)[0] + ".mw.json"
        with open(mw_path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print("[MW] 已写 %s" % mw_path)
    if args.record:
        record_write(rec, args.record)
        print("[i] record 已写 %s" % args.record)
    if args.debug:
        # draw_debug 吃分析尺度坐标；lb 是画布坐标（出图/调试用），这里另拷一份。
        lb_dbg = dict(lb, lanes=[(ln["x_analysis"], ln["width"]) for ln in lf["lanes"]])
        draw_debug(args.image, info, lb_dbg, args.debug, cfg_rows=rows if mk_labels else None,
                   box_analysis=lf["box_analysis"])
        print("debug 覆盖图:", args.debug, "（红框=按预设定形的胶面，红/绿线=泳道/条带行，品红=配置指定的 marker 行）")
    R, B = W - L - face_w_px, H - T - face_h_px
    print("输出 %s（%d×%d px, %d dpi；%d 条文字）= 胶面 %d×%d + 白底留白 L%d R%d T%d B%d" % (
        args.out, W, H, dpi, res["items"], face_w_px, face_h_px, L, R, T, B))
    print("[i] 实际尺寸：胶面 %.2f×%.2f cm（%d×%d px）· 成品 %.2f×%.2f cm（含白底留白）" % (
        face_w_px / dpi * 2.54, face_h_px / dpi * 2.54, face_w_px, face_h_px,
        W / dpi * 2.54, H / dpi * 2.54))
    return 0


def cmd_export(args):
    """scene JSON 或编辑器 HTML → PNG / SVG / HTML（只读 scene，不跑检测；spec §7）。

    输入两种：`render` 写的 <out>.scene.json，或编辑器存下的 <out>.html
    （scene 从 #scene 读、胶面从 #gel 的 data URI 取）。两种都只走 scene_png /
    scene_svg / editor 三个 writer，不碰照片、不跑检测。
    输出：`--png` / `--svg` / `--html` 任意组合（至少给一个）。
    `--dpi` 只覆盖本次，不写回 scene 文件。
    """
    if not (args.png or args.svg or args.html):
        raise RuntimeError("export 需要 --png / --svg / --html 至少一个输出路径")
    tmpdir = None
    if re.search(r"\.html?$", args.scene, re.I):
        # 编辑器存下的 HTML：版式在 #scene，胶面在 #gel 的 data URI 里（spec §6/§7）。
        sc, gel_bytes = editor_extract(args.scene)
        tmpdir = tempfile.mkdtemp(prefix="ep-export-")
        name = os.path.basename(sc["gel"].get("image") or "gel.png")
        gel_path = os.path.join(tmpdir, name)
        with open(gel_path, "wb") as f:
            f.write(gel_bytes)
        sc["gel"]["image"] = gel_path
    else:
        sc = scene_load(args.scene)
        gel_path = sc["gel"]["image"]
        if not os.path.isabs(gel_path):
            gel_path = os.path.join(os.path.dirname(os.path.abspath(args.scene)), gel_path)
        if not os.path.isfile(gel_path):
            raise RuntimeError("scene 指向的胶面图不存在：%s" % gel_path)
    dpi = int(args.dpi or sc["output"]["dpi"])

    try:
        if args.svg:
            si = scene_svg_write(sc, args.svg, gel_path=gel_path, dpi=dpi)
            print("[i] SVG 已写 %s（%d 条文字；页面 %.2f×%.2f cm；%.1f KB）" % (
                args.svg, si["n_items"], si["page_cm"][0], si["page_cm"][1], si["bytes"] / 1024.0))
        if args.html:
            editor_write_html(EDITOR_TEMPLATE, sc, gel_path, args.html)
            print("[i] 编辑器已写 %s（离线可编辑）" % args.html)
        if not args.png:
            return 0

        res = scene_png_write(sc, args.png, dpi=dpi, gel_path=gel_path)
        R = res["width_px"] - res["left_px"] - res["face_w_px"]
        B = res["height_px"] - res["top_px"] - res["face_h_px"]
        print("输出 %s（%d×%d px, %d dpi；%d 条文字）= 胶面 %d×%d + 白底留白 L%d R%d T%d B%d" % (
            args.png, res["width_px"], res["height_px"], res["dpi"], res["items"],
            res["face_w_px"], res["face_h_px"], res["left_px"], R, res["top_px"], B))
        return 0
    finally:
        if tmpdir:
            for n in os.listdir(tmpdir):
                try:
                    os.unlink(os.path.join(tmpdir, n))
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


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
    p.add_argument("--gel-size", help="胶体尺寸 JSON（必须给；如 data/gel_size.json）")
    p.add_argument("--gel-lanes", type=int, default=None,
                   help="照片是裁过的、只含 N 条泳道时给 N：改按--gel-size 里的 lane_pitch_cm 的泳道间距定标")
    p.add_argument("--record", help="把探测记录（裁切后胶面的 cm 坐标 JSON）写到这个路径")
    p.add_argument("--keep-lanes", help="校验泳道选择语法（如 1,2,8-10）；probe 不裁切，record 仍是整块胶")
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
    r.add_argument("--gel-size", help="胶体尺寸 JSON（必须给；如 data/gel_size.json）")
    r.add_argument("--gel-cm", help="覆盖 --gel-size 预设里的宽x高（cm），如 8.6x6.25；不能单独使用")
    r.add_argument("--gel-lanes", type=int, default=None,
                   help="照片是裁过的、只含 N 条泳道时给 N：改按--gel-size 里的 lane_pitch_cm 的泳道间距定标")
    r.add_argument("--record", help="把探测记录（裁切后胶面的 cm 坐标 JSON）写到这个路径")
    r.add_argument("--keep-lanes", help="只保留这些原始泳道号（1 起，逗号/区间：1,2,8-10）；省略 = 全保留")
    r.add_argument("--marker-policy", choices=("calibrate-only", "keep"),
                   help="keep 的泳道里没有 marker 泳道时怎么办：calibrate-only=只用它定标不画标签 / keep=把最近的 marker 泳道加进来")
    r.add_argument("--splice-gap-cm", type=float, default=0.15,
                   help="拼接时段与段之间的白缝宽度（cm，默认 0.15；只有一段时没有缝）")
    r.add_argument("--trim-top-cm", type=float, default=None,
                   help="从胶面上缘再裁掉多少 cm（胶面上缘已是孔底，一般不用；默认读--gel-size 里的 trim_top_cm）")
    r.add_argument("--trim-bottom-cm", type=float, default=None,
                   help="从胶面下缘再裁掉多少 cm（默认读--gel-size 里的 trim_bottom_cm）")
    r.add_argument("--angle", type=float, default=None)
    r.add_argument("--no-rotate", action="store_true")
    r.add_argument("--margin-frac", type=float, default=0.05, help="四周外扩（占胶面长边比例，默认 0.05）")
    r.add_argument("--font", help="字体文件路径（默认 Arial；配置里的 font 只在是路径时才用）")
    r.add_argument("--outline", default=None,
                   help="文字白边颜色（默认不描边：成品是白底；要压在胶上时才给 white）")
    r.add_argument("--no-outline", action="store_true", help="不要白边")
    r.add_argument("--no-editor", action="store_true",
                   help="不写 <out>.html 编辑器（批量出图时用；spec §3）")
    r.add_argument("--mw-method", choices=("logfit", "interp"), default="logfit",
                   help="样品带分子量的标定方法：logfit=log10(MW)~y 直线（会外推）/ interp=marker 带之间内插（不外推）")
    r.add_argument("--no-mw", action="store_true", help="不输出分子量报告")
    r.add_argument("--debug", help="再出一张检测覆盖图")
    r.add_argument("--svg", action="store_true",
                   help="同时写 <out 同 stem>.svg（矢量；胶面内嵌，spec §5）")
    add_detect(r)
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("selftest")
    s.set_defaults(func=lambda a: selftest())

    e = sub.add_parser("export", help="从 scene JSON / 编辑器 HTML 出 PNG / SVG / HTML（不跑检测）")
    e.add_argument("scene", help="render 写出的 <out>.scene.json，或编辑器存下的 <out>.html")
    e.add_argument("--png", help="输出 PNG 路径")
    e.add_argument("--svg", help="输出 SVG 路径（可与 --png 同时给，也可只给 --svg）")
    e.add_argument("--html", help="输出编辑器 HTML 路径（spec §7）")
    e.add_argument("--dpi", type=int, default=None, help="覆盖 scene 里的 output.dpi")
    e.set_defaults(func=cmd_export)

    d = sub.add_parser("doctor", help="环境自检：python / magick / 字体 / 数据文件")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        # 一行错误、非 0 退出，不吐 traceback（spec §8：缺预设时的消息走这里）
        print("[!] %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
