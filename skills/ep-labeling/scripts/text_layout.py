#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字体选择、文字量宽、标签的角度与墨迹框（figure 量页边、摆标签用；字本身由 VectorCraft 画）。

角度口径（与 vc_file / 控制口一致）：度，**逆时针**为正（屏幕所见）。90° = 字从下往上读，
像 matplotlib 的 rotation=90；-90° 从上往下读。锚点与 justify 的含义见 references/vectorcraft.md §1。
"""
import math
import os

from magick_io import FONTS, MAGICK, _run


def pick_font(cfg_font=None):
    """配置里的 font 可能是 VectorCraft 的字体名（本 skill 用不了）——只在它像个真路径时采用。"""
    if cfg_font and ("/" in cfg_font or cfg_font.lower().endswith((".ttf", ".ttc", ".otf"))):
        if os.path.exists(cfg_font):
            return cfg_font
        raise RuntimeError("配置里的字体路径不存在: %s" % cfg_font)
    for f in FONTS:
        if os.path.exists(f):
            return f
    return None


_EXTENT = {}


def text_extent(font, px, text):
    """文字的真实包围盒（px）—— 对齐用，别拿「0.55 em × 字数」估。"""
    key = (font, px, text)
    if key in _EXTENT:
        return _EXTENT[key]
    cmd = [MAGICK, "-font", font or "Arial", "-pointsize", str(int(px)), "-format", "%w %h",
           "label:" + text]
    try:
        wd, ht = _run(cmd, False).split()
        _EXTENT[key] = (int(wd), int(ht))
    except Exception:
        _EXTENT[key] = (int(len(text) * 0.55 * px), int(px))
    return _EXTENT[key]


CAP = 0.716             # Arial 大写字高 / 字号：把「框的竖直中心」换成点文字的基线


def window_deg(angle):
    """角度归一化到 (-180, 180]。不是数字 → 报错（别让它变成 nan 再传染整份文档）。"""
    try:
        a = float(angle)
    except (TypeError, ValueError):
        raise RuntimeError("角度必须是数字（收到 %r）" % (angle,))
    if not math.isfinite(a):
        raise RuntimeError("角度必须是有限数字（收到 %r）" % (angle,))
    return ((a + 180.0) % 360.0) - 180.0


def box_offsets(width_pt, size_pt, angle, justify):
    """点文字的墨迹框相对**锚点**的偏移 → (dx0, dy0, dx1, dy1)，单位 pt，屏幕坐标（y 向下）。

    框的本地口径与 vc_file.text 一致：u 沿阅读方向，宽 = width_pt（按 justify 落在锚点左/中/右），
    v 从基线向上 = 大写字高 CAP × size（不含下伸部；8 pt Arial 的下伸部约 1.7 pt，页边留白吃得下）。
    angle = 逆时针度，绕锚点转：x' = cosθ·u + sinθ·v，y' = −sinθ·u + cosθ·v。
    """
    if justify not in ("Left", "Center", "Right"):
        raise RuntimeError("text justify 只收 Left/Center/Right（收到 %r）" % (justify,))
    w = float(width_pt)
    u0 = {"Left": 0.0, "Center": -w / 2.0, "Right": -w}[justify]
    u1 = u0 + w
    v0, v1 = -CAP * float(size_pt), 0.0
    t = math.radians(float(angle))
    c, s = math.cos(t), math.sin(t)
    xs = [c * u + s * v for u in (u0, u1) for v in (v0, v1)]
    ys = [-s * u + c * v for u in (u0, u1) for v in (v0, v1)]
    return (min(xs), min(ys), max(xs), max(ys))


def anchor_for(box, centre_x=None, centre_y=None, bottom=None, inner=None, side="left"):
    """由「框落在哪」反解锚点（pt）。只给需要的约束：

    centre_x / centre_y : 框的中心对齐到这个坐标；
    bottom              : 框的下边（离胶最近的那条）在这个 y；
    inner + side        : 框的内侧竖边（side=left = 框的右边、side=right = 框的左边）在这个 x。
    """
    dx0, dy0, dx1, dy1 = box
    ax = ay = None
    if centre_x is not None:
        ax = centre_x - (dx0 + dx1) / 2.0
    if centre_y is not None:
        ay = centre_y - (dy0 + dy1) / 2.0
    if bottom is not None:
        ay = bottom - dy1
    if inner is not None:
        ax = inner - (dx1 if side == "left" else dx0)
    if ax is None or ay is None:
        raise RuntimeError("anchor_for：框的位置约束不够（x/y 解不出来）")
    return ax, ay
