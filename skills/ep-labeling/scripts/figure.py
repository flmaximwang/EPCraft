#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Figure 与 Profiles 两块画板、整份文档的生成，以及 sync 的增量计划（spec §4、§7、§9）。

一份「目标」描述，两个写入口：
    targets()        模型 + 报告 + 当前标签文字 → 全部 agent 管的对象的规格（spec dict 列表）+ 画板矩形
    build_document() render：新文档，Workbench（用户改的线）+ targets() 的全部对象，一次写成
    plan_sync()      sync：活文档（document.json）+ 新模型 → 最少的操作列表（ops）
    apply_ops_json() 把 ops 落到文档 JSON 上（selftest 用来验 plan_sync；离线也能用）
V7 再把同一套 ops 翻成控制口命令。render 与 sync 因此不会各算各的。

所有权（spec §4.4，plan_sync 的硬规则）：
    标签（labels 层里 `label lane n` / `label band <MW>`）：agent 只动位置、改名、新建、删除，
        **从不写文字与样式**（文字、字体、字号、颜色归用户）；labels 层里别的名字的对象一律不碰；
    analysis / profiles 两层：每次整层重建；
    gel 层：Figure 上的 `gel seg k`（剪切组）由 agent 管；Workbench 那张 `gel` 图不动；
    Workbench 的线（lanes / trimmed / window / marker bands）归用户，agent 只给 trimmed 里的 trim 标记换灰色、
        lanes 里的分隔线换回青色；
    ep-meta：每次改写。

坐标：pt，y 向下。Workbench 画板在 (0, 0) 起，Figure 画板在它**下面**（这样 Figure 变宽变高不会压到
Workbench），Profiles 在 Workbench 右边。画板顺序固定：0 = Figure（导出 fig.png 的那块）、1 = Workbench、
2 = Profiles。
"""
import copy
import json

import vc_file
import workbench
from lane_select import layout as strips_layout, strips as lane_strips
from text_layout import CAP as TEXT_CAP, box_offsets, anchor_for, pick_font, text_extent, window_deg


PT = vc_file.PT_PER_CM
MARGIN_MIN_CM = 0.43     # 页面最小留白（旧口径 margin_frac 0.05 × 胶面长边 8.6 cm）
PAD_CM = 0.17            # 文字越界之外再留的白（旧口径 margin × 0.4）
SPLICE_GAP_CM = 0.15     # 不连续的保留段之间留白（拼缝披露，gel-prior §6b；与 --splice-gap-cm 默认一致）
LANE_SIZE, LANE_GAP_PT = 8.0, 4.0
MARKER_SIZE, MARKER_GAP_PT = 8.0, 4.0
CAP = TEXT_CAP           # Arial 大写字高 / 字号（text_layout.CAP 的别名，测试与排字用）
BOARD_GAP = 60.0         # 画板之间的间隔（pt）
GREY = (0.6, 0.6, 0.6)
UNNAMED = "?"            # 新泳道（用户加的线）的占位名：agent 去问名字；读回时仍算「没有名字」

# Profiles 两种版式（V-D3，待用户选）：
#   stack  每道一行横向曲线，横轴 = 胶上的 y（上方标 MW 刻度），行高 ROW_PT
#   strips 每道一条竖向曲线，与胶同比例（纵轴就是胶上的 y），按泳道顺序并排
PROFILE_LAYOUT = "stack"
ROW_PT, PLOT_W_PT, LABEL_W_PT = 34.0, 320.0, 46.0
STRIP_W_PT = 30.0

_MEASURE = {}


def measure(text, size_pt):
    """文字框宽高（pt）：magick 在 100 pt 下量一次再按字号缩放（与 scene 的 ink box 同一来源）。"""
    key = text
    if key not in _MEASURE:
        _MEASURE[key] = text_extent(pick_font(), 100, text) if text else (0, 0)
    w, h = _MEASURE[key]
    return w * size_pt / 100.0, h * size_pt / 100.0


# ---------------------------------------------------------------- 目标

def targets(model, report, gel_key, gel_px, wb, labels=None, layout=None, measure_fn=None):
    """→ {"artboards": {"Figure": rect, "Profiles": rect}, "specs": [对象规格…]}。

    model  = workbench 模型（cm）；report = profiles.analyze 的报告（曲线、叫出的带、标定）；
    gel_key/gel_px = 文档里胶面图的 key 与 (宽, 高) px；wb = Workbench 画板与胶面位置
        {"artboard": (x, y, w, h), "gel_x", "gel_y"}（render 用 workbench.frame，sync 从文档读）；
    labels = 现有标签的文字与字号 {名字: (text, size)}：页边距按**当前**文字量（用户改长了，页就长）；
        省略 = 用默认文字（render）。
    """
    measure_fn = measure_fn or measure
    layout = layout or PROFILE_LAYOUT
    labels = labels or {}
    specs = []
    W, H = model["gel"]["width_cm"], model["gel"]["height_cm"]
    lanes = model["lanes"]
    cs = workbench.centres(model)
    kept = [i for i, ln in enumerate(lanes, 1) if not ln["trimmed"]]
    if not kept:
        raise workbench.WorkbenchError("所有泳道都在 trimmed 里 —— Figure 上至少要留一条")

    # ---- Figure：保留段（中点切）、拼缝留白
    segs, gel_w_cm = strips_layout(lane_strips(kept, cs, W), SPLICE_GAP_CM)

    def fig_x_cm(lane):
        x = cs[lane - 1]
        for sg in segs:
            if lane in sg["lanes"]:
                return sg["dst_x0_cm"] + (x - sg["src_x0_cm"])
        return None

    meta = model["meta"]
    names = meta.get("lane_names") or []
    defaults = meta.get("label_angle") or {}
    lane_default = float(defaults.get("lane", 0.0) or 0.0)
    marker_default = float(defaults.get("marker", 0.0) or 0.0)
    lane_angles = meta.get("lane_angles") or []
    lane_items = []                                      # {name, text, size, angle, x_cm, box}
    for i in kept:
        nm = "label lane %d" % i
        cur = labels.get(nm)
        if cur is not None:
            text, size = cur["text"], cur["size"]
        else:
            default = names[i - 1] if i - 1 < len(names) else None
            if default == "":
                continue                                 # 空道：不写名字（与 lanes.json 的 "" 同义）
            text, size = (UNNAMED if default is None else default), LANE_SIZE
        fallback = lane_angles[i - 1] if i - 1 < len(lane_angles) else lane_default
        angle = _label_angle(cur, fallback)
        lane_items.append({"name": nm, "text": text, "size": size, "angle": angle, "x_cm": fig_x_cm(i),
                           "justify": "Center",
                           "box": box_offsets(measure_fn(text, size)[0], size, angle, "Center")})

    ml = model["marker_lane"]
    marker_items = []                                    # {name, text, size, angle, y_cm, rgb}
    if not lanes[ml - 1]["trimmed"]:
        by_mw = {b["mw"]: b["y_cm"] for b in model["bands"]}
        for e in meta.get("marker_labels") or []:
            ys = [by_mw[m] for m in e["mws"] if m in by_mw]
            if not ys:
                continue                                 # 这条标签的带线全被删了 → 标签也不要
            nm = "label band %s" % "/".join("%g" % m for m in e["mws"])
            cur = labels.get(nm)
            text, size = (cur["text"], cur["size"]) if cur is not None else (e["text"], MARKER_SIZE)
            marker_items.append({"name": nm, "text": text, "size": size,
                                 "angle": _label_angle(cur, e.get("angle", marker_default)),
                                 "y_cm": sum(ys) / len(ys), "rgb": _hex_rgb(e.get("color"))})
        # 用户改名（band 35 → band 36）或新画的 band 线：配置里没有对应标签 → 给它一个默认的 "<MW> kD"，
        # 否则它照样参与标定、Figure 上却没有字（spec §2.6：改名就是改值）。
        covered = {m for e in meta.get("marker_labels") or [] for m in e["mws"]}
        for b in model["bands"]:
            if b["mw"] in covered:
                continue
            nm = "label band %g" % b["mw"]
            cur = labels.get(nm)
            text, size = (cur["text"], cur["size"]) if cur is not None else ("%g kD" % b["mw"], MARKER_SIZE)
            marker_items.append({"name": nm, "text": text, "size": size,
                                 "angle": _label_angle(cur, marker_default),
                                 "y_cm": b["y_cm"], "rgb": (0.0, 0.0, 0.0)})
        marker_items.sort(key=lambda it: it["y_cm"])      # spread 要按 y 排好的顺序
    left_side = (fig_x_cm(ml) or 0.0) < gel_w_cm / 2.0 if marker_items else True
    justify = "Right" if left_side else "Left"
    for it in marker_items:
        it["box"] = box_offsets(measure_fn(it["text"], it["size"])[0], it["size"], it["angle"], justify)

    # ---- 标签落位：按**墨迹框**摆，角度只改框的形状；页边距也从框算
    def overflow(box, ax, ay):
        """框（相对锚点的 pt 偏移）放在胶面坐标系里 → 四边越界量（cm，0 = 没越界）。"""
        x0, y0 = (ax + box[0]) / PT, (ay + box[1]) / PT
        x1, y1 = (ax + box[2]) / PT, (ay + box[3]) / PT
        return max(0.0, -x0), max(0.0, x1 - gel_w_cm), max(0.0, -y0), max(0.0, y1 - H)

    need = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}

    def grow(ax, ay, box):
        l, r, t, b = overflow(box, ax, ay)
        need["left"], need["right"] = max(need["left"], l), max(need["right"], r)
        need["top"], need["bottom"] = max(need["top"], t), max(need["bottom"], b)

    for it in lane_items:                                # 泳道名：框的下边离胶顶 LANE_GAP，左右居中在泳道上
        ax, ay = anchor_for(it["box"], centre_x=it["x_cm"] * PT, bottom=-LANE_GAP_PT)
        it["anchor_cm"] = (ax / PT, ay / PT)
        grow(ax, ay, it["box"])
    ys_pt = spread([it["y_cm"] * PT for it in marker_items],
                   [it["box"][3] - it["box"][1] for it in marker_items])   # 挨得近的带：上下让开、保持顺序
    for it, yc in zip(marker_items, ys_pt):              # marker 标签：框的竖直中心对准带，内侧边离胶 MARKER_GAP
        inner = -MARKER_GAP_PT if left_side else gel_w_cm * PT + MARKER_GAP_PT
        ax, ay = anchor_for(it["box"], centre_y=yc, inner=inner, side="left" if left_side else "right")
        it["anchor_cm"] = (ax / PT, ay / PT)
        grow(ax, ay, it["box"])
    mg = {k: max(MARGIN_MIN_CM, v + PAD_CM) if v > 0 else MARGIN_MIN_CM for k, v in need.items()}
    fx0 = wb["artboard"][0]
    fy0 = wb["artboard"][1] + wb["artboard"][3] + BOARD_GAP
    fig_rect = (fx0, fy0, (gel_w_cm + mg["left"] + mg["right"]) * PT, (H + mg["top"] + mg["bottom"]) * PT)
    gx, gy = fx0 + mg["left"] * PT, fy0 + mg["top"] * PT       # Figure 上胶面左上角

    s = W * PT / float(gel_px[0])
    for k, sg in enumerate(segs, 1):
        x = gx + sg["dst_x0_cm"] * PT
        wcm = sg["src_x1_cm"] - sg["src_x0_cm"]
        specs.append({"kind": "clip", "layer": "gel", "name": "gel seg %d" % k,
                      "rect": (x, gy, wcm * PT, H * PT), "image": (x - sg["src_x0_cm"] * PT, gy, s), "key": gel_key,
                      "px": tuple(gel_px)})
    for it in lane_items:
        specs.append({"kind": "text", "layer": "labels", "name": it["name"], "text": it["text"],
                      "size": it["size"], "angle": it["angle"], "justify": it["justify"], "rgb": (0, 0, 0),
                      "x": gx + it["anchor_cm"][0] * PT, "y": gy + it["anchor_cm"][1] * PT})
    for it in marker_items:
        specs.append({"kind": "text", "layer": "labels", "name": it["name"], "text": it["text"],
                      "size": it["size"], "angle": it["angle"], "justify": justify, "rgb": it["rgb"],
                      "x": gx + it["anchor_cm"][0] * PT, "y": gy + it["anchor_cm"][1] * PT})

    # ---- Workbench 上的分析层：泳道号、叫出来的带
    wgx, wgy = wb["gel_x"], wb["gel_y"]
    for i, c in enumerate(cs, 1):
        specs.append({"kind": "text", "layer": "analysis", "name": "tag %d" % i, "text": str(i), "size": 6.0,
                      "x": wgx + c * PT, "y": wgy - 4, "justify": "Center", "rgb": (0.35, 0.35, 0.35)})
    for ln in report["lanes"]:
        c = cs[ln["lane"] - 1]
        for b in ln["bands"]:
            y = wgy + b["y_cm"] * PT
            specs.append({"kind": "line", "layer": "analysis", "name": "peak %d" % ln["lane"],
                          "a": (wgx + (c - 0.08) * PT, y), "b": (wgx + (c + 0.08) * PT, y),
                          "rgb": (0.1, 0.6, 0.1), "width": 0.5})

    # ---- Profiles
    px0 = wb["artboard"][0] + wb["artboard"][2] + BOARD_GAP
    prof_specs, prof_wh = (_profiles_stack if layout == "stack" else _profiles_strips)(model, report, px0, 0.0,
                                                                                       measure_fn)
    specs += prof_specs
    return {"artboards": {"Figure": fig_rect, "Profiles": (px0, 0.0) + prof_wh}, "specs": specs}


def full_scale(ln, report):
    """一条曲线的满幅（灰度级）= max(曲线最高点, 本胶最强样品道的 1/4, 10 × 该道噪声)。

    只按各道自己的最高点缩放时，空道的背景纹理（~3 灰度级）会被放大成满幅、看着像一排带
    （IMG_1496 的 2/3/7/8 道）；全胶统一刻度又会把淡带（3–4 级）压成一条线。折中：每道至少按
    最强样品道的 1/4 画 —— 空道落在底部，淡带（IMG_1496 第 5 道 11 级）仍然顶到半幅以上。
    marker 道照自己的最高点画。满幅值写在曲线旁边。"""
    peak = max(ln["profile"]["ink"] + [0.0])
    if ln["marker"]:
        return max(peak, 1.0)
    strongest = max([max(o["profile"]["ink"] + [0.0]) for o in report["lanes"] if not o["marker"]] + [0.0])
    return max(peak, 0.25 * strongest, 10.0 * ln.get("noise", 0.0), 1.0)


def _profiles_stack(model, report, x0, y0, measure_fn):
    """每道一行：横轴 = 积分窗内的 y（cm → PLOT_W_PT），曲线 = ink（每道各自满幅，右侧注明满幅灰度级）。"""
    specs = []
    top, bot = report["window"]["top_cm"], report["window"]["bottom_cm"]
    X = lambda y: x0 + LABEL_W_PT + (y - top) / max(1e-9, bot - top) * PLOT_W_PT
    head = 22.0
    cal = report["calibration"]
    xs = spread([X(y) for y, _m in cal["points"]], [measure_fn("%g" % mw, 5.0)[0] for _y, mw in cal["points"]])
    for (y, mw), xl in zip(cal["points"], xs):                       # MW 刻度（来自 band 线），字横向让开
        specs.append({"kind": "line", "layer": "profiles", "name": "axis tick", "a": (X(y), y0 + head - 4),
                      "b": (X(y), y0 + head + ROW_PT * len(report["lanes"])), "rgb": (0.85, 0.85, 0.85), "width": 0.3})
        specs.append({"kind": "text", "layer": "profiles", "name": "axis mw", "text": "%g" % mw, "size": 5.0,
                      "x": xl, "y": y0 + head - 6, "justify": "Center", "rgb": (0.4, 0.4, 0.4)})
    for r, ln in enumerate(report["lanes"]):
        base = y0 + head + ROW_PT * (r + 1) - 4
        ink = ln["profile"]["ink"]
        peak = full_scale(ln, report)
        pts = [(X(y), base - max(0.0, v) / peak * (ROW_PT - 8)) for y, v in zip(ln["profile"]["y_cm"], ink)]
        rgb = (0.75, 0.0, 0.45) if ln["marker"] else GREY if ln["trimmed"] else (0.1, 0.3, 0.8)
        if len(pts) >= 2:
            specs.append({"kind": "polyline", "layer": "profiles", "name": "curve %d" % ln["lane"],
                          "points": _thin(pts), "rgb": rgb, "width": 0.6})
        specs.append({"kind": "text", "layer": "profiles", "name": "caption %d" % ln["lane"],
                      "text": "%d %s" % (ln["lane"], ln["label"] or ""), "size": 6.0,
                      "x": x0 + 2, "y": base - 2, "justify": "Left", "rgb": (0, 0, 0)})
        specs.append({"kind": "text", "layer": "profiles", "name": "scale %d" % ln["lane"],
                      "text": "%.0f" % peak, "size": 4.5, "x": X(bot) + 3, "y": base - (ROW_PT - 8),
                      "justify": "Left", "rgb": (0.5, 0.5, 0.5)})
        for b in ln["bands"]:
            specs.append({"kind": "text", "layer": "profiles", "name": "band %d" % ln["lane"],
                          "text": "-" if b["mw_kda"] is None else "%.0f%s" % (b["mw_kda"], "*" if b["extrapolated"] else ""),
                          "size": 4.5, "x": X(b["y_cm"]), "y": base - (ROW_PT - 8) - 1, "justify": "Center",
                          "rgb": rgb})
    return specs, (LABEL_W_PT + PLOT_W_PT + 30.0, head + ROW_PT * len(report["lanes"]) + 10.0)


def _profiles_strips(model, report, x0, y0, measure_fn):
    """每道一条竖向曲线，纵轴与胶同比例（1 cm = 1 cm），曲线向右 = ink 越大；左边一列 MW 刻度。"""
    specs = []
    head, axis_w = 22.0, 30.0
    Y = lambda ycm: y0 + head + ycm * PT
    H = model["gel"]["height_cm"]
    n = len(report["lanes"])
    pts_ = report["calibration"]["points"]
    ys = spread([Y(y) for y, _m in pts_], [CAP * 5.0] * len(pts_))
    for (y, mw), yl in zip(pts_, ys):
        specs.append({"kind": "line", "layer": "profiles", "name": "axis tick", "a": (x0 + axis_w - 4, Y(y)),
                      "b": (x0 + axis_w + STRIP_W_PT * n, Y(y)), "rgb": (0.85, 0.85, 0.85), "width": 0.3})
        specs.append({"kind": "text", "layer": "profiles", "name": "axis mw", "text": "%g" % mw, "size": 5.0,
                      "x": x0 + axis_w - 6, "y": yl + CAP * 5.0 / 2.0, "justify": "Right", "rgb": (0.4, 0.4, 0.4)})
    for r, ln in enumerate(report["lanes"]):
        left = x0 + axis_w + STRIP_W_PT * r
        ink = ln["profile"]["ink"]
        peak = full_scale(ln, report)
        pts = [(left + 2 + max(0.0, v) / peak * (STRIP_W_PT - 6), Y(y)) for y, v in zip(ln["profile"]["y_cm"], ink)]
        rgb = (0.75, 0.0, 0.45) if ln["marker"] else GREY if ln["trimmed"] else (0.1, 0.3, 0.8)
        if len(pts) >= 2:
            specs.append({"kind": "polyline", "layer": "profiles", "name": "curve %d" % ln["lane"],
                          "points": _thin(pts), "rgb": rgb, "width": 0.6})
        specs.append({"kind": "text", "layer": "profiles", "name": "caption %d" % ln["lane"],
                      "text": str(ln["lane"]), "size": 6.0, "x": left + STRIP_W_PT / 2, "y": y0 + head - 6,
                      "justify": "Center", "rgb": (0, 0, 0)})
        for b in ln["bands"]:
            specs.append({"kind": "line", "layer": "profiles", "name": "band %d" % ln["lane"],
                          "a": (left + 1, Y(b["y_cm"])), "b": (left + STRIP_W_PT - 2, Y(b["y_cm"])),
                          "rgb": rgb, "width": 0.3})
    return specs, (axis_w + STRIP_W_PT * n + 10.0, head + H * PT + 10.0)


def spread(targets, sizes, gap=1.0):
    """一维排字：把中心 targets（已按顺序）挪成互不重叠（相邻中心距 ≥ 两者半尺寸之和 + gap），
    保持顺序、总位移平方最小（相邻违例的合并成簇，簇整体放在其成员目标的均值上）。→ 新中心。"""
    if not targets:
        return []
    off = [0.0]
    for a, b in zip(sizes, sizes[1:]):
        off.append(off[-1] + (a + b) / 2.0 + gap)
    clusters = []                                             # [起, 止, 基准]
    for i, t in enumerate(targets):
        clusters.append([i, i, t - off[i]])
        while len(clusters) > 1:
            a, b = clusters[-2], clusters[-1]
            if a[2] + off[a[1]] + (sizes[a[1]] + sizes[b[0]]) / 2.0 + gap <= b[2] + off[b[0]] + 1e-9:
                break
            lo, hi = a[0], b[1]
            base = sum(targets[k] - off[k] for k in range(lo, hi + 1)) / (hi - lo + 1)
            clusters[-2:] = [[lo, hi, base]]
    out = [0.0] * len(targets)
    for lo, hi, base in clusters:
        for k in range(lo, hi + 1):
            out[k] = base + off[k]
    return out


def _thin(pts, tol=0.15):
    """去掉与前后两点共线（偏差 < tol pt）的中间点：剖面 ~700 点 → 文档小、app 画得快。"""
    if len(pts) <= 2:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        (ax, ay), (bx, by), (cx, cy) = out[-1], pts[i], pts[i + 1]
        dx, dy = cx - ax, cy - ay
        d = abs(dy * (bx - ax) - dx * (by - ay)) / max(1e-9, (dx * dx + dy * dy) ** 0.5)
        if d >= tol:
            out.append(pts[i])
    out.append(pts[-1])
    return [(round(x, 2), round(y, 2)) for x, y in out]


def _hex_rgb(h):
    if not h:
        return (0.0, 0.0, 0.0)
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _label_angle(cur, fallback):
    """标签的角度：文档里现有的（用户改过的）优先，没有才用配置/上次记下的。"""
    if cur is not None and cur.get("angle") is not None:
        return window_deg(cur["angle"])
    return window_deg(fallback)


# ---------------------------------------------------------------- 规格 → 文档节点

def node(doc, spec):
    """一条规格 → vc_file 节点（id 由 doc 分配）。"""
    k = spec["kind"]
    if k == "text":
        return vc_file.text(doc, spec["name"], spec["x"], spec["y"], spec["text"], size=spec["size"],
                            rgb=spec["rgb"], justify=spec["justify"], angle=spec.get("angle", 0.0))
    if k == "line":
        return vc_file.line(doc, spec["name"], spec["a"], spec["b"], spec["rgb"], spec["width"])
    if k == "polyline":
        return vc_file.polyline(doc, spec["name"], spec["points"], spec["rgb"], spec["width"])
    if k == "clip":
        ix, iy, s = spec["image"]
        img = vc_file.image(doc, "gel", spec["key"], spec["px"][0], spec["px"][1], ix, iy, s)
        return vc_file.clip(doc, spec["name"], spec["rect"], img)
    raise RuntimeError("未知规格 kind=%r" % k)


def _meta_angles(model, tg):
    """把算好的标签角度写回 model["meta"]（lane_angles / marker_labels[].angle；0 不写）。

    render 与 sync 共用：写出的 ep-meta 因此总和布局一致，刚 render 完的文档第一次 sync = 0 个操作。
    lane_angles 按泳道顺序存（增删泳道时由 plan_sync 先按上一次的泳道匹配重排）。
    """
    meta = model["meta"]
    specs = {sp["name"]: sp for sp in tg["specs"] if sp["layer"] == "labels"}
    old = meta.get("lane_angles") or []
    default_lane = float((meta.get("label_angle") or {}).get("lane", 0.0) or 0.0)
    idx = [int(k.split()[-1]) for k in specs if k.startswith("label lane ")]
    n = max(idx + [len(old)])
    final = []
    for j in range(1, n + 1):
        sp = specs.get("label lane %d" % j)
        final.append(sp["angle"] if sp else (old[j - 1] if j - 1 < len(old) else default_lane))
    if any(abs(a) > 1e-9 for a in final):
        meta["lane_angles"] = final
    else:
        meta.pop("lane_angles", None)
    for e in meta.get("marker_labels") or []:
        sp = specs.get("label band %s" % "/".join("%g" % m for m in e["mws"]))
        if sp is not None:
            if abs(sp["angle"]) > 1e-9:
                e["angle"] = sp["angle"]
            else:
                e.pop("angle", None)


def build_document(model, report, png, title="fig", layout=None):
    """render：整份新文档（vc_file builder）。Workbench 来自 workbench.add_workbench（不画泳道号/峰，
    它们在 targets 的 analysis 层里），其余全部来自 targets()。"""
    doc = vc_file.new_doc(title)
    key, w, h = vc_file.add_image(doc, png)
    wb = workbench.frame(model, 0.0, 0.0)
    tg = targets(model, report, key, (w, h), wb, layout=layout)
    _meta_angles(model, tg)                              # ep-meta 与标签角度一起写（sync 幂等的前提）
    vc_file.add_artboard(doc, "Figure", *tg["artboards"]["Figure"])
    layers = workbench.make_layers(doc)
    workbench.add_workbench(doc, layers, model, key, w, h, x0=0.0, y0=0.0, tags=False)
    vc_file.add_artboard(doc, "Profiles", *tg["artboards"]["Profiles"])
    for sp in tg["specs"]:
        vc_file.lay_children(layers[sp["layer"]]).append(node(doc, sp))
    return doc


# ---------------------------------------------------------------- sync 计划

def wb_frame(document):
    """从文档读 Workbench 画板与胶面位置（与 workbench.frame 同形）。"""
    ab = next(a for a in document["artboards"] if a["name"] == "Workbench")
    r = ab["rect"]
    img = next(n for n in vc_file.lay_children(vc_file.layer_by_name(document, "gel")) if n.get("name") == "gel")
    return {"artboard": (r["x0"], r["y0"], r["x1"] - r["x0"], r["y1"] - r["y0"]),
            "gel_x": img["kind"]["xf"][4], "gel_y": img["kind"]["xf"][5]}


def plan_sync(document, model, report, layout=None, measure_fn=None, tol=0.01):
    """活文档 + 用户改过之后读回的新模型 + 新报告 → (ops, 新 meta)。

    ops 只含必要的改动；在一份已经同步过的文档上再跑一次 → 空列表（幂等）。
    """
    ops = []
    L = {l["name"]: l for l in document["layers"]}
    gel_img = next(n for n in vc_file.lay_children(L["gel"]) if n.get("name") == "gel")
    key = gel_img["kind"]["key"]
    px = (gel_img["kind"]["width"], gel_img["kind"]["height"])
    wb = wb_frame(document)

    # ---- 标签：旧泳道号 → 新泳道号（按 ep-meta 里上次的中心就近配），用户文字带过去
    old_meta = model["meta"]
    prev = old_meta.get("prev_centres_cm") or []
    new_c = workbench.centres(model)
    match = workbench.match_lanes(prev, new_c) if prev else list(range(len(new_c)))
    by_name = {}
    for n in vc_file.lay_children(L["labels"]):
        if n.get("name") and n["kind"]["type"] == "text":
            by_name.setdefault(n["name"], n)
    old_names = old_meta.get("lane_names") or []
    old_angles = old_meta.get("lane_angles") or []
    default_lane = float((old_meta.get("label_angle") or {}).get("lane", 0.0) or 0.0)
    lane_names, lane_angles, labels, keep_ids = [], [], {}, {}
    for j, i in enumerate(match, 1):
        node_ = by_name.get("label lane %d" % (i + 1)) if i is not None else None
        lane_angles.append(old_angles[i] if i is not None and i < len(old_angles) else default_lane)
        if node_ is not None:
            info = vc_file.text_info(node_)
            lane_names.append(None if info["text"] == UNNAMED else info["text"])   # 占位 "?" = 还没起名
            labels["label lane %d" % j] = info
            keep_ids["label lane %d" % j] = node_
        else:
            lane_names.append(old_names[i] if i is not None and i < len(old_names) else None)
    for nm, n in by_name.items():
        if nm.startswith("label band "):
            labels[nm] = vc_file.text_info(n)
            keep_ids[nm] = n
    model = copy.deepcopy(model)
    model["meta"]["lane_names"] = lane_names
    model["meta"]["lane_angles"] = lane_angles
    model["meta"]["prev_centres_cm"] = new_c
    report = dict(report, lanes=[dict(ln, label=lane_names[ln["lane"] - 1]) for ln in report["lanes"]])
    tg = targets(model, report, key, px, wb, labels=labels, layout=layout, measure_fn=measure_fn)

    # ---- 角度回写进 ep-meta：标签的角度以文档为准（用户在 app 里转过的就记下来），
    #      新建标签下次仍按它；没有标签的泳道（trimmed）保留原来的角度。0 不写（旧文档 sync 后不变）。
    _meta_angles(model, tg)

    # ---- 画板
    for idx, nm in ((0, "Figure"), (2, "Profiles")):
        cur = document["artboards"][idx]["rect"]
        want = tg["artboards"][nm]
        if any(abs(a - b) > tol for a, b in zip((cur["x0"], cur["y0"], cur["x1"] - cur["x0"], cur["y1"] - cur["y0"]), want)):
            ops.append({"op": "artboard", "index": idx, "rect": [round(v, 3) for v in want]})

    want = {}
    for sp in tg["specs"]:
        want.setdefault(sp["layer"], []).append(sp)

    # ---- 整层重建：analysis、profiles（内容不变就不动，保证幂等）
    for lay in ("analysis", "profiles"):
        cur = [_spec_of(n, lay) for n in vc_file.lay_children(L[lay])]
        if not _same_specs(cur, want.get(lay, []), tol):
            ops += [{"op": "delete", "id": n["id"]} for n in vc_file.lay_children(L[lay])]
            ops += [{"op": "create", "layer": lay, "spec": sp} for sp in want.get(lay, [])]

    # ---- Figure 的胶段
    segs = {n["name"]: n for n in vc_file.lay_children(L["gel"]) if (n.get("name") or "").startswith("gel seg ")}
    for sp in want.get("gel", []):
        cur = segs.pop(sp["name"], None)
        if cur is None:
            ops.append({"op": "create", "layer": "gel", "spec": sp})
            continue
        rect_node = next(c for c in cur["kind"]["children"] if c["kind"].get("clipping"))
        img_node = next(c for c in cur["kind"]["children"] if c["kind"]["type"] == "image")
        x, y, w, h = sp["rect"]
        pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        if any(abs(a[0] - b[0]) > tol or abs(a[1] - b[1]) > tol
               for a, b in zip([tuple(p["p"]) for p in rect_node["kind"]["path"]["subpaths"][0]["anchors"]], pts)):
            ops.append({"op": "reshape", "id": rect_node["id"], "points": [list(p) for p in pts], "closed": True})
        ix, iy, _s = sp["image"]
        dx, dy = ix - img_node["kind"]["xf"][4], iy - img_node["kind"]["xf"][5]
        if abs(dx) > tol or abs(dy) > tol:
            ops.append({"op": "move", "id": img_node["id"], "dx": round(dx, 3), "dy": round(dy, 3)})
    ops += [{"op": "delete", "id": n["id"]} for n in segs.values()]

    # ---- 标签：只动位置 / 改名 / 新建 / 删除
    wanted_labels = {sp["name"]: sp for sp in want.get("labels", [])}
    used = set()
    for nm, sp in wanted_labels.items():
        n = keep_ids.get(nm)
        if n is None:
            ops.append({"op": "create", "layer": "labels", "spec": sp})
            continue
        used.add(n["id"])
        if n.get("name") != nm:
            ops.append({"op": "rename", "id": n["id"], "name": nm})
        dx, dy = sp["x"] - n["kind"]["xf"][4], sp["y"] - n["kind"]["xf"][5]
        if abs(dx) > tol or abs(dy) > tol:
            ops.append({"op": "move", "id": n["id"], "dx": round(dx, 3), "dy": round(dy, 3)})
    for nm, n in by_name.items():
        if (nm.startswith("label lane ") or nm.startswith("label band ")) and n["id"] not in used:
            ops.append({"op": "delete", "id": n["id"]})

    # ---- Workbench：trimmed 里的 trim 标记变灰、lanes 里的分隔线变青
    for lay, rgb in (("trimmed", GREY), ("lanes", workbench.RGB["lane"])):
        for n in vc_file.lay_children(L[lay]):
            st = next((it for it in n.get("appearance", {}).get("items", []) if it["kind"] == "stroke"), None)
            c = st and st["paint"].get("color")
            if not c or any(abs(c.get(k, -1) - v) > 0.01 for k, v in zip("rgb", rgb)):
                ops.append({"op": "stroke", "id": n["id"], "rgb": list(rgb)})

    # ---- ep-meta
    meta_txt = json.dumps(model["meta"], ensure_ascii=False, separators=(",", ":"))
    mt = next((n for n in vc_file.lay_children(L["ep-meta"]) if n.get("name") == "ep-meta"), None)
    if mt is not None and vc_file.text_info(mt)["text"] != meta_txt:
        ops.append({"op": "set_text", "id": mt["id"], "text": meta_txt})
    return ops, model["meta"]


def _spec_of(n, layer):
    """文档节点 → 可与目标规格比较的最小描述（只比几何与文字）。"""
    k = n["kind"]
    if k["type"] == "text":
        t = vc_file.text_info(n)
        return ("text", n.get("name"), t["text"], round(t["x"], 2), round(t["y"], 2))
    pts = tuple(tuple(round(v, 2) for v in a["p"]) for a in k["path"]["subpaths"][0]["anchors"])
    return ("path", n.get("name"), pts)


def _same_specs(cur, want, tol):
    def key(sp):
        if sp["kind"] == "text":
            return ("text", sp["name"], sp["text"], round(sp["x"], 2), round(sp["y"], 2))
        pts = sp["points"] if sp["kind"] == "polyline" else [sp["a"], sp["b"]]
        return ("path", sp["name"], tuple(tuple(round(v, 2) for v in p) for p in pts))
    return sorted(cur, key=repr) == sorted((key(s) for s in want), key=repr)


# ---------------------------------------------------------------- 把 ops 落到文档 JSON 上

def apply_ops_json(top, ops):
    """把 plan_sync 的 ops 落到 vc_file.read 出来的 top（就地改，返回 top）。V7 的控制口执行器与它一一对应。"""
    d = top["document"]
    L = {l["name"]: l for l in d["layers"]}
    parent = {}
    for lay in d["layers"]:
        for n in vc_file.lay_children(lay):
            parent[n["id"]] = (lay, None)
            for c in n["kind"].get("children") or []:
                parent[c["id"]] = (lay, n)
    nodes = {n["id"]: n for _l, n in vc_file.walk(d)}
    nxt = [max(list(nodes) + [l["id"] for l in d["layers"]] + [0]) + 1]
    shim = {"tpl": vc_file._template(vc_file.TEMPLATE), "next": 0, "blobs": {}}

    def fresh_ids(n):
        n["id"] = nxt[0]
        nxt[0] += 1
        for c in n["kind"].get("children") or []:
            fresh_ids(c)
    for op in ops:
        o = op["op"]
        if o == "artboard":
            x, y, w, h = op["rect"]
            d["artboards"][op["index"]]["rect"] = {"x0": x, "y0": y, "x1": x + w, "y1": y + h}
        elif o == "delete":
            lay, grp = parent[op["id"]]
            kids = grp["kind"]["children"] if grp else vc_file.lay_children(lay)
            kids[:] = [n for n in kids if n["id"] != op["id"]]
        elif o == "create":
            shim["next"] = nxt[0]
            if op["spec"].get("key"):
                shim["blobs"][op["spec"]["key"]] = b""     # 图在文件里已有（images 表），这里只占位
            n = node(shim, op["spec"])
            fresh_ids(n)
            vc_file.lay_children(L[op["layer"]]).append(n)
            nodes[n["id"]] = n
            parent[n["id"]] = (L[op["layer"]], None)
        elif o == "move":
            n = nodes[op["id"]]
            if "xf" in n["kind"]:
                n["kind"]["xf"][4] += op["dx"]
                n["kind"]["xf"][5] += op["dy"]
            else:
                for a in n["kind"]["path"]["subpaths"][0]["anchors"]:
                    a["p"][0] += op["dx"]
                    a["p"][1] += op["dy"]
        elif o == "rename":
            nodes[op["id"]]["name"] = op["name"]
        elif o == "set_text":
            nodes[op["id"]]["kind"]["runs"][0]["text"] = op["text"]
            del nodes[op["id"]]["kind"]["runs"][1:]
        elif o == "stroke":
            st = next(it for it in nodes[op["id"]]["appearance"]["items"] if it["kind"] == "stroke")
            st["paint"] = {"type": "solid", "color": {"model": "rgb", "r": op["rgb"][0], "g": op["rgb"][1], "b": op["rgb"][2]}}
        elif o == "reshape":
            n = nodes[op["id"]]
            n["kind"]["path"]["subpaths"] = [{"anchors": [{"p": list(p)} for p in op["points"]], "closed": op["closed"]}]
            live = n["kind"].get("live")
            if live and live.get("shape") == "rectangle":
                (x0, y0), (x1, _), (_, y1) = op["points"][0], op["points"][1], op["points"][2]
                live.update(w=x1 - x0, h=y1 - y0, xf=[1.0, 0.0, 0.0, 1.0, x0, y0])
        else:
            raise RuntimeError("未知 op %r" % o)
    return top
