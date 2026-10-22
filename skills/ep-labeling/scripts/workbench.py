#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workbench 模型：用户在 VectorCraft 里改的那块「整胶 + 线」，与它的读回与校验（spec §4）。

模型是纯数据，单位 cm，原点 = 裁切后胶面左上角（与 record 同一口径，gel-prior §7）：

    {"gel":    {"width_cm", "height_cm"},
     "window": {"top_cm", "bottom_cm"},                      全部泳道共用的积分窗
     "lanes":  [{"left_cm", "right_cm", "trimmed"}],         按中心从左到右；第 i 条 = 泳道 i+1
     "bands":  [{"mw", "y_cm"}],                             marker 条带线，自上而下
     "marker_lane": n,                                       1 起；由条带线落在哪条泳道里决定
     "meta":   {...}}                                        ep-meta 文字对象里的运行元数据

文档里的绑定靠**图层**，不靠对象名（用户新画的线没有名字也算）：
    lanes / trimmed 图层里的竖线 → 按 x 排序两两成对（左、右）；
    window 图层里恰好两条横线 → 上、下；
    marker bands 图层里的横线 → 名字必须是 `band <MW>`；
    gel 图层里顶层的 image `gel` → Workbench 上胶面的位置与比例（pt ↔ cm 就从它的 xf 来）。
线不必完全横平竖直：竖线取两端 x 的中点，横线取两端 y 的中点。

本模块只依赖 vc_file（文档读写）与 mw_report.parse_mw（标签里的分子量数字）。
"""
import json
import re

import vc_file
from config import as_entry
from mw_report import marker_pairs, parse_mw


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
LINE_W = 0.5                       # pt
MARGIN_CM = 0.6                    # Workbench 画板四周留白（放泳道号）
GRAB_CM = 0.15                     # 横线伸出胶边/泳道边的长度，方便抓
BOTTOM_GUARD = 0.03                # 默认积分窗下沿避开胶底渐变（失效报告 §4：97.7% 处的假结构）
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
    window        = (top_cm, bottom_cm)；省略 = 整块胶面，下沿让开 3%。
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
    lanes = []
    for i, ln in enumerate(src):
        half = float(ln["width_cm"]) / 2.0
        lo = (xs[i - 1] + xs[i]) / 2.0 if i else 0.0          # 不越过与邻道的中点
        hi = (xs[i] + xs[i + 1]) / 2.0 if i + 1 < n else gel["width_cm"]
        lanes.append({"left_cm": round(max(lo, xs[i] - half), 4),
                      "right_cm": round(min(hi, xs[i] + half), 4),
                      "trimmed": (i + 1) in set(trim)})

    entries = [as_entry(e, {}) for e in marker_labels]
    span = [max(1, int(e.get("rows", 1))) for e in entries]
    if marker_rows_cm is None:
        idx = src[int(marker_lane) - 1]["index"]
        marker_rows_cm = [b["y_cm"] for b in rec["bands"][str(idx)]]
    rows = sorted(float(y) for y in marker_rows_cm)
    if len(rows) != sum(span):
        raise WorkbenchError(
            "marker 泳道 %d 探到 %d 条带，标签需要 %d 行（rows 求和）—— 不硬配。\n"
            "改 markers 的 labels（并带写成 {\"text\": \"21/20 kD\", \"rows\": 2}，缺的带删掉那条标签），"
            "或用 y_px 手工标定。" % (int(marker_lane), len(rows), sum(span)))
    for e in entries:
        if not parse_mw(e.get("text", "")):
            raise WorkbenchError("marker 标签 %r 里没有分子量数字，没法做成 band 线" % e.get("text"))
    pairs = marker_pairs(entries, span, rows, as_entry)
    bands = [{"mw": float(mw), "y_cm": round(float(y), 4)} for y, mw in pairs]

    top, bottom = window if window else (0.0, gel["height_cm"] * (1.0 - BOTTOM_GUARD))
    m = dict(meta or {})
    m.update(lane_names=list(lane_names),
             marker_labels=[{"text": e.get("text", ""), "mws": parse_mw(e.get("text", "")),
                             **({"color": e["color"]} if e.get("color") else {})} for e in entries],
             prev_centres_cm=[round(x, 4) for x in xs])
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


def add_workbench(doc, layers, model, gel_key, gel_w_px, gel_h_px, x0, y0=0.0, peaks=()):
    """把模型画到 doc 上：Workbench 画板、整胶图、线、泳道号、ep-meta。返回 frame()。

    gel_key/gel_w_px/gel_h_px = vc_file.add_image 的结果（胶面图，宽对应 gel.width_cm）；
    peaks = [(泳道号, y_cm), …] 样品条带的小刻度（V4 的分析结果；建模时可以没有）。
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
    for i, ln in enumerate(model["lanes"], 1):
        lay, col = (layers["trimmed"], RGB["trimmed"]) if ln["trimmed"] else (layers["lanes"], RGB["lane"])
        for x in (ln["left_cm"], ln["right_cm"]):
            kids(lay).append(vc_file.line(doc, "lane", (X(x), Y(win["top_cm"])), (X(x), Y(win["bottom_cm"])),
                                          col, LINE_W))
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

    # 泳道
    lanes = []
    for lname, trimmed in (("lanes", False), ("trimmed", True)):
        ls = lines(lname)
        horiz = [n for _x, _y, v, n in ls if not v]
        if horiz:
            errs.append("%s 图层里有 %d 条横线（id %s）—— 泳道边界应是竖线"
                        % (lname, len(horiz), [n["id"] for n in horiz]))
        xs = sorted(cx(x) for x, _y, v, _n in ls if v)
        if len(xs) % 2:
            errs.append("%s 图层里有 %d 条竖线（奇数）—— 每条泳道要一左一右两条；补一条或删一条"
                        % (lname, len(xs)))
            continue
        lanes += [{"left_cm": round(xs[i], 4), "right_cm": round(xs[i + 1], 4), "trimmed": trimmed}
                  for i in range(0, len(xs), 2)]
    lanes.sort(key=lambda ln: ln["left_cm"] + ln["right_cm"])

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


def validate(model, structural_only=False):
    """模型的不变式（spec §4.3）→ 错误列表；空 = 合法。structural_only 时跳过依赖 marker 泳道的检查。"""
    errs = []
    lanes = model["lanes"]
    if not lanes:
        errs.append("一条泳道都没有")
    W = model["gel"]["width_cm"]
    for i, ln in enumerate(lanes, 1):
        if ln["right_cm"] - ln["left_cm"] < 0.02:
            errs.append("泳道 %d 的左右边线几乎重合（%.3f cm）" % (i, ln["right_cm"] - ln["left_cm"]))
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
    """新泳道 → 旧泳道下标（0 起）或 None。一对一、就近；超过 tol（默认 = 旧中心距中位数的一半）不配。

    用来让用户改过的泳道名、标签跟着泳道走：增删泳道之后，名字不会顺移到邻道上。
    """
    prev = [float(x) for x in prev_centres]
    new = [float(x) for x in new_centres]
    if tol is None:
        gaps = sorted(b - a for a, b in zip(prev, prev[1:]))
        tol = gaps[len(gaps) // 2] / 2.0 if gaps else float("inf")
    cand = sorted((abs(n - p), j, i) for j, n in enumerate(new) for i, p in enumerate(prev))
    out, used = [None] * len(new), set()
    for dist, j, i in cand:
        if dist > tol:
            break
        if out[j] is None and i not in used:
            out[j] = i
            used.add(i)
    return out


def centres(model):
    return [round((ln["left_cm"] + ln["right_cm"]) / 2.0, 4) for ln in model["lanes"]]


def lane_names_after(model, prev_centres, prev_names):
    """按 match_lanes 把旧名字搬到新泳道上；新出现的泳道 → None（agent 去问用户）。"""
    m = match_lanes(prev_centres, centres(model))
    return [prev_names[i] if i is not None and i < len(prev_names) else None for i in m]


def _fmt_mw(mw):
    return ("%g" % float(mw))
