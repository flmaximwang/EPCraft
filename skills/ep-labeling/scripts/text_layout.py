#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字体、文字真实包围盒、magick 写字命令。"""
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


def annotate_cmd(ops, items, font, outline="#ffffff"):
    """items: [(x, y, text, size_pt, color)] —— 坐标是**输出（成图）像素**，y 是文字基线。

    先 `-density 72`：magick 会按图片当时的 density 把 pointsize 再乘一次 dpi/72，
    带着 300 dpi 写字会让 9 pt 变成 ~156 px（实测大 4 倍）。dpi 元数据留到最后再写。
    outline 给字描一圈白边：照片里胶顶与泳道号过渡是渐变的，纯黑字压上去就糊（实测）。
    """
    cmd = list(ops) + ["-density", "72"]
    if font:
        cmd += ["-font", font]
    cmd += ["-gravity", "northwest"]
    if outline:
        # 先整批描白底（-stroke 是画在字上面的，直接配 -fill 黑会把字吃成白字，实测），
        # 再整批盖黑字。两批分开跑，避免后一条的白底压住前一条的字。
        cmd += ["-stroke", outline]
        for x, y, text, size_pt, _color in items:
            px = max(6, int(round(size_pt)))
            cmd += ["-pointsize", str(px),          # 必须显式给：不给就是 IM 默认 12 pt，
                    "-strokewidth", "%.1f" % max(1.0, px * 0.07), "-fill", outline,   # 字号不随 dpi 变
                    "-annotate", "+%d+%d" % (int(round(x)), int(round(y))), text]
        cmd += ["-stroke", "none"]
    for x, y, text, size_pt, color in items:
        px = max(6, int(round(size_pt)))            # pointsize=pt 在 72 dpi 下就是 px
        cmd += ["-pointsize", str(px), "-fill", color,
                "-annotate", "+%d+%d" % (int(round(x)), int(round(y))), text]
    return cmd


_EXTENT = {}
_INKH = {}

def ink_height(font, px, sample="250kD"):
    """文字**墨迹**高（去掉字体自带的行内留白）。

    判断「两个标签会不会叠字」要用这个，不能用 `label:` 的框高 —— 框里有字体的上下留白
    （8 pt @300dpi：框高 33 px，墨迹只有 23 px），拿框高比会把本来就放得下的相邻标签也报警。
    """
    key = (font, px)
    if key in _INKH:
        return _INKH[key]
    try:
        h = int(_run([MAGICK, "-font", font or "Arial", "-pointsize", str(int(px)),
                      "label:" + sample, "-trim", "-format", "%h", "info:"], False))
    except Exception:
        h = int(round(0.72 * px))
    _INKH[key] = h
    return h


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
