#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scene_png：**唯一的 PNG writer** —— scene + dpi → PNG（spec §2 决策 3、§7）。

版式全在 scene 里（cm / pt，与 dpi 无关）。本模块只做两件事：

    1. 由 scene 的页面规则 page_size_cm() 推出成品像素（每边只取整一次）；
    2. 胶面 PNG 贴在 (L, T)，每条文字用 anchor + size_pt 放回像素，交给
       text_layout.annotate_cmd（两遍描边与 render 完全同一条命令）。

不读照片、不跑检测、不 import 任何 stage 模块；render 和 export 都走这里，
所以「成品 PNG」只有一条画字路径（spec §9：export 与 render 逐像素相同）。
"""
import os

from magick_io import MAGICK, _run, identify
from scene import page_size_cm
from text_layout import annotate_cmd, text_extent

# 成品像素 / cm（dpi=96 时 37.795）——writer 与场景各处的唯一换算口径。
PPC = lambda dpi: float(dpi) / 2.54                                    # noqa: E731


def _dpi(scene, dpi=None):
    """本次出图用的 dpi：命令行覆盖 > scene.output.dpi（scene 文件本身不改）。"""
    return int(dpi if dpi is not None else scene["output"]["dpi"])


def page_px(scene, dpi=None):
    """scene → 成品页面像素：(W, H, L, T, face_w_px, face_h_px)。

    页面 = 胶面 + 四边留白，四边都来自 page_size_cm()（spec §4 的推导量），
    每边按 int(round(cm × dpi/2.54)) **只取整一次**；胶面同理。W/H 是三者相加，
    不是把总宽再取整一次 —— 否则胶面右/下侧会多出 1 px 的白边。
    """
    dpi = _dpi(scene, dpi)
    ppc = PPC(dpi)
    ps = page_size_cm(scene)
    face_w_px = int(round(float(scene["gel"]["width_cm"]) * ppc))
    face_h_px = int(round(float(scene["gel"]["height_cm"]) * ppc))
    left = int(round(ps["left_cm"] * ppc))
    right = int(round(ps["right_cm"] * ppc))
    top = int(round(ps["top_cm"] * ppc))
    bottom = int(round(ps["bottom_cm"] * ppc))
    W, H = face_w_px + left + right, face_h_px + top + bottom
    return (W, H, left, top, face_w_px, face_h_px)


def _resolve_gel(scene, out_path, gel_path):
    """胶面图路径：显式给了就用；否则取 scene.gel.image（相对路径按 scene/输出同目录解析）。

    render 把 fig.gel.png 与 fig.scene.json 写在同一目录，export 由调用方显式传入
    （它知道 scene 文件在哪），编辑器（E4）会把内嵌的胶面落到临时文件再传进来。
    """
    if gel_path:
        return gel_path
    path = str((scene.get("gel") or {}).get("image") or "")
    if not path:
        raise RuntimeError("scene：gel.image 为空，无法出图")
    if os.path.isabs(path):
        return path
    here = os.path.join(os.path.dirname(os.path.abspath(out_path)), path)
    return here if os.path.isfile(here) else path


def _out_items(scene, dpi, left, top):
    """scene 的每条文字 → annotate 的 (x, y, text, px, color)。

    锚点说的是「文字框的哪个点落在 (x_cm, y_cm)」：先把锚点换到成品像素，
    再按 anchor 反推 ink box 左上角（与 scene.item_box_cm 同一套语义，只是单位换回 px）。
    """
    ppc = PPC(dpi)
    out = []
    for it in scene["items"]:
        px = max(6, int(round(float(it["size_pt"]) / 72.0 * dpi)))
        tw, th = text_extent(scene["font"].get("file"), px, it["text"])
        vertical, horizontal = str(it["anchor"]).split("-")
        x_a = left + float(it["x_cm"]) * ppc
        y_a = top + float(it["y_cm"]) * ppc
        ax = x_a - (tw if horizontal == "right" else tw / 2.0 if horizontal == "center" else 0.0)
        ay = y_a - (th if vertical == "bottom" else th / 2.0 if vertical == "middle" else 0.0)
        out.append((ax, ay, it["text"], px, it["color"]))
    return out


def write_png(scene, out_path, dpi=None, gel_path=None, font_path=None):
    """scene → PNG（spec §9：与 render 的成品逐像素相同）。

    dpi 缺省取 scene.output.dpi；gel_path 缺省取 scene.gel.image（相对输出目录解析）；
    font_path 缺省取 scene.font.file。outline 直接用 scene.outline.color（None = 不描边）。
    返回本次实际用的数：页面像素、胶面像素、四边留白、条数与每条文字的落点
    —— 调用方（cmd_render）用它打印人看到的那些行，数字保证与文件一致。
    """
    dpi = _dpi(scene, dpi)
    ppc = PPC(dpi)
    W, H, left, top, face_w_px, face_h_px = page_px(scene, dpi)
    gel_path = _resolve_gel(scene, out_path, gel_path)
    if not os.path.isfile(gel_path):
        raise RuntimeError("scene 指向的胶面图不存在：%s" % gel_path)
    font = font_path if font_path is not None else (scene.get("font") or {}).get("file")
    outline = (scene.get("outline") or {}).get("color")
    bg = (scene.get("page") or {}).get("background") or "#ffffff"

    out_items = _out_items(scene, dpi, left, top)

    # 胶面按 scene 的 cm 尺寸贴：gel PNG 已经是成品 dpi 的像素时（render 写的）
    # 尺寸相同、-resize 是空操作；手写 scene 声明了别的尺寸时按 cm 贴合，不会溢出页面。
    gel_ops = ["(", gel_path, "-repage", "+0+0"]
    gw, gh = identify(gel_path)
    if (gw, gh) != (face_w_px, face_h_px):
        gel_ops += ["-resize", "%dx%d!" % (face_w_px, face_h_px)]
    gel_ops += [")"]
    base = [MAGICK, "-size", "%dx%d" % (W, H), "xc:" + bg] + gel_ops + \
        ["-gravity", "northwest", "-geometry", "+%d+%d" % (left, top), "-composite"]

    cmd = annotate_cmd(base, out_items, font, outline)
    # dpi 进 pHYs（Word/PowerPoint 靠它算物理尺寸）；+profile * 已在胶面 PNG 里做过。
    cmd += ["-units", "PixelsPerInch", "-density", str(dpi), out_path]
    _run(cmd)

    return {
        "dpi": dpi,
        "width_px": W,
        "height_px": H,
        "left_px": left,
        "top_px": top,
        "face_w_px": face_w_px,
        "face_h_px": face_h_px,
        "page_w_cm": W / ppc,
        "page_h_cm": H / ppc,
        "items": len(out_items),
        "out_items": out_items,
    }
