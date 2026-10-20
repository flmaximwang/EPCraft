#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene：出图的**版式数据**（胶面 cm 坐标 + 一条标签一项），与像素无关。

scene 是 spec §4 的那份 JSON：`render` 在画字之前把「页面、胶面、每条文字」冻结下来，
之后的 PNG / SVG / 浏览器编辑器都只读它（spec §2 决策 1/3）。

    原点 = **裁切/拼接后胶面**的左上角；
    x 向右、y 向下，长度单位 cm，字号单位 pt（与 dpi 无关）；
    胶面左边/上边的标签因此是负坐标。

与 record 一样，本模块不存照片坐标、分析尺度、倾角或任何像素框：
stage 1–4 的内部 px 只在 build() 进门前用一次，换成 cm 之后就不再回头。

页面尺寸是**推导量**，不写死在文件里（spec §4）：

    page = 胶面 + 每边 max(margin_min_cm, 文字越界 + pad_cm)

文字越界按 ink box（锚点 + size_pt + 字体实测宽高）算；writer 和编辑器都调用
page_size_cm()，所以标签变长时页面跟着长，而不是把字裁掉。

本模块只依赖 text_layout（字体度量），不 import 任何 stage 模块。
"""
import json

from text_layout import text_extent

ANCHORS = tuple("%s-%s" % (v, h)
                for v in ("top", "middle", "bottom")
                for h in ("left", "center", "right"))

# spec §4 的例句把 marker 的锚点写成 `right-middle`（横-竖），而句式给的是
# `{top|middle|bottom}-{left|center|right}`（竖-横）。两种都收：两轴各出现一次，
# 读起来无歧义；写入统一用竖-横的规范名。
_AXIS_V = ("top", "middle", "bottom")
_AXIS_H = ("left", "center", "right")


def norm_anchor(anchor):
    """锚点名 → 规范形式（竖-横，如 middle-right）；非法抛 RuntimeError。"""
    parts = str(anchor).split("-")
    if len(parts) != 2:
        raise RuntimeError("anchor=%r 非法（要 <竖>-<横>，如 bottom-center）" % (anchor,))
    a, b = parts
    if a in _AXIS_H and b in _AXIS_V:          # right-middle → middle-right
        a, b = b, a
    if a not in _AXIS_V or b not in _AXIS_H:
        raise RuntimeError("anchor=%r 非法（要 <竖>-<横>，如 bottom-center）" % (anchor,))
    return "%s-%s" % (a, b)


ROLES = ("lane", "marker", "free")

REQUIRED = ("version", "gel", "page", "output", "font", "items")

# spec §9：scene 里不许出现照片坐标/角度/缩放这类键（与 record 同一条闸）。
FORBIDDEN = ("rect", "bbox", "angle", "scale")

_BOX_CM = {}


def _f(v, what):
    """转 float；非数字给一条中文错误（validate 用）。"""
    if isinstance(v, bool):
        raise RuntimeError("scene：%s 必须是数字，不是 bool" % what)
    try:
        return float(v)
    except (TypeError, ValueError):
        raise RuntimeError("scene：%s 必须是数字（现在是 %r）" % (what, v))


def _cm_box_size(scene, item):
    """标签墨迹框的 cm 宽高（按 dpi 取整到实际写字用的 pointsize，再换成 cm）。

    ImageMagick 写字时 pointsize 取 int(round(size_pt / 72 * dpi))，这里必须用同一口径，
    否则 page 会把字量宽/量窄。text_extent 自己带缓存，这里只缓存 cm 结果。
    """
    dpi = _f(scene["output"]["dpi"], "output.dpi")
    px = max(6, int(round(_f(item["size_pt"], "items[].size_pt") / 72.0 * dpi)))
    text = str(item.get("text", ""))
    font = (scene.get("font") or {}).get("file")
    key = (font, px, text)
    if key not in _BOX_CM:
        w_px, h_px = text_extent(font, px, text)
        cm_per_px = 2.54 / dpi
        _BOX_CM[key] = (w_px * cm_per_px, h_px * cm_per_px)
    return _BOX_CM[key]


def item_box_cm(scene, item):
    """一条标签的 ink box（cm，胶面左上角为原点）：返回 (left, top, right, bottom)。

    锚点命名的是**文字框**上落在 (x_cm, y_cm) 的那个点：
        {top|middle|bottom}-{left|center|right}
    lane 用 bottom-center（框底边在胶顶之上 gap），marker 用 middle-right / middle-left
    （框竖直中心落在条带行上）。写入 scene 的就是锚点，writer 不再自己推导版式。
    """
    w, h = _cm_box_size(scene, item)
    x = _f(item["x_cm"], "items[].x_cm")
    y = _f(item["y_cm"], "items[].y_cm")
    try:
        anchor = norm_anchor(item.get("anchor", ""))
    except RuntimeError:
        raise RuntimeError("scene：标签 %r 的 anchor=%r 非法（只接受 %s）"
                           % (item.get("id"), item.get("anchor"), " / ".join(ANCHORS)))
    vertical, horizontal = anchor.split("-")
    left = x - (w if horizontal == "right" else w / 2.0 if horizontal == "center" else 0.0)
    top = y - (h if vertical == "bottom" else h / 2.0 if vertical == "middle" else 0.0)
    return (left, top, left + w, top + h)


def page_size_cm(scene):
    """spec §4 的页面规则：胶面 + 每边 max(margin_min_cm, 文字越界 + pad_cm)。

    返回宽高与四边留白（cm）。writer / 编辑器都调它，页面永远由当前文字推导。
    """
    gw = _f(scene["gel"]["width_cm"], "gel.width_cm")
    gh = _f(scene["gel"]["height_cm"], "gel.height_cm")
    margin = _f(scene["page"]["margin_min_cm"], "page.margin_min_cm")
    pad = _f(scene["page"]["pad_cm"], "page.pad_cm")
    need = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
    for item in scene.get("items") or []:
        left, top, right, bottom = item_box_cm(scene, item)
        need["left"] = max(need["left"], -left)
        need["right"] = max(need["right"], right - gw)
        need["top"] = max(need["top"], -top)
        need["bottom"] = max(need["bottom"], bottom - gh)
    left = max(margin, need["left"] + pad)
    right = max(margin, need["right"] + pad)
    top = max(margin, need["top"] + pad)
    bottom = max(margin, need["bottom"] + pad)
    return {
        "width_cm": gw + left + right,
        "height_cm": gh + top + bottom,
        "left_cm": left,
        "right_cm": right,
        "top_cm": top,
        "bottom_cm": bottom,
    }


def _item_id(role, bind, n):
    """稳定的 item id：label 指什么就写在 id 里（writer 只当唯一键用）。"""
    if role == "lane" and bind.get("lane") is not None:
        return "lane-%s" % bind["lane"]
    if role == "marker" and bind.get("label_index") is not None:
        return "marker-%s-%s" % (bind.get("marker_lane", 0), bind["label_index"])
    return "item-%d" % n


def build(items, face_bb, px_per_cm, dpi, font=None, outline=None,
          margin_frac=0.05, gel_image="", record=None, written=None,
          background="#ffffff", face_px=None, out_items=None, out_origin=None):
    """stage 1–4 的画布 px → scene dict（spec §4）。

    items      = cmd_render 已经算好的文字项，每项一个 dict：
                 {ax, ay, box_w, box_h}（**画布 px** 的 ink box 左上角/宽高）、
                 text / size_pt / color / role / bind / anchor。
    face_bb    = 最终胶面（裁切/拼接版式）在画布上的 (x0, y0, x1, y1)，px。
    px_per_cm  = stage 1 冻结的画布 px / cm。
    dpi        = 成品 dpi（只进 output.dpi；scene 本身是物理量）。
    font       = 字体文件路径（None = magick 默认）。
    outline    = 白边颜色；None = 不描边。
    margin_frac/ gel_image / record/ written = 页面留白比例、gel PNG 文件名、来源信息。
    face_px    = 胶面在成品里的实际像素宽高 (w, h)；给了就用它定 gel.width/height_cm，
                 页面规则才能和成品 PNG 的像素数严格对上（E2 的 writer 要用）。
    out_items/out_origin = 成品里的文字框（与 items 一一对应）和胶面左上角 (L, T)。
                 给了就按**成品 px** 反算锚点 cm：writer 从 cm 回到 px 时无亚像素漂移。

    只做一次换算：锚点 px → 胶面 cm。页边距在这里也换算成 cm，
    margin_frac 之后不再出现在任何地方（writer 只读 scene）。
    """
    ppc = _f(px_per_cm, "px_per_cm")
    x0, y0, x1, y1 = (_f(v, "face_bb") for v in face_bb)
    canvas_w_cm = (x1 - x0) / ppc
    canvas_h_cm = (y1 - y0) / ppc
    out_ppc = _f(dpi, "dpi") / 2.54
    if face_px:
        face_w_cm = _f(face_px[0], "face_px") / out_ppc
        face_h_cm = _f(face_px[1], "face_px") / out_ppc
    else:
        face_w_cm, face_h_cm = canvas_w_cm, canvas_h_cm
    margin_min_cm = float(margin_frac) * max(canvas_w_cm, canvas_h_cm)
    # 旧口径：base_m = margin_frac × 胶面长边（画布 px），pad = max(6 px, base_m × 0.4)。
    # 换成 cm 时 6 px 也除以 ppc；base_m × 0.4 换成 cm 就是 margin_min_cm × 0.4。
    pad_cm = max(6.0 / ppc, margin_min_cm * 0.4)

    out_boxes = out_items or []
    out_boxes = [(it.get("ax"), it.get("ay")) if isinstance(it, dict) else it for it in out_boxes]
    out_x0, out_y0 = out_origin if out_origin else (x0, y0)

    out_list = []
    for n, it in enumerate(items, 1):
        anchor = str(it["anchor"])
        vertical, horizontal = anchor.split("-")
        bind = dict(it.get("bind") or {})
        ident = it.get("id") or _item_id(it["role"], bind, n)
        if out_boxes:
            # 成品 px 的 box 左上角 + 成品字号量出的 box 宽高 → 锚点（px）
            ox, oy = _f(out_boxes[n - 1][0], "out_items"), _f(out_boxes[n - 1][1], "out_items")
            size_px = max(6, int(round(_f(it["size_pt"], "items[].size_pt") / 72.0 * float(dpi))))
            bw, bh = text_extent(font, size_px, str(it.get("text", "")))
            x_px = ox + (bw if horizontal == "right" else bw / 2.0 if horizontal == "center" else 0.0)
            y_px = oy + (bh if vertical == "bottom" else bh / 2.0 if vertical == "middle" else 0.0)
            x_cm = (x_px - out_x0) / out_ppc
            y_cm = (y_px - out_y0) / out_ppc
        else:
            ax, ay = _f(it["ax"], "items[].ax"), _f(it["ay"], "items[].ay")
            bw, bh = _f(it["box_w"], "items[].box_w"), _f(it["box_h"], "items[].box_h")
            x_px = ax + (bw if horizontal == "right" else bw / 2.0 if horizontal == "center" else 0.0)
            y_px = ay + (bh if vertical == "bottom" else bh / 2.0 if vertical == "middle" else 0.0)
            x_cm = (x_px - x0) / ppc
            y_cm = (y_px - y0) / ppc
        out_list.append({
            "id": ident,
            "role": it["role"],
            "bind": bind,
            "text": str(it.get("text", "")),
            "x_cm": round(x_cm, 6),
            "y_cm": round(y_cm, 6),
            "anchor": anchor,
            "size_pt": float(it["size_pt"]),
            "color": it.get("color", "#000000"),
        })

    scene = {
        "version": 1,
        "gel": {
            "image": gel_image,
            "width_cm": round(face_w_cm, 4),
            "height_cm": round(face_h_cm, 4),
        },
        "page": {
            "margin_min_cm": round(margin_min_cm, 4),
            "pad_cm": round(pad_cm, 4),
            "background": background,
        },
        "output": {"dpi": int(dpi)},
        "font": {
            "family": _font_family(font),
            "file": font,
        },
        "outline": {
            "color": outline,
            "width_frac": 0.07,
        },
        "items": out_list,
    }
    source = {"skill": "ep-labeling"}
    if record:
        source["record"] = record
    if written:
        source["written"] = written
    scene["source"] = source
    return scene


def _font_family(font):
    """字体文件 → 给 SVG/浏览器用的 family 名（Arial.ttf → Arial；没有就用 Arial）。"""
    if not font:
        return "Arial"
    name = str(font).replace("\\", "/").rsplit("/", 1)[-1]
    for ext in (".ttf", ".ttc", ".otf"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name or "Arial"


def _walk_keys(o):
    """递归收集 dict 的所有键名（禁键检查用）。"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _walk_keys(v)


def validate(scene):
    """scene schema（spec §4）：必需键、唯一 id、角色/锚点/字号、gel.image、version=1。

    另外拒绝任何名字里带 rect / bbox / angle / scale 的键（spec §9，与 record 同一条闸）。
    不合法一律 RuntimeError（与代码库其余校验一致）；合法返回 scene 本身。
    """
    if not isinstance(scene, dict):
        raise RuntimeError("scene：顶层必须是 JSON 对象")
    missing = [k for k in REQUIRED if k not in scene]
    if missing:
        raise RuntimeError("scene：缺必需键 %s（必需 %s）"
                           % (" / ".join(missing), " / ".join(REQUIRED)))
    if int(scene["version"]) != 1:
        raise RuntimeError("scene：version 必须是 1（现在是 %r）" % (scene["version"],))

    bad = [k for k in _walk_keys(scene)
           if any(t in str(k).lower() for t in FORBIDDEN)]
    if bad:
        raise RuntimeError("scene：出现了照片坐标/角度/缩放的键 %s（spec §9 不允许）"
                           % ", ".join(sorted(set(map(str, bad)))))

    gel = scene["gel"]
    if not isinstance(gel, dict) or not str(gel.get("image") or "").strip():
        raise RuntimeError("scene：gel.image 必须给出（gel PNG 的文件名）")
    if _f(gel.get("width_cm"), "gel.width_cm") <= 0 or _f(gel.get("height_cm"), "gel.height_cm") <= 0:
        raise RuntimeError("scene：gel.width_cm / height_cm 必须为正数")
    page = scene["page"]
    if not isinstance(page, dict):
        raise RuntimeError("scene：page 必须是对象")
    if _f(page.get("margin_min_cm"), "page.margin_min_cm") < 0 or _f(page.get("pad_cm"), "page.pad_cm") < 0:
        raise RuntimeError("scene：page.margin_min_cm / pad_cm 不能为负")
    if _f(scene["output"].get("dpi"), "output.dpi") <= 0:
        raise RuntimeError("scene：output.dpi 必须为正数")
    if not isinstance(scene["font"], dict):
        raise RuntimeError("scene：font 必须是对象")

    items = scene.get("items")
    if not isinstance(items, list):
        raise RuntimeError("scene：items 必须是数组")
    seen = set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise RuntimeError("scene：items[%d] 必须是对象" % i)
        for k in ("id", "role", "text", "x_cm", "y_cm", "anchor", "size_pt", "color"):
            if k not in it:
                raise RuntimeError("scene：items[%d] 缺 %s" % (i, k))
        ident = str(it["id"])
        if not ident:
            raise RuntimeError("scene：items[%d] 的 id 不能为空" % i)
        if ident in seen:
            raise RuntimeError("scene：id 重复：%s" % ident)
        seen.add(ident)
        if it["role"] not in ROLES:
            raise RuntimeError("scene：%s 的 role=%r 非法（只接受 %s）"
                               % (ident, it["role"], " / ".join(ROLES)))
        try:
            norm_anchor(it["anchor"])
        except RuntimeError:
            raise RuntimeError("scene：%s 的 anchor=%r 非法（只接受 %s）"
                               % (ident, it["anchor"], " / ".join(ANCHORS)))
        if _f(it["size_pt"], "items[].size_pt") <= 0:
            raise RuntimeError("scene：%s 的 size_pt 必须为正数" % ident)
        _f(it["x_cm"], "items[].x_cm")
        _f(it["y_cm"], "items[].y_cm")
        if not isinstance(it["text"], str):
            raise RuntimeError("scene：%s 的 text 必须是字符串" % ident)
    return scene


def write(scene, path):
    """写 scene：UTF-8、不转义中文、缩进 2、末尾换行（diff 才干净）。写完前先 validate。"""
    validate(scene)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scene, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load(path):
    """读回 scene 并校验（下游 writer / 测试用）。"""
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f))
