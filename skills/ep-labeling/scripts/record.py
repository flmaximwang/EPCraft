#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""record：stage 1–3 的内部输出 → 探测记录（裁切后胶面的 cm 坐标）。

record 是「这张图是怎么来的」的唯一凭据（spec §7 / conventions §6）：

    - 原点 = 裁切后胶面（旋转、按预设裁切、扣掉 trims 之后那个面）的左上角；
    - x 向右、y 向下，单位 cm（物理量，与 dpi 无关）；
    - 不存照片坐标、gel_box、倾角、分析尺度 —— 需要原图像素才能解释的数一律不进 record。

stage 1–3 内部仍用照片/画布像素；本模块只在最后一步做一次换算：

    x_cm = (x_canvas - gel_box[0]) / px_per_cm
    y_cm = (y_canvas - gel_box[1]) / px_per_cm

条带的 y 在 stage 3 里是**分析尺度**，先经 geometry.analysis_to_canvas() 换成画布坐标
（旋转后 y 依赖 x，所以带上该泳道的 x 一起转）；条带 width 是分析尺度的竖直半高宽，
旋转不改变长度，乘 a2y 换成画布 px 再除 px_per_cm。

本模块不 import 任何 stage 模块，只依赖 geometry（底层几何）。
"""
import json

from geometry import analysis_to_canvas


def _cm(v, origin, px_per_cm):
    """画布 px → 以裁切后胶面左上角为原点的 cm。"""
    return (float(v) - float(origin)) / float(px_per_cm)


def build(preset, ga, lf, bb, info, crop=None):
    """把 stage 1–3 的内部输出（照片/画布 px）换成 record（裁切后胶面的 cm 坐标）。

    preset = 本次实际用的预设 dict（render 里可能已被 --gel-cm / trim 覆盖）；
    ga     = gel_area.compute() 的冻结输出；
    lf     = lane_fit.find_lanes() 的冻结输出；
    bb     = band_fit.find_bands() 的冻结输出（bands 与分析尺度的泳道 x 一一对应）；
    info   = detect_gel() 的返回（只用 image / analysis 的比例）；
    crop   = T6 的裁切/拼接结构；本轮不传，也就不会出现 crop 键。
    """
    ppc = float(ga["px_per_cm"])
    gx0, gy0 = float(ga["gel_box"][0]), float(ga["gel_box"][1])
    canvas = ga.get("canvas")
    angle = float(ga["angle"])
    a2y = info["image"][1] / float(info["analysis"][1])   # 分析尺度 px → 画布 px（旋转不改长度）

    lanes = []
    bands = {}
    for ln, bs in zip(lf["lanes"], bb["bands"]):
        index = int(ln["index"])
        lanes.append({
            "index": index,
            "x_cm": round(_cm(ln["x_canvas"], gx0, ppc), 4),
            "width_cm": round(float(ln["width_canvas"]) / ppc, 4),
            "estimated": not bool(ln["measured"]),
            "evidence": round(float(ln["evidence"]), 2),
        })
        rows = []
        for y_analysis, width_analysis, depth in bs:
            # 该带的 y 必须和该泳道的 x 一起转：旋转后画布的 y 不再是只由分析 y 决定。
            _, y_canvas = analysis_to_canvas(float(ln["x_analysis"]), float(y_analysis),
                                             angle, info, canvas)
            rows.append({
                "y_cm": round(_cm(y_canvas, gy0, ppc), 4),
                "width_cm": round(float(width_analysis) * a2y / ppc, 4),
                "depth": round(float(depth), 2),
            })
        bands[str(index)] = rows

    rec = {
        "preset": {
            "name": preset.get("name", ""),
            "width_cm": float(preset["width_cm"]),
            "height_cm": float(preset["height_cm"]),
            "lane_count": int(preset["lane_count"]),
            "lane_pitch_cm": float(preset["lane_pitch_cm"]),
            "trim_top_cm": float(preset.get("trim_top_cm") or 0.0),
            "trim_bottom_cm": float(preset.get("trim_bottom_cm") or 0.0),
            "anchor": preset.get("anchor") or "bottom",
        },
        # 实际产出的胶面：现在就是 stage 1 定出来的面（T6 裁/拼之后改这里）。
        "gel": {"width_cm": round(float(ga["width_cm"]), 4),
                "height_cm": round(float(ga["height_cm"]), 4)},
        # stage 1/2 的交叉核对（数值 + pass/warn），顺序即执行顺序。
        "checks": [dict(c) for c in ga["checks"]] + [dict(c) for c in lf["checks"]],
        "lanes": lanes,
        "bands": bands,
    }
    if crop:
        rec["crop"] = crop
    return rec


def bands_from_rows(lane_x_analysis, rows, detected, ga, info):
    """把**手工标定**的条带行（markers.y_px / y_frac，分析尺度 y）写成 record 的条带。

    为什么要单独一条路：record 的 bands 来自 stage 3 的自动探测，而手动标定是**人对着
    覆盖图读出来的**，两者会差 ~0.7 mm（自动行系统性偏上）。MW 报告现在只吃 record，
    所以手动标定的行必须写进 record，否则报告会悄悄退回自动行 —— 标定就白做了。
    宽度/深度借最近的自动带（手动只改 y）。
    """
    ppc = float(ga["px_per_cm"])
    gy0 = float(ga["gel_box"][1])
    a2y = info["image"][1] / float(info["analysis"][1])
    out = []
    for y in rows:
        _, y_canvas = analysis_to_canvas(float(lane_x_analysis), float(y),
                                         float(ga["angle"]), info, ga.get("canvas"))
        near = min(detected, key=lambda b: abs(float(b[0]) - float(y))) if detected else None
        out.append({
            "y_cm": round(_cm(y_canvas, gy0, ppc), 4),
            "width_cm": round(float(near[1]) * a2y / ppc, 4) if near else 0.0,
            "depth": round(float(near[2]), 2) if near else 0.0,
        })
    return out


def write(rec, path):
    """写 record：UTF-8、不转义中文、缩进 2、末尾换行（文件 diff 才干净）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load(path):
    """读回 record（测试与下游工具用）。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)
