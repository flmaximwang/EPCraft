#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene_svg：scene → SVG（spec §5）。

一份 scene（spec §4，单位 cm / pt）画成**独立**的 SVG：

    width/height 用 cm、viewBox 用 cm  ⇒ 1 user unit = 1 cm，编辑器按真实物理尺寸打开；
    胶面 PNG 以 base64 data URI 内嵌在 <g id="gel"> 里（不引用任何外部文件/相对路径）；
    每条标签一项 <text>，在 <g id="labels"> 里，带 id / data-role / data-bind；
    白边（outline）是**同一几何的第二个 <text>，压在填充字下面**（两遍画，不用 paint-order）；
    顺序 = scene.items 的顺序，writer 绝不重排（页面规则与画序无关）。

字号为什么写成 cm 数而不是 "8pt"
-------------------------------
SVG 里的绝对单位是**按 user unit** 解释的：1pt = 4/3 user unit。viewBox 是 cm 时
1 user unit = 1 cm，于是 font-size="8pt" = 10.67 cm —— 整页都是字。实测（本机 Chrome
headless 与 rsvg-convert 2.62.3 一致，都是标准 SVG/CSS 语义）：

    viewBox="0 0 10 4" width="10cm"、8pt   → 墨迹 328×80 px（10cm 宽 378 px）
    viewBox 同上、font-size="0.28222"      → 墨迹  17×8  px（= 8 pt @96dpi）

所以要「按真实物理尺寸打开」就只能写 user unit 数：size_cm = size_pt × 2.54 / 72。
`data-size-pt` 保留原始 pt 数（编辑器/人读用）。

基线怎么来的（不猜，实测）
-------------------------
SVG 没有「文字框顶边」这种写法，只有基线；而 PNG 那边 `-annotate +x+y` 的 y 是框顶边
（text_layout.annotate_cmd，selftest 已实测）。所以这里必须把「框顶边 → 基线」的偏移量
按同一字体/字号量出来：

    magick -font <ttf> -pointsize <px> -format "%@" label:250 info:
    → "52x24+1+6"（宽×墨高 + 左 + 上），墨迹行 = [上, 上+墨高−1]，基线 = 上 + 墨高

实测（Arial.ttf，本机 ImageMagick 7.1.1-44；px = size_pt/72*dpi 取整）：

    px    11   15   20   33   66   100
    基线   10   14   19   30   61    92      （= 上+墨高，单位 px，从框顶边起算）

即基线 ≈ 0.90–0.92 × px，随字号略有取整差异，所以运行时按 (font, px) 量一次并缓存，
不写死比例。px 就是 PNG writer 交给 `-pointsize` 的那个数（export: size_pt/72*dpi 取整），
所以量出来的偏移和 PNG 的落笔位置是同一个引擎、同一口径。三种竖直锚点同一条公式：

    基线 = 框顶边 + 偏移          （item_box_cm 已经把锚点落成框的四条边）
    bottom-*：= y − 框高 + 偏移 = y − descent

（等价于 brief 的 bottom: y − descent，descent = 框高 − 偏移。）

注：text_extent 在本仓库里走的是估算分支（magick 那条命令没写输出文件，会失败），
框高 = px、框宽 ≈ 0.55 em × 字数；PNG writer 与页面规则用的是同一份数，所以 writer
之间仍然对齐（§8），这里只是照 scene 的框算，不另立一套度量。
"""
import base64
import json
import os
from xml.sax.saxutils import escape, quoteattr

from magick_io import MAGICK, _run
from scene import item_box_cm, norm_anchor, page_size_cm, validate

# 白边宽度口径：与 annotate_cmd 一致（宽度 = max(1.0, px*width_frac) px；px 就是 -pointsize 的 px）
STROKE_PX_MIN = 1.0
STROKE_FRAC = 0.07

_OFFSET = {}


def baseline_offset_px(font, px):
    """框顶边 → 基线的偏移（px，按该字体/字号**实测**；带缓存）。

    用 `label:250` 的墨迹框：数字没有下伸部，墨迹最后一行 +1 就是基线（实测与
    `-annotate +0+0` 的墨迹行完全一致）。magick 不可用时退回 0.905×px（Arial 的
    ascent/em，量级对、只用于出图不至于失败）。
    """
    key = (font, int(px))
    if key in _OFFSET:
        return _OFFSET[key]
    try:
        out = _run([MAGICK, "-font", font or "Arial", "-pointsize", str(int(px)),
                    "-format", "%@", "label:250", "info:"], False).strip()
        # "52x24+1+6" → 上=6、墨高=24 ⇒ 基线 30（取最后一个 + 后面的 Y）
        box = out.split()[0]
        rest = box.split("x", 1)[1]
        h_s, _, _ = rest.partition("+")
        y_s = rest.rsplit("+", 1)[-1]
        off = int(y_s) + int(h_s)
    except Exception:
        off = int(round(0.905 * float(px)))
    _OFFSET[key] = off
    return off


def _fmt(v, nd=4):
    """数字 → 属性文本：固定小数、去掉尾零（12.34 / -0.14 / 8）。"""
    s = ("%." + str(nd) + "f") % float(v)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _size_px(scene, item, dpi):
    """标签在成品里的 pointsize（px）——与 scene._cm_box_size / export 同一口径。"""
    return max(6, int(round(float(item["size_pt"]) / 72.0 * float(dpi))))


def _color(item):
    return str(item.get("color") or "#000000")


def _outline(scene):
    """scene.outline → (颜色, 宽度分数)；没有/颜色为空 ⇒ 不描边。"""
    o = scene.get("outline") or {}
    return o.get("color"), float(o.get("width_frac", STROKE_FRAC) or STROKE_FRAC)


def _text_el(item, *, ident, x_cm, y_cm, anchor, family, size_pt,
             fill, stroke=None, stroke_cm=None):
    """一个 <text> 元素（fill 与 outline 共用同一几何，只有描边/填充不同）。

    font-size 是 user unit（= cm）数，不是 "8pt" —— 见模块头「字号」一节。
    """
    attrs = [
        'id=%s' % quoteattr(ident),
        'data-role=%s' % quoteattr(str(item.get("role", ""))),
        'data-bind=%s' % quoteattr(json.dumps(item.get("bind") or {},
                                             ensure_ascii=False, separators=(",", ":"))),
        'x="%s"' % _fmt(x_cm),
        'y="%s"' % _fmt(y_cm),
        'font-family=%s' % quoteattr(str(family)),
        'font-size="%s"' % _fmt(size_pt * 2.54 / 72.0, 5),
        'data-size-pt="%s"' % _fmt(size_pt),
        'fill=%s' % quoteattr(str(fill)),
        'text-anchor="%s"' % anchor,
        'xml:space="preserve"',
    ]
    if stroke:
        attrs += ['stroke=%s' % quoteattr(str(stroke)),
                  'stroke-width="%s"' % _fmt(stroke_cm),
                  'stroke-linejoin="round"']
    body = escape(str(item.get("text", "")))
    return "  <text %s>%s</text>" % (" ".join(attrs), body)


def _gel_uri(path):
    """胶面 PNG → data URI（standalone SVG：不引用外部文件）。"""
    if not path or not os.path.isfile(path):
        raise RuntimeError("SVG：胶面图不存在：%s" % (path or "（未给 gel_path）"))
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def svg_string(scene, gel_path=None, dpi=None):
    """scene → SVG 文本（spec §5）。gel_path 省略时用 scene.gel.image（按当前目录解析）。"""
    validate(scene)
    dpi = float(dpi or (scene.get("output") or {}).get("dpi") or 96)
    ps = page_size_cm(scene)
    gw = float(scene["gel"]["width_cm"])
    gh = float(scene["gel"]["height_cm"])
    left, top = ps["left_cm"], ps["top_cm"]
    w_cm, h_cm = ps["width_cm"], ps["height_cm"]
    font_file = (scene.get("font") or {}).get("file")
    family = (scene.get("font") or {}).get("family") or "Arial"
    bg = (scene.get("page") or {}).get("background") or "#ffffff"
    out_color, out_frac = _outline(scene)

    # 页面尺寸只写一份：width/height 与 viewBox 必须逐字一致，否则 1 user unit ≠ 1 cm。
    w_s, h_s = _fmt(w_cm), _fmt(h_cm)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        'width="%scm" height="%scm" viewBox="0 0 %s %s">' % (w_s, h_s, w_s, h_s),
        '  <title>ep-labeling figure</title>',
        # 白底：SVG 默认透明；scene.page.background 是页面数据的一部分。
        '  <rect id="page" x="0" y="0" width="%s" height="%s" fill=%s/>'
        % (w_s, h_s, quoteattr(str(bg))),
        '  <g id="gel">',
        # preserveAspectRatio=none：胶面宽高比已由 scene 的 cm 冻结，这里只做严格定位。
        '    <image x="%s" y="%s" width="%s" height="%s" preserveAspectRatio="none" href=%s/>'
        % (_fmt(left), _fmt(top), _fmt(gw), _fmt(gh), quoteattr(_gel_uri(
            gel_path or scene["gel"].get("image")))),
        '  </g>',
        '  <g id="labels">',
    ]

    cm_per_px = 2.54 / dpi
    for it in scene.get("items") or []:
        anchor = norm_anchor(it["anchor"])
        horizontal = anchor.split("-")[1]
        taut = {"left": "start", "center": "middle", "right": "end"}[horizontal]
        _box_l, box_t, _box_r, _box_b = item_box_cm(scene, it)
        px = _size_px(scene, it, dpi)
        off_cm = baseline_offset_px(font_file, px) * cm_per_px
        # 基线 = 框顶边 + 偏移；锚点已经在 item_box_cm 里落成框的四条边了，三轴同一公式。
        base_cm = box_t + off_cm
        x_page = left + float(it["x_cm"])
        y_page = top + base_cm
        size_pt = float(it["size_pt"])
        ident = str(it["id"])

        if out_color:
            # 两遍画：先白边（压在下面），同一 x/y/anchor。宽度与 annotate_cmd 同口径：
            # PNG 那边传给 -pointsize 的就是 px，描边 = max(1, px*width_frac) px（%.1f 取一位）。
            stroke_px = round(max(STROKE_PX_MIN, px * out_frac), 1)
            lines.append(_text_el(
                it, ident=ident + "-outline", x_cm=x_page, y_cm=y_page, anchor=taut,
                family=family, size_pt=size_pt, fill=out_color,
                stroke=out_color, stroke_cm=stroke_px * cm_per_px))
        lines.append(_text_el(
            it, ident=ident, x_cm=x_page, y_cm=y_page, anchor=taut,
            family=family, size_pt=size_pt, fill=_color(it)))

    lines += ['  </g>', '</svg>', '']
    return "\n".join(lines)


def write_svg(scene, out_path, gel_path=None, dpi=None):
    """写 SVG 文件（UTF-8、末尾换行），返回 {page_cm, n_items, bytes} 供日志用。"""
    s = svg_string(scene, gel_path=gel_path, dpi=dpi)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(s)
    ps = page_size_cm(scene)
    return {"page_cm": (ps["width_cm"], ps["height_cm"]),
            "n_items": len(scene.get("items") or []),
            "bytes": len(s.encode("utf-8"))}
