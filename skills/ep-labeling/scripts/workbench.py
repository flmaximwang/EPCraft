#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workbench 模型：用户在 VectorCraft 里改的那块「整胶 + 线」，与它的读回与校验（spec §4）。

模型是纯数据，单位 cm，原点 = 裁切后胶面左上角（与 record 同一口径，gel-prior §7）：

    {"gel":    {"width_cm", "height_cm"},
     "window": {"top_cm", "bottom_cm"},                      全部泳道共用的积分窗
     "lanes":  [{"left_cm", "right_cm", "trimmed"}],         按中心从左到右；第 i 条 = 泳道 i+1；
                                                             相邻泳道首尾相接（泳道 i 的 right = 泳道 i+1 的 left）
     "bands":  [{"mw", "y_cm"}],                             marker 条带线，自上而下
     "marker_lane": n,                                       1 起；由条带线落在哪条泳道里决定
     "meta":   {...}}                                        ep-meta 文字对象里的运行元数据

文档里的绑定靠**图层**，不靠对象名（用户新画的线没有名字也算）：
    lanes 图层里的竖线 → 按 x 排序，相邻两条夹一条泳道（相邻泳道共用中间那条分隔线：n 道 = n+1 条线）；
    trimmed 图层里的短线 → 中点落在哪条泳道里，那条泳道就不进 Figure（trim 标记）；
    window 图层里恰好两条横线 → 上、下；
    marker bands 图层里的横线 → 名字必须是 `band <MW>`；
    gel 图层里顶层的 image `gel` → Workbench 上胶面的位置与比例（pt ↔ cm 就从它的 xf 来）。
线不必完全横平竖直：竖线取两端 x 的中点，横线取两端 y 的中点。

依赖 vc_file（文档读写）、mw_report（分子量数字与配对）；from_image 另用 well_grid（泳道种子）与 profiles（marker 行）。
"""
import json
import re

import profiles
import vc_file
import well_grid
from config import as_entry
from mw_report import marker_pairs, parse_mw
from text_layout import window_deg


PT = vc_file.PT_PER_CM
# 图层：列表顺序 = 文件里自下而上（spec §4.2）。(名字, printable, visible, locked, 颜色预设)
LAYERS = [
    ("gel", True, True, True, 0),
    ("analysis", False, True, True, 5),
    ("window", False, True, False, 1),
    ("lanes", False, True, False, 2),
    ("trimmed", False, True, False, 6),
    ("marker bands", False, True, False, 3),
    ("labels", True, True, False, 4),
    ("profiles", True, True, True, 7),
    ("ep-meta", False, False, True, 8),
]
RGB = {"lane": (0.0, 0.7, 1.0), "trimmed": (0.6, 0.6, 0.6), "window": (1.0, 0.55, 0.0),
       "band": (0.9, 0.0, 0.6)}
TRIM_Y_CM = 0.35                   # trim 标记画在胶面上沿之上多少 cm（泳道号在 0.14 cm 处）
LINE_W = 0.5                       # pt
MARGIN_CM = 0.6                    # Workbench 画板四周留白（放泳道号）
GRAB_CM = 0.15                     # 横线伸出胶边/泳道边的长度，方便抓
BOTTOM_GUARD = 0.03                # 默认积分窗下沿避开胶底渐变（失效报告 §4：97.7% 处的假结构）
TOP_GUARD_CM = 0.15                # 默认积分窗上沿避开加样孔：孔底的 U 形轮廓在胶面最上 0.12 cm 里，
                                   # 不让开时每条剖面开头都是一根大尖峰（V5 在 IMG_1496 上看到），还会撑大每道的满幅
_BAND_NAME = re.compile(r"^\s*band\s+(\d+(?:\.\d+)?)\s*$", re.I)


class WorkbenchError(RuntimeError):
    """Workbench 读不通：消息逐条写「在 VectorCraft 里改什么」。"""


# ---------------------------------------------------------------- 从探测结果建模

def from_record(rec, lane_names, marker_labels, marker_lane, marker_rows_cm=None,
                trim=(), window=None, meta=None):
    """record（stage 1–3 的 cm 输出）→ Workbench 模型。

    lane_names    = 每条泳道的名字（从左到右，"" = 空道），长度 = record 的泳道数；
    marker_labels = markers 配置的 labels（字符串或 {text, rows}），自上而下；
    marker_lane   = marker 泳道号（1 起）；
    marker_rows_cm= 手工标定的 marker 行（cm）；省略 = 用 record 里该泳道探到的条带；
    trim          = 要放进 trimmed 图层的泳道号；
    window        = (top_cm, bottom_cm)；省略 = 上沿让开加样孔 0.15 cm、下沿让开 3%。
    条带数与标签需要的行数（rows 求和）对不上 → 拒绝（不硬配，同 render 的老闸门）。
    """
    gel = {"width_cm": float(rec["gel"]["width_cm"]), "height_cm": float(rec["gel"]["height_cm"])}
    src = sorted(rec["lanes"], key=lambda ln: ln["x_cm"])
    n = len(src)
    if len(lane_names) != n:
        raise WorkbenchError("lanes 配置有 %d 个名字，胶上是 %d 条泳道" % (len(lane_names), n))
    if not 1 <= int(marker_lane) <= n:
        raise WorkbenchError("marker 泳道 %s 超出 1–%d" % (marker_lane, n))
    xs = [float(ln["x_cm"]) for ln in src]
    lanes = _cells(xs, gel["width_cm"], trim, float(src[0]["width_cm"]) / 2.0, float(src[-1]["width_cm"]) / 2.0)

    if marker_rows_cm is None:
        idx = src[int(marker_lane) - 1]["index"]
        marker_rows_cm = [b["y_cm"] for b in rec["bands"][str(idx)]]
    return _assemble(gel, lanes, marker_rows_cm, lane_names, marker_labels, marker_lane, window, meta,
                     "探到")


def from_image(png, gel_width_cm, gel_height_cm, lane_count, pitch_prior_cm, lane_names, marker_labels,
               marker_lane, marker_rows_cm=None, trim=(), window=None, meta=None):
    """胶面图（stage 1 的产物，PNG 字节）→ Workbench 模型。不用老 stage 2/3（V8）：

    泳道 = well_grid 的等距梳子（相位与间距由胶面顶上的加样孔定）；
    marker 条带线 = marker 泳道剖面上叫出来的峰（profiles.call_bands）—— 比老 stage 3 的行准：
    老行在 5 张 DGC 照片上一律偏下 0.13–0.5 mm（V4 实测）。
    返回 (模型, 种子信息 dict：pitch_cm / first_cm / source / depth / note)。
    """
    w, h, gray = profiles.gel_gray(png)
    ppc = w / float(gel_width_cm)
    if abs(h / ppc - gel_height_cm) > max(0.02, 2.0 / ppc):
        raise WorkbenchError("胶面图 %dx%d px 与胶面 %.3f×%.3f cm 不成比例" % (w, h, gel_width_cm, gel_height_cm))
    if len(lane_names) != lane_count:
        raise WorkbenchError("lanes 配置有 %d 个名字，胶上是 %d 条泳道" % (len(lane_names), lane_count))
    if not 1 <= int(marker_lane) <= lane_count:
        raise WorkbenchError("marker 泳道 %s 超出 1–%d" % (marker_lane, lane_count))
    sd = well_grid.seed(gray, w, h, ppc, lane_count, pitch_prior_cm)
    lanes = _cells([sd["first_cm"] + k * sd["pitch_cm"] for k in range(lane_count)], float(gel_width_cm), trim,
                   sd["pitch_cm"] / 2.0, sd["pitch_cm"] / 2.0)
    # 胶面高取**图本身**的宽高比（整像素）：预设高 6.25 cm 在 300 dpi 下是 738 px = 6.2469 cm。
    # 读回（from_document）只能从图量高，两边口径不一致时，刚 render 完的文档第一次 sync 就会「改」画板与剪切框。
    gel = {"width_cm": float(gel_width_cm), "height_cm": h * float(gel_width_cm) / w}
    top, bottom = window if window else (TOP_GUARD_CM, gel["height_cm"] * (1.0 - BOTTOM_GUARD))
    if marker_rows_cm is None:
        ml = lanes[int(marker_lane) - 1]
        ys, gs = profiles.lane_profile(gray, w, h, ppc, ml["left_cm"], ml["right_cm"], top, bottom)
        _b, _ink, sm = profiles.ink_trace(gs, ppc)
        marker_rows_cm = [b["y_cm"] for b in profiles.call_bands(ys, gs, sm, ppc)[0]]
    m = dict(meta or {})
    m["seed"] = {k: sd[k] for k in ("pitch_cm", "first_cm", "source", "depth")}
    model = _assemble(gel, lanes, marker_rows_cm, lane_names, marker_labels, marker_lane, (top, bottom), m,
                      "的剖面上叫出")
    return model, sd


def _cells(xs, width_cm, trim, half_first, half_last):
    """泳道中心 → 首尾相接的泳道：相邻两道的分隔线在两中心的中点；最外两条线在首/末道中心外 half_*。"""
    n = len(xs)
    cuts = ([max(0.0, xs[0] - half_first)] + [(a + b) / 2.0 for a, b in zip(xs, xs[1:])]
            + [min(width_cm, xs[-1] + half_last)])
    return [{"left_cm": round(cuts[i], 4), "right_cm": round(cuts[i + 1], 4), "trimmed": (i + 1) in set(trim)}
            for i in range(n)]


def _assemble(gel, lanes, marker_rows_cm, lane_names, marker_labels, marker_lane, window, meta, how):
    """两个建模入口的共同后半段：marker 行配 MW（不硬配）、积分窗、元数据、校验。"""
    entries = [as_entry(e, {}) for e in marker_labels]
    span = [max(1, int(e.get("rows", 1))) for e in entries]
    rows = sorted(float(y) for y in marker_rows_cm)
    if len(rows) != sum(span):
        raise WorkbenchError(
            "marker 泳道 %d %s %d 条带（y = %s cm），标签需要 %d 行（rows 求和）—— 不硬配。\n"
            "改 markers 的 labels（并带写成 {\"text\": \"21/20 kD\", \"rows\": 2}，缺的带删掉那条标签），"
            "或手工给 marker 行。" % (int(marker_lane), how, len(rows), [round(y, 2) for y in rows], sum(span)))
    for e in entries:
        if not parse_mw(e.get("text", "")):
            raise WorkbenchError("marker 标签 %r 里没有分子量数字，没法做成 band 线" % e.get("text"))
    pairs = marker_pairs(entries, span, rows, as_entry)
    bands = [{"mw": float(mw), "y_cm": round(float(y), 4)} for y, mw in pairs]

    top, bottom = window if window else (TOP_GUARD_CM, gel["height_cm"] * (1.0 - BOTTOM_GUARD))
    m = dict(meta or {})
    marker_angle = float((m.get("label_angle") or {}).get("marker", 0.0) or 0.0)

    def _label_meta(e):
        """一条 marker 标签的元数据。角度 0 不写（旧文档 sync 后逐字节不变 → 幂等）。"""
        d = {"text": e.get("text", ""), "mws": parse_mw(e.get("text", ""))}
        a = window_deg(e.get("angle", marker_angle))
        if abs(a) > 1e-9:
            d["angle"] = a
        if e.get("color"):
            d["color"] = e["color"]
        return d

    m.update(lane_names=list(lane_names),
             marker_labels=[_label_meta(e) for e in entries],
             prev_centres_cm=[round((ln["left_cm"] + ln["right_cm"]) / 2.0, 4) for ln in lanes])
    model = {"gel": gel, "window": {"top_cm": round(float(top), 4), "bottom_cm": round(float(bottom), 4)},
             "lanes": lanes, "bands": bands, "marker_lane": int(marker_lane), "meta": m}
    errs = validate(model)
    if errs:
        raise WorkbenchError("探测结果建出的 Workbench 不合法：\n- " + "\n- ".join(errs))
    return model


# ---------------------------------------------------------------- 写进文档

def make_layers(doc):
    """按 spec §4.2 的顺序建全部图层（Figure / Profiles 的东西以后也放进这些层）。→ {名字: 图层}"""
    return {name: vc_file.add_layer(doc, name, printable=p, visible=v, locked=lk, color=col)
            for name, p, v, lk, col in LAYERS}


def frame(model, x0, y0=0.0):
    """Workbench 画板与胶面在文档里的位置（pt）：{"artboard": (x, y, w, h), "gel_x", "gel_y"}。"""
    m = MARGIN_CM * PT
    w = model["gel"]["width_cm"] * PT + 2 * m
    h = model["gel"]["height_cm"] * PT + 2 * m
    return {"artboard": (x0, y0, w, h), "gel_x": x0 + m, "gel_y": y0 + m}


def add_workbench(doc, layers, model, gel_key, gel_w_px, gel_h_px, x0, y0=0.0, peaks=(), tags=True):
    """把模型画到 doc 上：Workbench 画板、整胶图、线、泳道号、ep-meta。返回 frame()。

    gel_key/gel_w_px/gel_h_px = vc_file.add_image 的结果（胶面图，宽对应 gel.width_cm）；
    peaks = [(泳道号, y_cm), …] 样品条带的小刻度（V4 的分析结果；建模时可以没有）；
    tags=False 时不画泳道号（figure.build_document 让 targets() 统一画 analysis 层）。
    """
    fr = frame(model, x0, y0)
    vc_file.add_artboard(doc, "Workbench", *fr["artboard"])
    gx, gy = fr["gel_x"], fr["gel_y"]
    W, H = model["gel"]["width_cm"], model["gel"]["height_cm"]
    s = W * PT / float(gel_w_px)
    if abs(gel_h_px * s - H * PT) > max(1.0, 0.01 * H * PT):
        raise WorkbenchError("胶面图 %dx%d px 与胶面 %.3f×%.3f cm 的宽高比不符"
                             % (gel_w_px, gel_h_px, W, H))
    X = lambda cm: gx + cm * PT
    Y = lambda cm: gy + cm * PT
    kids = vc_file.lay_children
    kids(layers["gel"]).append(vc_file.image(doc, "gel", gel_key, gel_w_px, gel_h_px, gx, gy, s))

    win = model["window"]
    for nm in ("top", "bottom"):
        y = Y(win[nm + "_cm"])
        kids(layers["window"]).append(vc_file.line(doc, nm, (X(-GRAB_CM), y), (X(W + GRAB_CM), y),
                                                   RGB["window"], LINE_W))
    lanes = model["lanes"]
    for x in [lanes[0]["left_cm"]] + [ln["right_cm"] for ln in lanes]:   # 相邻泳道共用一条分隔线
        kids(layers["lanes"]).append(vc_file.line(doc, "lane", (X(x), Y(win["top_cm"])), (X(x), Y(win["bottom_cm"])),
                                                  RGB["lane"], LINE_W))
    for i, ln in enumerate(lanes, 1):
        if ln["trimmed"]:
            a, b = ln["left_cm"], ln["right_cm"]
            q = (b - a) / 4.0
            kids(layers["trimmed"]).append(vc_file.line(doc, "trim", (X(a + q), Y(-TRIM_Y_CM)), (X(b - q), Y(-TRIM_Y_CM)),
                                                        RGB["trimmed"], 1.5))
        if tags:
            kids(layers["analysis"]).append(vc_file.text(
                doc, "tag %d" % i, X((ln["left_cm"] + ln["right_cm"]) / 2.0), gy - 4, str(i), size=6,
                rgb=(0.35, 0.35, 0.35)))
    mk = model["lanes"][model["marker_lane"] - 1]
    for b in model["bands"]:
        y = Y(b["y_cm"])
        kids(layers["marker bands"]).append(vc_file.line(
            doc, "band %s" % _fmt_mw(b["mw"]), (X(mk["left_cm"] - GRAB_CM), y), (X(mk["right_cm"] + GRAB_CM), y),
            RGB["band"], LINE_W))
    for lane, y_cm in peaks:
        ln = model["lanes"][lane - 1]
        cx = (ln["left_cm"] + ln["right_cm"]) / 2.0
        kids(layers["analysis"]).append(vc_file.line(
            doc, "peak %d" % lane, (X(cx - 0.08), Y(y_cm)), (X(cx + 0.08), Y(y_cm)), (0.1, 0.6, 0.1), LINE_W))
    kids(layers["ep-meta"]).append(vc_file.text(
        doc, "ep-meta", fr["artboard"][0], fr["artboard"][1] + fr["artboard"][3] + 12,
        json.dumps(model["meta"], ensure_ascii=False, separators=(",", ":")), size=4, justify="Left"))
    return fr


# ---------------------------------------------------------------- 从文档读回

def from_document(document):
    """document.json 的结果（或 vc_file.read 的 top["document"]）→ (模型, 提示列表)。

    读不通 → WorkbenchError，消息列出全部要改的地方（一次说完，不让用户改一处跑一次）。
    """
    errs, notes = [], []
    gel_img = None
    lay = vc_file.layer_by_name(document, "gel")
    if lay:
        imgs = [n for n in vc_file.lay_children(lay) if n["kind"].get("type") == "image" and n.get("name") == "gel"]
        if len(imgs) == 1:
            gel_img = imgs[0]
    if gel_img is None:
        raise WorkbenchError("找不到 Workbench 的胶面图（gel 图层里名叫 gel 的图片，应恰好一张）—— "
                             "这不是本 skill 写的文档，或 gel 图层被改过；请重新 render")
    a, _b, _c, d_, gx, gy = gel_img["kind"]["xf"]
    if abs(a - d_) > 1e-6 * max(1.0, abs(a)):
        raise WorkbenchError("Workbench 胶面图被非等比缩放过（gel 图层应是锁定的）；请重新 render")
    W = gel_img["kind"]["width"] * a / PT
    H = gel_img["kind"]["height"] * a / PT
    cx = lambda pt: (pt - gx) / PT
    cy = lambda pt: (pt - gy) / PT

    def lines(name):
        """该图层里的线 → [(中点 x pt, 中点 y pt, 是否竖, 节点)]；非线对象记提示后跳过。"""
        l = vc_file.layer_by_name(document, name)
        out = []
        for _top, n in vc_file.walk({"layers": [l]} if l else {"layers": []}):
            k = n.get("kind", {})
            if k.get("type") != "path" or len(k["path"]["subpaths"]) != 1 or k["path"]["subpaths"][0]["closed"]:
                if k.get("type") not in ("group",):
                    notes.append("%s 图层里有一个不是直线的对象（id %s），忽略" % (name, n.get("id")))
                continue
            (x0, y0), (x1, y1) = vc_file.line_ends(n)
            out.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0, abs(x1 - x0) <= abs(y1 - y0), n))
        return out

    # 积分窗
    wl = lines("window")
    if len(wl) != 2 or any(v for _x, _y, v, _n in wl):
        errs.append("window 图层应当恰好有两条横线（top、bottom），现在是 %d 条线%s"
                    % (len(wl), "，其中有竖线" if any(v for *_r, v, _n in wl) else ""))
        top = bottom = None
    else:
        top, bottom = sorted(cy(y) for _x, y, _v, _n in wl)
        if bottom - top < 0.05:
            errs.append("window 的 top 与 bottom 几乎重合（%.3f cm）—— 拉开它们" % (bottom - top))
        if top < 0 or bottom > H:
            notes.append("window 超出胶面（%.2f–%.2f cm，胶高 %.2f）—— 分析时裁到胶面内" % (top, bottom, H))
            top, bottom = max(0.0, top), min(H, bottom)

    meta = {}
    ml = vc_file.layer_by_name(document, "ep-meta")
    mt = [n for n in (vc_file.lay_children(ml) if ml else []) if n.get("name") == "ep-meta"]
    if mt:
        try:
            meta = json.loads(vc_file.text_info(mt[0])["text"])
        except ValueError:
            notes.append("ep-meta 文字不是 JSON（被改过？）—— 泳道名匹配退回按顺序")
    else:
        notes.append("没有 ep-meta 文字对象 —— 泳道名匹配退回按顺序")

    # 泳道
    lanes = _shared_lanes(lines, cx, errs)

    # marker 条带线
    bands, band_x, band_nm = [], [], []
    for x, y, vert, n in lines("marker bands"):
        m = _BAND_NAME.match(n.get("name") or "")
        if vert:
            errs.append("marker bands 图层里有竖线（id %s）—— 条带线应是横线" % n.get("id"))
        elif not m:
            errs.append("marker bands 图层里有一条线名叫 %r（id %s）—— 改名为「band <分子量>」，如 band 35"
                        % (n.get("name"), n.get("id")))
        else:
            bands.append({"mw": float(m.group(1)), "y_cm": round(cy(y), 4)})
            band_x.append(cx(x))
            band_nm.append(n.get("name"))
    bands.sort(key=lambda b: b["y_cm"])

    model = {"gel": {"width_cm": round(W, 4), "height_cm": round(H, 4)},
             "window": {"top_cm": round(top, 4) if top is not None else None,
                        "bottom_cm": round(bottom, 4) if bottom is not None else None},
             "lanes": lanes, "bands": bands, "marker_lane": None, "meta": meta}
    if not errs and band_x:
        where = [next((i for i, ln in enumerate(lanes, 1) if ln["left_cm"] <= x <= ln["right_cm"]), None)
                 for x in band_x]
        stray = [nm for nm, w in zip(band_nm, where) if w is None]
        hits = sorted({w for w in where if w is not None})
        if stray:
            errs.append("条带线 %s 的中点不在任何泳道里 —— 把它们拖回 marker 泳道上，或把那条泳道的边线拉宽"
                        % ", ".join(stray))
        elif len(hits) > 1:
            errs.append("marker 条带线横跨了泳道 %s —— 它们应当都落在 marker 那一条泳道里" % hits)
        else:
            model["marker_lane"] = hits[0]
    errs += validate(model, structural_only=bool(errs))
    if errs:
        raise WorkbenchError("Workbench 有 %d 处要改（什么都没动）：\n- %s" % (len(errs), "\n- ".join(errs)))
    return model, notes


def _shared_lanes(lines, cx, errs):
    """lanes 层的竖线按 x 排序、相邻两条夹一道；trimmed 层每条短线的中点落在哪道，哪道就 trim。"""
    ls = lines("lanes")
    horiz = [n for _x, _y, v, n in ls if not v]
    if horiz:
        errs.append("lanes 图层里有 %d 条横线（id %s）—— 泳道分隔线应是竖线"
                    % (len(horiz), [n["id"] for n in horiz]))
    xs = sorted(cx(x) for x, _y, v, _n in ls if v)
    if len(xs) < 2:
        errs.append("lanes 图层里只有 %d 条竖线 —— 至少两条才夹得出一条泳道" % len(xs))
        return []
    lanes = [{"left_cm": round(a, 4), "right_cm": round(b, 4), "trimmed": False} for a, b in zip(xs, xs[1:])]
    for x, _y, vert, n in lines("trimmed"):
        if vert:
            errs.append("trimmed 图层里有竖线（id %s）—— 泳道分隔线都留在 lanes 里；要拿掉一条泳道，"
                        "在 trimmed 里画一条短横线落在那条泳道里" % n.get("id"))
            continue
        hit = next((ln for ln in lanes if ln["left_cm"] < cx(x) < ln["right_cm"]), None)
        if hit is None:
            errs.append("trimmed 图层里的线（id %s）中点不在任何泳道里 —— 把它拖进要拿掉的那条泳道上方"
                        % n.get("id"))
        else:
            hit["trimmed"] = True
    return lanes


def validate(model, structural_only=False):
    """模型的不变式（spec §4.3）→ 错误列表；空 = 合法。structural_only 时跳过依赖 marker 泳道的检查。"""
    errs = []
    lanes = model["lanes"]
    if not lanes:
        errs.append("一条泳道都没有")
    W = model["gel"]["width_cm"]
    for i, ln in enumerate(lanes, 1):
        if ln["right_cm"] - ln["left_cm"] < 0.02:
            errs.append("泳道 %d 的两条边线几乎重合（%.3f cm）—— 删掉多出来的那条线" % (i, ln["right_cm"] - ln["left_cm"]))
        if ln["right_cm"] <= 0 or ln["left_cm"] >= W:
            errs.append("泳道 %d（%.2f–%.2f cm）整个在胶面外" % (i, ln["left_cm"], ln["right_cm"]))
    for i in range(1, len(lanes)):
        if lanes[i]["left_cm"] < lanes[i - 1]["right_cm"] - 1e-6:
            errs.append("泳道 %d 与泳道 %d 重叠（%.2f > %.2f cm）—— 两条泳道的边线交错了"
                        % (i, i + 1, lanes[i - 1]["right_cm"], lanes[i]["left_cm"]))
    win = model["window"]
    if win.get("top_cm") is not None and win["top_cm"] >= win["bottom_cm"]:
        errs.append("window 的 top 在 bottom 下面")
    if structural_only:
        return errs
    bands = model["bands"]
    if len(bands) < 3:
        errs.append("marker 条带线只剩 %d 条，标定至少要 3 条" % len(bands))
    for a, b in zip(bands, bands[1:]):
        if b["mw"] >= a["mw"]:
            errs.append("条带线 band %s（y=%.2f cm）在 band %s（y=%.2f cm）下面，但分子量不更小 —— "
                        "检查名字或位置" % (_fmt_mw(b["mw"]), b["y_cm"], _fmt_mw(a["mw"]), a["y_cm"]))
    # marker 泳道可以在 trimmed 里：不进 Figure，标定照样用（同 --marker-policy calibrate-only）
    ml = model.get("marker_lane")
    if ml is None or not 1 <= ml <= len(lanes):
        errs.append("找不到 marker 泳道")
    return errs


# ---------------------------------------------------------------- 泳道名跟着泳道走

def match_lanes(prev_centres, new_centres, tol=None):
    """新泳道 → 旧泳道下标（0 起）或 None。一对一、**保序**、按距离配。

    用来让用户改过的泳道名、标签跟着泳道走：增删泳道之后，名字不会顺移到邻道上。

    V10（Issue #1）：旧实现有两个毛病 ——
      a) 容差是「半个孔距」（0.52 cm 的胶上 tol=0.26），而**半个孔距正好是最常见的合法编辑量**
         （把最外侧那条线拖到胶边，泳道中心实测挪 0.2651 cm）；差 0.005 cm 就判成「新泳道」，
         旧道被删、名字变 `?`，还 create+delete 翻掉对象 id。
      b) 就近贪心、不看顺序：整体平移时可能把相邻道配反。
    现在：容差放宽到 0.75 × 上一次孔距中位数（容纳拖边，又小于一个孔距、不会配到邻道）；
    配对用**保序** DP —— 只允许 i、j 各自单调增（泳道按 x 排序，交叉配对没有物理意义），
    优先配对数最多、其次总位移最小；容差外配不上的新道 → None（agent 去问名字）。
    """
    prev = [float(x) for x in prev_centres]
    new = [float(x) for x in new_centres]
    if not new:
        return []
    if not prev:
        return [None] * len(new)
    gaps = sorted(b - a for a, b in zip(prev, prev[1:]))
    if tol is None:
        tol = 0.75 * gaps[len(gaps) // 2] if gaps else float("inf")
    n, m = len(prev), len(new)
    score = [[(0, 0.0)] * (n + 1) for _ in range(m + 1)]     # (配对数, −总位移)，比大小即可
    take = [[False] * (n + 1) for _ in range(m + 1)]
    for j in range(1, m + 1):
        for i in range(1, n + 1):
            best = max(score[j - 1][i], score[j][i - 1])
            d = abs(new[j - 1] - prev[i - 1])
            if d <= tol:
                pairs, neg = score[j - 1][i - 1]
                cand = (pairs + 1, neg - d)
                if cand > best:
                    best, take[j][i] = cand, True
            score[j][i] = best
    out: list = [None] * m
    j, i = m, n
    while j and i:
        if take[j][i]:
            out[j - 1] = i - 1
            j, i = j - 1, i - 1
        elif score[j - 1][i] >= score[j][i - 1]:
            j -= 1
        else:
            i -= 1
    return out


def centres(model):
    return [round((ln["left_cm"] + ln["right_cm"]) / 2.0, 4) for ln in model["lanes"]]


def lane_names_after(model, prev_centres, prev_names):
    """按 match_lanes 把旧名字搬到新泳道上；新出现的泳道 → None（agent 去问用户）。"""
    m = match_lanes(prev_centres, centres(model))
    return [prev_names[i] if i is not None and i < len(prev_names) else None for i in m]


def _fmt_mw(mw):
    return ("%g" % float(mw))
