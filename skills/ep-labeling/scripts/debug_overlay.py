#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe / render --debug 的检测覆盖图。"""
from magick_io import MAGICK, _run
from text_layout import pick_font


def draw_debug(image, info, lb, out, cfg_rows=None):
    x0, y0, x1, y1 = info["bbox_analysis"]
    a = info["image"][0] / float(info["analysis"][0])
    cmd = [MAGICK, image, "-auto-orient", "-stroke", "red", "-strokewidth", "3",
           "-fill", "none", "-draw", "rectangle %d,%d %d,%d" % (x0 * a, y0 * a, x1 * a, y1 * a)]
    for (c, wid), it in zip(lb["lanes"], lb.get("interp", [False] * len(lb["lanes"]))):
        cmd += ["-stroke", "orange" if it else "red", "-strokewidth", "2",
                "-draw", "line %d,%d %d,%d" % (c * a, y0 * a, c * a, y1 * a)]
    cmd += ["-stroke", "deepskyblue", "-strokewidth", "3"]
    for i, (c, wid) in enumerate(lb["lanes"], 1):
        cmd += ["-draw", "line %d,%d %d,%d" % ((c - wid / 2) * a, y0 * a, (c - wid / 2) * a, y1 * a),
                "-draw", "line %d,%d %d,%d" % ((c + wid / 2) * a, y0 * a, (c + wid / 2) * a, y1 * a)]
    cmd += ["-stroke", "lime", "-strokewidth", "4"]
    for bands in lb["bands"]:
        for by, _ in bands:
            cmd += ["-draw", "line %d,%d %d,%d" % (x0 * a, by * a, x1 * a, by * a)]
    fnt = pick_font()
    for y in range(int(y0 * a), int(y1 * a) + 1, int(50 * a)):      # y 标尺，手动标定时照读
        cmd += ["-stroke", "gray60", "-strokewidth", "1",
                "-draw", "line %d,%d %d,%d" % (x0 * a, y, x1 * a, y)]
        if fnt:
            cmd += ["-font", fnt, "-stroke", "none", "-fill", "gray30", "-pointsize", "26",
                    "-gravity", "northwest", "-annotate", "+%d+%d" % (max(0, int(x0 * a) - 90), int(y) - 30),
                    str(int(round(y / a)))]
    if cfg_rows:
        cmd += ["-stroke", "magenta", "-strokewidth", "3"]
        for r in cfg_rows:
            cmd += ["-draw", "line %d,%d %d,%d" % (x0 * a, r * a, x1 * a, r * a)]
    cmd += ["-resize", "1200x1200", out]
    _run(cmd)
