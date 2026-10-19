#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""magick 外呼与分析缩略图读取（PPM → 灰度）。其余模块的最底层。"""
import os
import subprocess


MAGICK = os.environ.get("GEL_MAGICK", "magick")
FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _run(cmd, binary=True):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            "找不到 `%s`（ImageMagick 7）。按 SKILL.md 的 Workflow 第 2 步装：\n"
            "  macOS   brew install imagemagick\n"
            "  Windows winget install ImageMagick.ImageMagick\n"
            "  Linux   apt install imagemagick / dnf install ImageMagick\n"
            "装完跑 `python3 %s doctor` 自检。" % (cmd[0], "gel_photo.py"))
    if p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(cmd), p.stderr.decode()[:800]))
    return p.stdout if binary else p.stdout.decode()


def identify(path, ops=()):
    """(w, h) —— ops 是在 -format 之前要做的操作（例如 -rotate 3）。"""
    cmd = [MAGICK, path, "-auto-orient"] + list(ops) + ["-format", "%w %h", "info:"]
    w, h = _run(cmd, False).split()
    return int(w), int(h)


def read_ppm(path, longest=700):
    """分析用缩略图 → (w, h, rgb: bytes)。P6 自带尺寸，一次外呼搞定。"""
    cmd = [MAGICK, path, "-auto-orient"]
    if longest:
        cmd += ["-resize", "%dx%d" % (longest, longest)]
    cmd += ["-depth", "8", "ppm:-"]
    d = _run(cmd)
    if not d.startswith(b"P6"):
        raise RuntimeError("预期 P6 PPM，收到 %r" % d[:8])
    i, toks = 0, []
    while len(toks) < 4:
        while d[i : i + 1].isspace():
            i += 1
        if d[i : i + 1] == b"#":
            while d[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not d[j : j + 1].isspace():
            j += 1
        toks.append(d[i:j])
        i = j
    w, h, mx = int(toks[1]), int(toks[2]), int(toks[3])
    body = d[i + 1 :]
    step = 1 if mx < 256 else 2
    need = w * h * 3 * step
    if len(body) < need:
        raise RuntimeError("PPM 数据不足: %d < %d" % (len(body), need))
    return w, h, body


def to_gray(w, h, rgb):
    g = bytearray(w * h)
    for i in range(w * h):
        j = i * 3
        g[i] = (rgb[j] * 77 + rgb[j + 1] * 150 + rgb[j + 2] * 29) >> 8
    return g


# ---------------------------------------------------------------- 找胶面
