#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workbench 模型：用户在 VectorCraft 里改的那块「整胶 + 线」，与它的读回与校验（spec §4）。

模型是纯数据，单位 cm，原点 = 裁切后胶面左上角（与 record 同一口径，gel-prior §7）：

    {"gel":    {"width_cm", "height_cm"},
     "window": {"top_cm", "bottom_cm"},                      全部泳道共用的积分窗
     "lanes":  [{"left_cm", "right_cm", "trimmed",          按中心从左到右；第 i 条 = 泳道 i+1；
                 ← 梯形才有的四个端点 →}],                   相邻泳道首尾相接（泳道 i 的 right = 泳道 i+1 的 left）
     "bands":  [{"mw", "y_cm"}],                             marker 条带线，自上而下
     "marker_lane": n,                                       1 起；由条带线落在哪条泳道里决定
                                                             （0 条线时取 ep-meta 记的那个）
     "meta":   {...}}                                        ep-meta 文字对象里的运行元数据

`left_cm` / `right_cm` 是泳道在**胶顶那一行（y = 0）**的边界。泳道通常是矩形（两条边都是竖线），
这时它们就是全窗常数，泳道字典与旧版逐键相同。分隔线画斜了（梯形泳道，凝胶 smile / 拍摄透视 /
加样孔与胶底不平行）时，字典再加四个端点键（cm，同一口径）：

    {..., "left_top_cm", "left_bottom_cm", "right_top_cm", "right_bottom_cm"}

`*_top_cm` = y = 0 处的 x，`*_bottom_cm` = y = gel.height_cm 处的 x，且 left_cm == left_top_cm、
right_cm == right_top_cm。两条边线不得在胶面内相交（validate 报「第 i 与第 j 条分隔线…相交」）；
矩形泳道不写这四个键 —— 旧文件、旧测试与旧输出都按原样成立。剖面逐行取该行自己的 L(y)..R(y)
（profiles.sample_lane），Figure 的泳道剪切按四边形裁（以胶顶那行定宽度与拼缝，下边跟着倾斜）。

文档里的绑定靠**图层**，不靠对象名（用户新画的线没有名字也算）：
    lanes 图层里的竖线 → 按 x 排序，相邻两条夹一条泳道（相邻泳道共用中间那条分隔线：n 道 = n+1 条线）；
    trimmed 图层里的短线 → 中点落在哪条泳道里，那条泳道就不进 Figure（trim 标记）；
    window 图层里恰好两条横线 → 上、下；
    marker bands 图层里的横线 → 名字必须是 `band <MW>`；
    sample bands 图层里的线 → 样品带刻度（名字 `peak n.k`；读回时按中点 x 判泳道，名字不参与，spec §7.5）；
    gel 图层里顶层的 image `gel` → Workbench 上胶面的位置与比例（pt ↔ cm 就从它的 xf 来）。
线不必完全横平竖直：竖线**保留两端**（两端 x 不同 = 梯形泳道的一条边，不是取中点压成竖线；
只把线沿自身方向延长到胶顶 / 胶底两行），横线取两端 y 的中点。

依赖 vc_file（文档读写）、mw_report（分子量数字与 MW 槽位展开）；from_image 另用 well_grid（泳道种子）与
profiles（marker 行）。marker 带「出生即 MW」：第 k 条检出带 = 第 k 个 MW 槽（ep-bdt），没有配对步骤。
"""
import json
import re

from . import profiles
from . import vc_file
from . import well_grid
from .config import as_entry
from .mw_report import expand_marker_mws, parse_mw, split_peak_note
from .text_layout import window_deg


PT = vc_file.PT_PER_CM
# 图层：列表顺序 = 文件里自下而上（spec §4.2）。(名字, printable, visible, locked, 颜色预设)
# 颜色预设只用 0–8（打得开的文档里用过的值；更大的没验过）
LAYERS = [
    ("gel", True, True, True, 0),
    ("analysis", False, True, True, 5),          # 只有泳道号 tag n（每次 sync 重建）
    ("sample bands", False, True, False, 5),     # 样品带刻度 peak n.k：用户可删 / 拖 / 补（spec §7.5）
    ("window", False, True, False, 1),
    ("lanes", False, True, False, 2),
    ("trimmed", False, True, False, 6),
    ("marker bands", False, True, False, 3),
    ("labels", True, True, False, 4),
    ("profiles", True, True, True, 7),
    ("ep-meta", False, False, True, 8),
    ("photo", False, True, True, 0),             # Crop 画板（crop-artboard spec §3）：照片预览，锁定
    ("crop", False, True, False, 2),             # Crop 画板：裁切框，用户可移动 / 旋转 / 等比缩放
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


def rows_needed(marker_labels):
    """markers 配置的 labels → 要几条 band 线（rows 求和）。"""
    return sum(max(1, int(as_entry(e, {}).get("rows", 1))) for e in marker_labels)


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
    标定行按 y 升序直接挂 MW 槽（ep-bdt）：0/1/2 行都收，数量对不上只记进 marker_confidence。
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
    marker 条带线 = marker 泳道剖面上**显著度最高的 need 个局部极小**（profiles.pick_marker，spec ep-9ji）：
    带数由标签给（MW 槽位之和），不用噪声门槛 —— 梯子道的噪声量的是条带自己（IMG_1586 第 3 道 σ = 17.2，
    11 条带只过 1 条）。位置比老 stage 3 的行准：老行在 5 张 DGC 照片上一律偏下 0.13–0.5 mm（V4 实测）。
    第 k 条检出带（y 升序）直接挂第 k 个 MW 槽（ep-bdt「出生即 MW」）—— 没有配对步骤，数量对不上也不拒绝：
    叫出几条放几条线，少了只挂前 k 个 MW，多了不落线。
    pick_marker 的闸门（断崖 + 下限 + 候选数）**只决定置信度**：不抛错，结果写进 meta["marker_confidence"]
    = {ok, picked, need, gate, notes}（gate = None 或没过闸门的原因）。render 用它挑候选道 / 标低置信。
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
    need = rows_needed(marker_labels)
    m = dict(meta or {})
    gate = None
    if marker_rows_cm is None:
        ml = lanes[int(marker_lane) - 1]
        ys, gs = profiles.lane_profile(gray, w, h, ppc, ml["left_cm"], ml["right_cm"], top, bottom)
        picked_rows, info = profiles.pick_marker(ys, gs, ppc, need)
        marker_rows_cm = [r["y_cm"] for r in picked_rows]
        gate = info                                           # 断崖倍数 / 闸门结果 → meta.marker_confidence
    m["seed"] = {k: sd[k] for k in ("pitch_cm", "first_cm", "source", "depth")}
    model = _assemble(gel, lanes, marker_rows_cm, lane_names, marker_labels, marker_lane, (top, bottom), m,
                      "的剖面上叫出", gate)
    return model, sd


def _cells(xs, width_cm, trim, half_first, half_last):
    """泳道中心 → 首尾相接的矩形泳道：相邻两道的分隔线在两中心的中点；最外两条线在首/末道中心外 half_*。"""
    n = len(xs)
    cuts = ([max(0.0, xs[0] - half_first)] + [(a + b) / 2.0 for a, b in zip(xs, xs[1:])]
            + [min(width_cm, xs[-1] + half_last)])
    return [{"left_cm": round(cuts[i], 4), "right_cm": round(cuts[i + 1], 4), "trimmed": (i + 1) in set(trim)}
            for i in range(n)]


# ---------------------------------------------------------------- 梯形泳道的几何

def _edge_x(ln, side, y_cm, height_cm):
    """泳道某一侧在 y 处的 x（cm）。矩形泳道 = 常数 `left_cm` / `right_cm`；梯形按 y 线性插值。"""
    x_t = float(ln["left_cm"] if side == "left" else ln["right_cm"])
    x_b = float(ln.get(side + "_bottom_cm", x_t))
    if abs(x_b - x_t) <= 0 or not height_cm:
        return x_t
    t = min(1.0, max(0.0, float(y_cm) / float(height_cm)))
    return x_t + (x_b - x_t) * t


def lane_bounds(ln, y_cm, height_cm=None):
    """泳道在 y 处的两条边界 x（cm）→ (L, R)。矩形泳道与 y 无关；梯形按 `*_bottom_cm` 插值。

    与 profiles.lane_bounds 同一口径；这里多一层是为了让建模 / 画线不依赖 profiles 的细节。
    height_cm 省略时梯形泳道按 profiles 的口径报错（必须知道胶面高才能插值）。"""
    return profiles.lane_bounds(ln, y_cm, height_cm)


def lane_centre_at(ln, y_cm, height_cm=None):
    """泳道在 y 处的中点 x（cm）；标签 / 刻度落位用（胶顶那行就是泳道中心）。"""
    l, r = lane_bounds(ln, y_cm, height_cm)
    return (l + r) / 2.0


def lane_edges(model, y0_cm=0.0, y1_cm=None, height_cm=None):
    """模型的 n+1 条分隔线 → [(y0 处的 x_cm, y1 处的 x_cm)]；下标 + 1 = 分隔线号。

    y0 / y1 省略时是胶顶 / 胶底两行（相交检查用）。画线时传**积分类窗**的 top / bottom：
    斜线仍是同一条直线，只是短一点，读回时沿它自己的方向延伸回两行，端点值不变；
    而按胶面两行画会把线挪到窗外的位置，读回就偏一点。"""
    lanes = model["lanes"]
    if not lanes:
        return []
    H = height_cm if height_cm is not None else model.get("gel", {}).get("height_cm")
    y1 = H if y1_cm is None else y1_cm
    at = lambda ln, side: (_edge_x(ln, side, y0_cm, H), _edge_x(ln, side, y1, H))   # noqa: E731
    return [at(lanes[0], "left")] + [at(ln, "right") for ln in lanes]


def _edge_crossings(edges, height_cm):
    """分隔线两两在胶面**内部**相交 → [(i, j, y_cm)]（i、j 是 1 起的线号，i < j；y 是交点所在的胶面行）。

    两条线都是 x(y) 的线性函数，所以看它们在胶顶 / 胶底两行的差 d(0)、d(H) 就够了：
    符号严格相反 = 在 (0, H) 之间穿过。端点处相碰（宽度趋 0）不算相交 —— 那是「边线几乎重合」的
    宽度检查管的事，两条线在胶顶/胶底并到一起时逐条报，不会漏。"""
    out = []
    eps = 1e-9
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            a0, a1 = edges[i]
            b0, b1 = edges[j]
            d0, d1 = b0 - a0, b1 - a1
            if not ((d0 > eps and d1 < -eps) or (d0 < -eps and d1 > eps)):
                continue
            t = -d0 / (d1 - d0)
            out.append((i + 1, j + 1, max(0.0, min(1.0, t)) * float(height_cm)))
    return out


def lane_is_quad(ln):
    """这条泳道是不是梯形（带全了四个端点键）。"""
    return profiles.is_quad(ln)


def _assemble(gel, lanes, marker_rows_cm, lane_names, marker_labels, marker_lane, window, meta, how, gate=None):
    """两个建模入口的共同后半段：MW 槽按序直挂、置信信息、积分窗、元数据、校验。

    「出生即 MW」（ep-bdt）：第 k 条带（y 升序）挂第 k 个 MW 槽，没有配对步骤；叫出几条放几条线，
    数量对不上不拒绝 —— 少了只挂前 k 个槽，多了不落线，两者都进 marker_confidence.notes。
    """
    entries = [as_entry(e, {}) for e in marker_labels]
    for e in entries:
        if not parse_mw(e.get("text", "")):
            raise WorkbenchError("marker 标签 %r 里没有分子量数字，没法做成 band 线" % e.get("text"))
    mws, notes = expand_marker_mws(entries, as_entry)
    rows = sorted(float(y) for y in marker_rows_cm)
    bands = [{"mw": float(mw), "y_cm": round(float(y), 4)}
             for y, mw in zip(rows, mws) if mw is not None]
    if len(rows) < len(mws):
        notes.append("marker 泳道 %d %s %d 条带，标签要 %d 个 MW 槽：只挂前 %d 个"
                     % (int(marker_lane), how, len(rows), len(mws), len(rows)))
    elif len(rows) > len(mws):
        notes.append("marker 泳道 %d %s %d 条带，比标签的 %d 个 MW 槽多 %d 条：多出的不落线"
                     % (int(marker_lane), how, len(rows), len(mws), len(rows) - len(mws)))
    split = split_peak_note(rows)
    if split:
        notes.append(split)

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
             prev_centres_cm=[round((ln["left_cm"] + ln["right_cm"]) / 2.0, 4) for ln in lanes],
             # ep-meta 记一份 marker 泳道：0 条 band 线时文档里没有线可量，读回只能靠它
             marker_lane=int(marker_lane))
    # 置信信息：闸门（pick_marker）只决定这里，不决定成败。picked = 真落线的条数。
    # ratio / nth / next 是断崖诊断（render 全不过闸门时拿 ratio 挑道、终端打印用），不影响语义。
    g = gate or {}
    # ok = 闸门过 + 每个 MW 槽都挂上了 + 没有劈峰提示。只比 len(rows) == len(mws) 不够：
    # rows 多于数字时计数可能相等，而槽里有 None → 实际落线更少。
    full = len(rows) == len(mws) and all(mw is not None for mw in mws)
    ok = bool(g.get("ok", True)) and full and not split
    gate_reason = None if g.get("ok", True) else g.get("reason")
    if not full and not gate_reason:
        gate_reason = "检出 %d 条带、MW 槽 %d 个、实际落线 %d 条" % (len(rows), len(mws), len(bands))
    if split and not gate_reason:
        gate_reason = split
    m["marker_confidence"] = {"ok": ok, "picked": len(bands), "need": len(mws), "gate": gate_reason,
                              "ratio": g.get("ratio"), "nth": g.get("nth"), "next": g.get("next"),
                              "notes": notes}
    model = {"gel": gel, "window": {"top_cm": round(float(top), 4), "bottom_cm": round(float(bottom), 4)},
             "lanes": lanes, "bands": bands, "marker_lane": int(marker_lane), "meta": m}
    errs = validate(model)
    if errs:
        raise WorkbenchError("探测结果建出的 Workbench 不合法：\n- " + "\n- ".join(errs))
    return model


# ---------------------------------------------------------------- 写进文档

def make_layers(doc, skip=()):
    """按 spec §4.2 的顺序建全部图层（Figure / Profiles 的东西以后也放进这些层）。→ {名字: 图层}
    skip = 不建的图层名（没有 Crop 画板的文档不建 photo / crop）。"""
    return {name: vc_file.add_layer(doc, name, printable=p, visible=v, locked=lk, color=col)
            for name, p, v, lk, col in LAYERS if name not in skip}


def layer_props(name):
    """LAYERS 里的一行 → 建图层用的属性（旧文档 sync 时补 sample bands 用）。"""
    for nm, printable, visible, locked, color in LAYERS:
        if nm == name:
            return {"name": nm, "printable": printable, "visible": visible, "locked": locked, "color": color}
    raise WorkbenchError("LAYERS 里没有图层 %r" % name)


def frame(model, x0, y0=0.0):
    """Workbench 画板与胶面在文档里的位置（pt）：{"artboard": (x, y, w, h), "gel_x", "gel_y"}。"""
    m = MARGIN_CM * PT
    w = model["gel"]["width_cm"] * PT + 2 * m
    h = model["gel"]["height_cm"] * PT + 2 * m
    return {"artboard": (x0, y0, w, h), "gel_x": x0 + m, "gel_y": y0 + m}


def add_workbench(doc, layers, model, gel_key, gel_w_px, gel_h_px, x0, y0=0.0, peaks=(), tags=True):
    """把模型画到 doc 上：Workbench 画板、整胶图、线、泳道号、ep-meta。返回 frame()。

    gel_key/gel_w_px/gel_h_px = vc_file.add_image 的结果（胶面图，宽对应 gel.width_cm）；
    peaks = [(泳道号, y_cm), …] 样品条带的小刻度（V4 的分析结果；建模时可以没有）：
        写进 sample bands 图层、名字 `peak <lane>.<k>`（k = 道内自上而下，1 起）；marker 道不画；
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
    edges = lane_edges(model, win["top_cm"], win["bottom_cm"], H)   # n+1 条分隔线（相邻泳道共用一条）
    for x_t, x_b in edges:                             # 竖线照旧；斜线按两端画（梯形泳道的边）
        kids(layers["lanes"]).append(vc_file.line(doc, "lane", (X(x_t), Y(win["top_cm"])),
                                                  (X(x_b), Y(win["bottom_cm"])), RGB["lane"], LINE_W))
    for i, ln in enumerate(lanes, 1):
        if ln["trimmed"]:                              # trim 标记落在胶顶那一行的边宽上（斜道也对齐）
            a, b = ln["left_cm"], ln["right_cm"]
            q = (b - a) / 4.0
            kids(layers["trimmed"]).append(vc_file.line(doc, "trim", (X(a + q), Y(-TRIM_Y_CM)), (X(b - q), Y(-TRIM_Y_CM)),
                                                        RGB["trimmed"], 1.5))
        if tags:
            kids(layers["analysis"]).append(vc_file.text(
                doc, "tag %d" % i, X(lane_centre_at(ln, 0.0, H)), gy - 4, str(i), size=6,
                rgb=(0.35, 0.35, 0.35)))
    mk = model["lanes"][model["marker_lane"] - 1]
    for b in model["bands"]:
        y = Y(b["y_cm"])
        xl, xr = lane_bounds(mk, b["y_cm"], H)         # 条带线按**它自己那一行**的泳道边界伸出去
        kids(layers["marker bands"]).append(vc_file.line(
            doc, "band %s" % _fmt_mw(b["mw"]), (X(xl - GRAB_CM), y), (X(xr + GRAB_CM), y),
            RGB["band"], LINE_W))
    by_lane = {}
    for lane, y_cm in peaks:
        if int(lane) != model["marker_lane"]:            # marker 道由 band <MW> 线表示，不画刻度
            by_lane.setdefault(int(lane), []).append(float(y_cm))
    for lane in sorted(by_lane):
        ln = model["lanes"][lane - 1]
        for k, y_cm in enumerate(sorted(by_lane[lane]), 1):
            cx = lane_centre_at(ln, y_cm, H)             # 刻度在它那一行的泳道中点上
            kids(layers["sample bands"]).append(vc_file.line(
                doc, "peak %d.%d" % (lane, k), (X(cx - 0.08), Y(y_cm)), (X(cx + 0.08), Y(y_cm)),
                (0.1, 0.6, 0.1), LINE_W))
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
    gel_img = vc_file.gel_node(document)
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
        """该图层里的线 → [(x0, y0, x1, y1, 是否竖, 节点)]，坐标是胶面 cm。

        两端都保留：泳道分隔线画斜了就是梯形泳道的一条边（x 随 y 线性变化），不能压成中点。
        竖/横按「沿哪个轴更长」判（与旧版同一口径）；非线对象记提示后跳过。"""
        l = vc_file.layer_by_name(document, name)
        out = []
        for _top, n in vc_file.walk({"layers": [l]} if l else {"layers": []}):
            k = n.get("kind", {})
            if k.get("type") != "path" or len(k["path"]["subpaths"]) != 1 or k["path"]["subpaths"][0]["closed"]:
                if k.get("type") not in ("group",):
                    notes.append("%s 图层里有一个不是直线的对象（id %s），忽略" % (name, n.get("id")))
                continue
            (x0, y0), (x1, y1) = vc_file.line_ends(n)
            out.append((cx(x0), cy(y0), cx(x1), cy(y1), abs(x1 - x0) <= abs(y1 - y0), n))
        return out

    def mid(t):
        """一条线（lines() 的元组）的中点 x、y（cm）。"""
        return (t[0] + t[2]) / 2.0, (t[1] + t[3]) / 2.0

    # 积分窗
    wl = lines("window")
    if len(wl) != 2 or any(t[4] for t in wl):
        errs.append("window 图层应当恰好有两条横线（top、bottom），现在是 %d 条线%s"
                    % (len(wl), "，其中有竖线" if any(t[4] for t in wl) else ""))
        top = bottom = None
    else:
        top, bottom = sorted(mid(t)[1] for t in wl)
        if bottom - top < 0.05:
            errs.append("window 的 top 与 bottom 几乎重合（%.3f cm）—— 拉开它们" % (bottom - top))
        if top < 0 or bottom > H:
            notes.append("window 超出胶面（%.2f–%.2f cm，胶高 %.2f）—— 分析时裁到胶面内" % (top, bottom, H))
            top, bottom = max(0.0, top), min(H, bottom)

    meta = {}
    node, parsed = vc_file.doc_meta(document)
    if node is None:
        notes.append("没有 ep-meta 文字对象 —— 泳道名匹配退回按顺序")
    elif parsed is not None:
        meta = parsed
    else:
        notes.append("ep-meta 文字不是 JSON（被改过？）—— 泳道名匹配退回按顺序")

    # 旧文档的草稿标定（meta.draft）就是旧版的低置信标记：换成新键，不静默丢掉置信信息。
    # 旧格式有两种：`draft = true`（布尔）和 `draft = {"picked","need","gate"}`（字典，main 上的实际形状）。
    if "marker_confidence" not in meta:
        legacy = meta.get("draft")
        if legacy:
            picked = need = gate = None
            if isinstance(legacy, dict):
                picked, need = legacy.get("picked"), legacy.get("need")
                gate = legacy.get("gate") or "旧文档的草稿标定"
            else:
                gate = "旧文档的草稿标定"
            meta["marker_confidence"] = {"ok": False, "picked": picked, "need": need,
                                         "gate": gate, "notes": []}
    meta.pop("draft", None)

    # 泳道
    lanes = _shared_lanes(lines, H, errs)

    # marker 条带线
    bands, band_x, band_y, band_nm = [], [], [], []
    for x0, y0, x1, y1, vert, n in lines("marker bands"):
        m = _BAND_NAME.match(n.get("name") or "")
        x, y = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if vert:
            errs.append("marker bands 图层里有竖线（id %s）—— 条带线应是横线" % n.get("id"))
        elif not m:
            errs.append("marker bands 图层里有一条线名叫 %r（id %s）—— 改名为「band <分子量>」，如 band 35"
                        % (n.get("name"), n.get("id")))
        else:
            bands.append({"mw": float(m.group(1)), "y_cm": round(y, 4)})
            band_x.append(x)
            band_y.append(y)
            band_nm.append(n.get("name"))
    bands.sort(key=lambda b: b["y_cm"])

    model = {"gel": {"width_cm": round(W, 4), "height_cm": round(H, 4)},
             "window": {"top_cm": round(top, 4) if top is not None else None,
                        "bottom_cm": round(bottom, 4) if bottom is not None else None},
             "lanes": lanes, "bands": bands, "marker_lane": None, "meta": meta}
    if not errs and band_x:
        where = [next((i for i, ln in enumerate(lanes, 1)
                       if lane_bounds(ln, by, H)[0] <= x <= lane_bounds(ln, by, H)[1]), None)
                 for x, by in zip(band_x, band_y)]
        stray = [nm for nm, w in zip(band_nm, where) if w is None]
        hits = sorted({w for w in where if w is not None})
        if stray:
            errs.append("条带线 %s 的中点不在任何泳道里 —— 把它们拖回 marker 泳道上，或把那条泳道的边线拉宽"
                        % ", ".join(stray))
        elif len(hits) > 1:
            errs.append("marker 条带线横跨了泳道 %s —— 它们应当都落在 marker 那一条泳道里" % hits)
        else:
            model["marker_lane"] = hits[0]
    elif not errs:
        # 一条 band 线都没有：marker 泳道只能来自 ep-meta（没有线可量）。缺了就明确报错，
        # 而不是让 sync 在一份刚 render 出来的低置信文档上失败。
        ml_meta = meta.get("marker_lane")
        if isinstance(ml_meta, int) and 1 <= ml_meta <= len(lanes):
            model["marker_lane"] = ml_meta
        else:
            errs.append("marker bands 图层里一条 band 线都没有，ep-meta 也没记 marker 泳道 —— "
                        "重新 render，或在 ep-meta 里补 marker_lane")
    errs += validate(model, structural_only=bool(errs))
    if errs:
        raise WorkbenchError("Workbench 有 %d 处要改（什么都没动）：\n- %s" % (len(errs), "\n- ".join(errs)))
    return model, notes


def _shared_lanes(lines, H, errs):
    """lanes 层的竖线 → 泳道：**相邻两条线夹一道**，两条线的两端决定这条泳道的四个角。

    排序按**胶顶那一行**的 x（梯形泳道在胶顶的左右顺序就是用户看到的顺序）。线只沿自身方向延长到
    y = 0 / y = H 两行（用户通常画在积分窗之间，延长后才是整条边）；两端 x 不同的线写进四个端点键，
    两端相同的线（竖线）照旧只写 left_cm / right_cm —— 矩形泳道的字典与旧版逐键相同。
    trim 标记按它所在那一行（不是中点平均后的 x）判道，梯形泳道斜着也一样落对。
    """
    ls = lines("lanes")
    horiz = [t for t in ls if not t[4]]
    if horiz:
        errs.append("lanes 图层里有 %d 条横线（id %s）—— 泳道分隔线应是竖线"
                    % (len(horiz), [t[5]["id"] for t in horiz]))
    edges = []
    for t in ls:
        if not t[4]:
            continue
        x0, y0, x1, y1 = t[0], t[1], t[2], t[3]
        if abs(y1 - y0) <= 1e-12:                       # 退化成一个点的线：当竖线用它的 x
            edges.append((x0, x0))
            continue
        k = (x1 - x0) / (y1 - y0)                        # x 随 y 的变化率（胶面 cm / cm）
        edges.append((x0 - k * y0, x0 + k * (H - y0)))    # y = 0 / y = H 两行上的 x
    if len(edges) < 2:
        errs.append("lanes 图层里只有 %d 条竖线 —— 至少两条才夹得出一条泳道" % len(edges))
        return []
    seps = sorted(((round(a, 4), round(b, 4)) for a, b in edges), key=lambda e: (e[0], e[1]))
    lanes = []
    for a, b in zip(seps, seps[1:]):
        ln = {"left_cm": a[0], "right_cm": b[0], "trimmed": False}
        if a[1] != a[0] or b[1] != b[0]:                 # 至少一条边是斜的 → 梯形泳道
            ln.update(left_top_cm=a[0], left_bottom_cm=a[1],
                      right_top_cm=b[0], right_bottom_cm=b[1])
        lanes.append(ln)
    for t in lines("trimmed"):
        x0, y0, x1, y1, vert, n = t
        if vert:
            errs.append("trimmed 图层里有竖线（id %s）—— 泳道分隔线都留在 lanes 里；要拿掉一条泳道，"
                        "在 trimmed 里画一条短横线落在那条泳道里" % n.get("id"))
            continue
        xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hit = next((ln for ln in lanes if lane_bounds(ln, ym, H)[0] < xm < lane_bounds(ln, ym, H)[1]), None)
        if hit is None:
            errs.append("trimmed 图层里的线（id %s）中点不在任何泳道里 —— 把它拖进要拿掉的那条泳道上方"
                        % n.get("id"))
        else:
            hit["trimmed"] = True
    return lanes


def lane_at(model, x_cm, y_cm=None):
    """点 (x, y) 落在哪条泳道（1 起）：闭区间、从左往右第一条；道外 → None。y 省略 = 胶顶那一行。

    与 from_document 判 marker 条带线同一口径；样品带刻度按它归道（spec §7.5，名字不参与）。
    梯形泳道按该行自己的 L(y)..R(y) 判（刻度 / 条带线在胶面下半时不会因斜边判错道）。"""
    H = model.get("gel", {}).get("height_cm")
    return next((i for i, ln in enumerate(model["lanes"], 1)
                 if (lambda lr: lr[0] <= x_cm <= lr[1])(
                     lane_bounds(ln, 0.0 if y_cm is None else y_cm, H))), None)


def sample_ticks(document):
    """sample bands 图层的线 → [(中点 x_cm, 中点 y_cm, 节点)]（spec §7.5：名字不参与，按中点 x 归道）。

    坐标与 from_document 同一口径：从 gel 图的 xf 把文档 pt 换成胶面 cm。图层不在 → []；
    组里的线也读；不是单条直线的对象跳过（组、文字都不是刻度）。"""
    lay = vc_file.layer_by_name(document, "sample bands")
    gel = vc_file.gel_node(document)
    if not lay or gel is None:
        return []
    gx, gy = gel["kind"]["xf"][4], gel["kind"]["xf"][5]
    out = []
    for _top, n in vc_file.walk({"layers": [lay]}):
        k = n.get("kind", {})
        subs = (k.get("path") or {}).get("subpaths") or []
        if k.get("type") != "path" or len(subs) != 1:
            continue
        (x0, y0), (x1, y1) = vc_file.line_ends(n)
        out.append((((x0 + x1) / 2.0 - gx) / PT, ((y0 + y1) / 2.0 - gy) / PT, n))
    return out


def _quad_w(ln, where):
    """泳道在胶顶 / 胶底那一行（where = 上 / 下）的宽度 cm。矩形泳道两行相同。"""
    if not lane_is_quad(ln):
        return ln["right_cm"] - ln["left_cm"]
    if where == "上":
        return ln["right_cm"] - ln["left_cm"]
    return float(ln["right_bottom_cm"]) - float(ln["left_bottom_cm"])


def validate(model, structural_only=False):
    """模型的不变式（spec §4.3）→ 错误列表；空 = 合法。structural_only 时跳过依赖 marker 泳道的检查。

    泳道可以是矩形（两条边竖）也可以是梯形（边斜，四个端点键）。检查顺序：
    每条泳道的上/下边宽度、整条在胶面外；分隔线两两相交（最多列 5 对）；相邻泳道胶顶、胶底边连续。
    矩形泳道的消息与旧版逐字相同（旧测试与用户的报错记忆都不变）。"""
    errs = []
    lanes = model["lanes"]
    if not lanes:
        errs.append("一条泳道都没有")
    W = model["gel"]["width_cm"]
    H = model["gel"]["height_cm"]
    for i, ln in enumerate(lanes, 1):
        if not lane_is_quad(ln):
            w_top = ln["right_cm"] - ln["left_cm"]
            if w_top < 0.02:
                errs.append("泳道 %d 的两条边线几乎重合（%.3f cm）—— 删掉多出来的那条线" % (i, w_top))
            if ln["right_cm"] <= 0 or ln["left_cm"] >= W:
                errs.append("泳道 %d（%.2f–%.2f cm）整个在胶面外" % (i, ln["left_cm"], ln["right_cm"]))
            continue
        l_top, r_top = ln["left_cm"], ln["right_cm"]
        l_bot, r_bot = ln["left_bottom_cm"], ln["right_bottom_cm"]
        for where, w in (("上", r_top - l_top), ("下", r_bot - l_bot)):
            if w < 0.02:
                errs.append("泳道 %d 的%s边几乎重合（%.3f cm）—— 删掉多出来的那条线" % (i, where, w))
        if max(r_top, r_bot) <= 0 or min(l_top, l_bot) >= W:
            errs.append("泳道 %d（胶顶 %.2f–%.2f cm，胶底 %.2f–%.2f cm）整个在胶面外"
                        % (i, l_top, r_top, l_bot, r_bot))
    for a, b, y in _edge_crossings(lane_edges(model), H)[:5]:
        if b == a + 1:
            errs.append("泳道 %d 的两条边线在胶面内相交（第 %d 与第 %d 条分隔线，y≈%.2f cm）—— "
                        "删掉多出来的那条线" % (a, a, b, y))
        else:
            errs.append("第 %d 与第 %d 条分隔线在胶面内相交（y≈%.2f cm）—— 它们框住的泳道交错了：拖直或删掉其中一条"
                        % (a, b, y))
    extra = len(_edge_crossings(lane_edges(model), H)) - 5
    if extra > 0:
        errs.append("分隔线相交的地方还有 %d 对，只列前 5 对" % extra)
    for i in range(1, len(lanes)):
        a, b = lanes[i - 1], lanes[i]
        if not (lane_is_quad(a) or lane_is_quad(b)):
            if b["left_cm"] < a["right_cm"] - 1e-6:
                errs.append("泳道 %d 与泳道 %d 重叠（%.2f > %.2f cm）—— 两条泳道的边线交错了"
                            % (i, i + 1, a["right_cm"], b["left_cm"]))
            continue
        for key, pkey, where in (("left_top_cm", "right_top_cm", "胶顶"),
                                 ("left_bottom_cm", "right_bottom_cm", "胶底")):
            prev = float(a.get(pkey, a["right_cm"]))
            cur = float(b.get(key, b["left_cm"]))
            if abs(cur - prev) > 1e-6:
                errs.append("泳道 %d 与泳道 %d 的%s边界不连续：%s %.4f != 上一道的 %s %.4f"
                            % (i, i + 1, where, key, cur, pkey, prev))
    win = model["window"]
    if win.get("top_cm") is not None and win["top_cm"] >= win["bottom_cm"]:
        errs.append("window 的 top 在 bottom 下面")
    if structural_only:
        return errs
    bands = model["bands"]
    # 0/1/2 条 band 线也能交付（低置信、样品 MW 无法估计）：标定条数不是结构性问题（ep-blz 验收）。
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
    """新泳道 → 旧泳道下标（0 起）或 None：**只按从左往右的序号**，不看位置、不设容差。

    用户的语义是「名字属于第几个格子」：第 i 条新泳道就是第 i 条旧泳道，多出来的（序号超出旧道数）
    → None（agent 去问名字）。所以拖线、调宽窄、把最外侧那条线拖到胶边（泳道中心挪半个孔距）都
    不动名字；删掉一条分隔线时，它右边那些格子的名字整体前移一格。
    `tol` 只为兼容旧调用保留，已不再使用。Issue #1 里「半个孔距被判成新道」的根因就是容差本身，
    这里把容差整个去掉。
    """
    prev = list(prev_centres)
    new = list(new_centres)
    return [i if i < len(prev) else None for i in range(len(new))]


def centres(model, y_cm=0.0, height_cm=None):
    """泳道中心 x（cm）列表：默认取**胶顶那一行**（y = 0），与旧版 `(left+right)/2` 逐位相同。

    y_cm 给出来时按该行的 L(y)..R(y) 取中点（梯形泳道的刻度 / 落位用）；height_cm 默认取模型的胶面高。"""
    H = height_cm
    if H is None and "gel" in model:
        H = model["gel"]["height_cm"]
    return [round(lane_centre_at(ln, y_cm, H), 4) for ln in model["lanes"]]


def lane_names_after(model, prev_centres, prev_names):
    """按 match_lanes 把旧名字搬到新泳道上；新出现的泳道 → None（agent 去问用户）。"""
    m = match_lanes(prev_centres, centres(model))
    return [prev_names[i] if i is not None and i < len(prev_names) else None for i in m]


def _fmt_mw(mw):
    return ("%g" % float(mw))
