#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字体选择与文字的真实包围盒（figure 量页边用；字本身由 VectorCraft 画）。"""
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
