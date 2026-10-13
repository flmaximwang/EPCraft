#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gel_photo.py —— 手机照片里的 SDS-PAGE 胶：自动认出胶面 → 正畸裁切 → 按两个 JSON 直接标注 → 出 PNG（dpi 可调）

子命令
    probe   <图>                          只探测：报胶框/角度/泳道 x/条带行；--debug 出一张覆盖图
    render  <图> --out 出.png [配置...]    出成品：裁切+正畸（+缩放+dpi）→ 写字 → PNG
    selftest                              不读用户文件：自检 PPM 解析、magick 旋转方向与数学、峰簇/找条带

依赖：系统 python3（纯 stdlib）+ ImageMagick 7（`magick`）。像素只在分析尺度上读，成图交给 magick。

坐标约定
    - 全程用「自动定向（-auto-orient）后」的原图像素坐标。
    - 分析用缩略图（默认长边 700）找胶；所有几何量按比例回到原图。
    - 角度是 magick 的 `-rotate` 语义（正数 = 顺时针），与本项目 rotate_pt() 同一套矩阵（selftest 实测锚定）。
"""
import argparse
import json
import math
import os
import subprocess
import sys

MAGICK = os.environ.get("GEL_MAGICK", "magick")
FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# ---------------------------------------------------------------- magick 外呼


def _run(cmd, binary=True):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            "找不到 `%s`（ImageMagick 7）。按 SKILL.md「环境」一节装：\n"
            "  macOS   brew install imagemagick\n"
            "  Windows winget install ImageMagick.ImageMagick\n"
            "  Linux   apt install imagemagick / dnf install ImageMagick\n"
            "装完跑 `python3 %s doctor` 自检。" % (cmd[0], os.path.basename(__file__)))
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


def blue_cue(w, h, rgb):
    """蓝度 = max(0, B-R)。染色胶在偏蓝光下比中性背景蓝得多；中性胶这条通道≈0。"""
    b = bytearray(w * h)
    for i in range(w * h):
        j = i * 3
        v = rgb[j + 2] - rgb[j]
        b[i] = v if 0 < v < 255 else 0
    return b


def bg_plane(w, h, gray, inset=0.08, band=0.06, drop=25):
    """用四边 8%~14% 环带（避开最外圈相框）的亮样本拟一个平面当灯箱背景。

    返回 (a, b, c, info)：bg(x,y) = a + b*x + c*y。
    """
    pts = [(x, y, gray[y * w + x]) for y in range(h) for x in range(w)
           if inset <= min(x / float(w), 1 - x / float(w), y / float(h), 1 - y / float(h)) <= inset + band]
    if len(pts) < 20:
        pts = [(x, y, gray[y * w + x]) for y in range(h) for x in range(w)]
    coef = _fit_plane(pts)
    for _ in range(2):
        kept = [(x, y, g) for x, y, g in pts
                if (g - (coef[0] + coef[1] * x + coef[2] * y)) > -drop]
        if len(kept) < 20:
            break
        coef = _fit_plane(kept)
    vals = sorted(g for _, _, g in pts)
    return coef[0], coef[1], coef[2], {
        "samples": len(pts), "ring_median": vals[len(vals) // 2],
        "center": round(coef[0] + coef[1] * w / 2 + coef[2] * h / 2, 1),
    }


def _fit_plane(pts):
    n = len(pts)
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts); sz = sum(p[2] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts); syy = sum(p[1] * p[1] for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    sxz = sum(p[0] * p[2] for p in pts); syz = sum(p[1] * p[2] for p in pts)
    M = [[n, sx, sy, sz], [sx, sxx, sxy, sxz], [sy, sxy, syy, syz]]
    for c in range(3):
        piv = max(range(c, 3), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        for r in range(3):
            if r != c and M[c][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, 4):
                    M[r][k] -= f * M[c][k]
    return [M[k][3] / M[k][k] if M[k][k] else 0.0 for k in range(3)]


def ink_mask(w, h, gray, blue, plane, cue="dark", thr=None, blue_thr=22):
    """cue: dark = 比背景暗；chroma = 蓝度；union = 两者取或。"""
    a, b, c = plane
    m = bytearray(w * h)
    for y in range(h):
        row = y * w
        bg = a + b * 0 + c * y
        for x in range(w):
            p = row + x
            dark = gray[p] < (bg + b * x) - (thr if thr is not None else max(6, (bg + b * x) // 20))
            bluish = blue[p] > blue_thr
            hit = dark if cue == "dark" else (bluish if cue == "chroma" else (dark or bluish))
            if hit:
                m[p] = 1
    return m


def morph(m, w, h, op, r=1):
    """3x3（r=1）腐蚀/膨胀各一遍，够用。op: 'erode' | 'dilate'。"""
    out = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            hit = 0
            for dy in range(-r, r + 1):
                yy = y + dy
                if yy < 0 or yy >= h:
                    continue
                for dx in range(-r, r + 1):
                    xx = x + dx
                    if xx < 0 or xx >= w:
                        continue
                    if m[yy * w + xx]:
                        hit = 1
                        break
                if hit:
                    break
            if op == "dilate":
                out[y * w + x] = hit
            else:
                # 腐蚀：窗口内全为 1
                allone = 1
                for dy in range(-r, r + 1):
                    yy = y + dy
                    for dx in range(-r, r + 1):
                        xx = x + dx
                        if 0 <= xx < w and 0 <= yy < h and not m[yy * w + xx]:
                            allone = 0
                out[y * w + x] = allone
    return out


def components(m, w, h, border_ok=False):
    """4 连通；return [(area, bbox)]，按面积降序。"""
    seen = bytearray(w * h)
    comps = []
    for start in range(w * h):
        if not m[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        area = 0
        pixels = []
        x0 = x1 = start % w
        y0 = y1 = start // w
        touches = False
        while stack:
            p = stack.pop()
            area += 1
            pixels.append(p)
            x, y = p % w, p // w
            if x < x0: x0 = x
            if x > x1: x1 = x
            if y < y0: y0 = y
            if y > y1: y1 = y
            if x == 0 or y == 0 or x == w - 1 or y == h - 1:
                touches = True
            for q in (p - 1 if x else -1, p + 1 if x < w - 1 else -1, p - w if y else -1, p + w if y < h - 1 else -1):
                if q >= 0 and m[q] and not seen[q]:
                    seen[q] = 1
                    stack.append(q)
        if touches and not border_ok:
            continue
        comps.append((area, (x0, y0, x1, y1), pixels))
    comps.sort(key=lambda c: -c[0])
    return comps


def comp_mask(w, h, pixels):
    out = bytearray(w * h)
    for p in pixels:
        out[p] = 1
    return out


def trim_bbox(cm, w, h, bbox, cov=0.5):
    """按覆盖率裁掉光晕/残影造成的毛边。"""
    x0, y0, x1, y1 = bbox
    rows, cols = {}, {}
    for y in range(y0, y1 + 1):
        c = sum(cm[y * w + x] for x in range(x0, x1 + 1))
        if c:
            rows[y] = c
    for x in range(x0, x1 + 1):
        c = sum(cm[y * w + x] for y in range(y0, y1 + 1))
        if c:
            cols[x] = c
    if not rows or not cols:
        return bbox
    rmax, cmax = max(rows.values()), max(cols.values())
    ys = [y for y, c in rows.items() if c >= cov * rmax]
    xs = [x for x, c in cols.items() if c >= cov * cmax]
    return (min(xs), min(ys), max(xs), max(ys))


def fit_edge(m, w, h, bbox, side, frac=(0.2, 0.8)):
    """在中间 frac 段里逐行取最左/最右 1 像素，最小二乘拟合 x = a*y + b。"""
    x0, y0, x1, y1 = bbox
    pts = []
    ya, yb = int(y0 + (y1 - y0) * frac[0]), int(y0 + (y1 - y0) * frac[1])
    for y in range(ya, yb + 1):
        row = y * w
        if side == "left":
            rng = range(x0, x1 + 1)
        else:
            rng = range(x1, x0 - 1, -1)
        for x in rng:
            if m[row + x]:
                pts.append((y, x))
                break
    if len(pts) < 5:
        return None
    n = len(pts)
    sy = sum(p[0] for p in pts)
    sx = sum(p[1] for p in pts)
    syy = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    den = n * syy - sy * sy
    if den == 0:
        return None
    a = (n * sxy - sy * sx) / den          # dx/dy
    b = (sx - a * sy) / n
    return a, b, len(pts)


def detect_gel(path, longest=700, cue="dark", rect=None, thr=None):
    """→ dict：原图坐标下的胶面矩形 + 角度 + 分析尺度信息。"""
    W0, H0 = identify(path)
    w, h, rgb = read_ppm(path, longest)
    gray = to_gray(w, h, rgb)
    blue = blue_cue(w, h, rgb)
    plane = bg_plane(w, h, gray)[:3]
    cross = {}
    for name in ("dark", "chroma"):
        mm = morph(morph(ink_mask(w, h, gray, blue, plane, name, thr), w, h, "erode"), w, h, "dilate")
        cc = components(mm, w, h)
        cross[name] = cc[0][1] if cc else None
        cross[name + "_area_frac"] = round(cc[0][0] / float(w * h), 4) if cc else None
    if rect is not None:
        s = W0 / float(w)
        return {"image": (W0, H0), "analysis": (w, h), "scale": s, "cross_cue": cross,
                "bbox_analysis": tuple(int(round(v / s)) for v in rect), "rect_orig": rect,
                "angle_vertical": 0.0, "edge_fits": {}, "override": True}
    m = morph(morph(ink_mask(w, h, gray, blue, plane, cue, thr), w, h, "erode"), w, h, "dilate")
    comps = components(m, w, h)
    if not comps:
        raise RuntimeError("没找到胶面：没有非背景的连通区域（换 --cue chroma / 调 --ink-thr / 给 --rect）")
    area, bbox, pixels = comps[0]
    cm = comp_mask(w, h, pixels)
    bbox = trim_bbox(cm, w, h, bbox)
    edges = {s: fit_edge(cm, w, h, bbox, s) for s in ("left", "right")}
    # 竖直边斜率 dx/dy → 需要转多少度把它转正（magick 正数=顺时针）
    slopes = [e[0] for e in edges.values() if e]
    ang_v = -math.degrees(math.atan(sum(slopes) / len(slopes))) if slopes else 0.0
    s = W0 / float(w)
    rect = (bbox[0] * s, bbox[1] * s, (bbox[2] + 1) * s, (bbox[3] + 1) * s)
    return {
        "image": (W0, H0),
        "analysis": (w, h),
        "scale": s,
        "area_frac": round(area / float(w * h), 4),
        "bbox_analysis": bbox,
        "cue": cue,
        "bg_plane": [round(v, 4) for v in plane],
        "cross_cue": cross,
        "rect_orig": rect,
        "angle_vertical": ang_v,
        "edge_fits": {k: (round(v[0], 5), round(v[1], 2), v[2]) for k, v in edges.items() if v},
    }


# ---------------------------------------------------------------- 泳道 / 条带


def clusters(prof, idx0, thr, gap=1):
    """连续 >= thr 的 run → [(加权中心, 宽度, 该 run 的峰值)]。"""
    runs, cur = [], []
    for k, v in enumerate(prof):
        if v >= thr:
            cur.append((k + idx0, v))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        tot = sum(v for _, v in r)
        c = sum(x * v for x, v in r) / tot if tot else r[0][0]
        out.append((c, len(r), max(v for _, v in r)))
    return out


def merge_near(runs, d=3.0):
    """把被拆成几段的同一条带合回来（相邻中心 < d px）。"""
    out = []
    for c, w, pk in runs:
        if out and c - out[-1][0] < d:
            pc, pw, ppk = out[-1]
            tot = pw + w
            out[-1] = ((pc * pw + c * w) / tot, tot, max(ppk, pk))
        else:
            out.append((c, w, pk))
    return out


def pitch_from_row(m_strict, w, x0, x1, ytop, ybot, need=4):
    """泳道间距：找「条带最密的那一行」，量这一行里各条带中心距的中位数。

    为什么不用整块胶的列剖面量：梯子的条带比一条泳道还宽，列剖面会把相邻泳道糊成一片，
    量出来的间距偏小（实测 14.4 vs 真值 ~17.5），后面按它补位就会多补泳道。
    单行里的条带是彼此分开的，量得准。
    """
    best = None
    for y in range(max(0, ytop), min(ybot + 1, ybot + 1)):
        row = y * w
        cols = [1 if m_strict[row + x] else 0 for x in range(x0, x1 + 1)]
        if sum(cols) < need:
            continue
        cl = clusters(cols, x0, 1)
        cl = merge_near(cl, 3.0)
        if len(cl) > (len(best[1]) if best else 0):
            best = (y, cl)
    if not best or len(best[1]) < 3:
        return 0.0, None
    cx = [c for c, _, _ in best[1]]
    gaps = sorted(b - a for a, b in zip(cx, cx[1:]))
    return gaps[len(gaps) // 2], best[0]


def split_wide(runs, pitch, factor=1.5):
    """一个 run 明显宽于一个间距（几条淡带糊成一片）→ 按间距均分回几条。

    实测：右半边一排单条带在宽松阈值下糊成宽 34 px 的 run（真值 2 条，间距 17），
    不劈开的话后面的补位会按错间距把这片再细分成 7 px 一条。
    """
    if not pitch:
        return runs
    out = []
    for c, wd, pk in runs:
        k = max(1, int(round(wd / pitch)))
        if k == 1 or wd < factor * pitch:
            out.append((c, wd, pk))
            continue
        step = wd / float(k)
        for j in range(k):
            out.append((c - wd / 2.0 + step * (j + 0.5), step, pk))
    return out


def fill_gaps(cx, widths, pitch, tol=0.3, max_k=8):
    """两两实测泳道之间按间距补空位（照片里总可能有一条完全没条带的泳道）。

    补出来的位置标 interp=True，日志与覆盖图都标出来 —— 这是估计值，不是像素证据。
    差值对不上整数格（相对偏差 > tol）就不补，只记下来提示（宁可少补，不要瞎补）。
    """
    marks = [(c, w, False) for c, w in zip(cx, widths)]
    if len(marks) < 2 or not pitch:
        return marks, []
    out, skipped = [], []
    for i in range(len(marks)):
        out.append(marks[i])
        if i + 1 >= len(marks):
            continue
        g = marks[i + 1][0] - marks[i][0]
        k = int(round(g / pitch))
        if k < 2:
            continue
        if k > max_k or abs(g / pitch - k) > tol:
            skipped.append((round(marks[i][0], 1), round(marks[i + 1][0], 1), round(g, 1),
                            round(g / pitch, 2)))
            continue
        for j in range(1, k):
            out.append((marks[i][0] + g * j / k, min(marks[i][1], marks[i + 1][1]), True))
    return out, skipped


def lanes_and_bands(path, rect, angle, longest=700, ignore_top=0.12, ignore_bottom=0.0,
                    cue="dark", thr=None, lane_mad_k=1.5, interp=True, want=None,
                    min_loose_ink=6):
    """在胶面矩形里找泳道 x 与每条泳道的条带行。

    两套判据，别混：
    - **条带行**：胶内中位数 − 3·1.4826·MAD（严格）—— 只要真条带。
    - **泳道**：胶内中位数 − k·1.4826·MAD（宽松，默认 k=1.5）—— 只有一条淡带的泳道也要算进来；
      严格判据会把这种泳道整条漏掉（实测：严格 9 条 / 真值 15 条）。
      宽松判据的噪声用两道闸挡：① 宽 run 劈开、相邻 run 合并；② 这条泳道要在**严格**判据下
      有 ≥2 点一行、或宽松判据下墨量 ≥ min_loose_ink，否则当噪声丢掉。
    - `interp=True`：两两实测泳道之间按间距把完全没像素证据的泳道补出来（标 interp）。
    """
    w, h, rgb = read_ppm(path, longest)
    gray = to_gray(w, h, rgb)
    x0, y0, x1, y1 = [int(round(v)) for v in rect]
    top = y0 + int((y1 - y0) * ignore_top)
    vals = sorted(gray[y * w + x] for y in range(top, y1 + 1) for x in range(x0, x1 + 1))
    n = len(vals) or 1
    med = vals[n // 2] if vals else 0
    mad = sorted(abs(v - med) for v in vals)[n // 2] if vals else 0
    band_thr = med - max(8, 3.0 * 1.4826 * mad)
    lane_thr = thr if thr is not None else med - max(3, lane_mad_k * 1.4826 * mad)

    def mask_at(t):
        m = bytearray(w * h)
        for y in range(max(0, top), min(h, y1 + 1)):
            row = y * w
            for x in range(max(0, x0), min(w, x1 + 1)):
                if gray[row + x] < t:
                    m[row + x] = 1
        return m

    ytop, ybot = top, y1 - int((y1 - y0) * ignore_bottom)
    m_loose, m_strict = mask_at(lane_thr), mask_at(band_thr)
    cols = [0] * (x1 - x0 + 2)
    for y in range(max(0, ytop), min(h, ybot + 1)):
        row = y * w
        for x in range(max(0, x0), min(w, x1 + 1)):
            if m_loose[row + x]:
                cols[x - x0] += 1
    cmax = max(cols) or 1
    pitch, pitch_row = pitch_from_row(m_strict, w, x0, x1, ytop, ybot)
    runs = merge_near(clusters(cols, x0, max(2, int(0.05 * cmax))), 3.0)
    if pitch:
        runs = merge_near(runs, 0.7 * pitch)      # 同一条泳道被拆成两段（梯子中间有暗缝）合回来
    runs = split_wide(runs, pitch)

    def evidence(c, wd):
        """这条泳道有没有墨，且墨是不是「一条带」而不是「一道竖纹」。

        ① 严格判据下有一行 ≥2 点 → 有真条带；
        ② 否则看宽松判据的行剖面形状：峰 ≥3 点、且峰比中位数高 ≥2 → 是一条带（淡带也算）；
           胶面自身的竖纹是**均匀**的（峰 ≈ 中位数），会被这一条挡掉（实测踩过：竖纹造出幽灵泳道）。
        """
        half = max(3, int(wd))
        xs = list(range(max(0, int(c - half)), min(w, int(c + half) + 1)))
        prof_s, prof_l = [], []
        for y in range(max(0, ytop), min(h, ybot + 1)):
            row = y * w
            prof_s.append(sum(1 for x in xs if m_strict[row + x]))
            prof_l.append(sum(1 for x in xs if m_loose[row + x]))
        if max(prof_s) >= 2:
            return True
        srt = sorted(prof_l)
        if not srt or srt[-1] < 3:
            return False
        return srt[-1] - srt[len(srt) // 2] >= 2

    lanes = [(c, wd) for c, wd, pk in runs if evidence(c, wd)]
    dropped = len(runs) - len(lanes)
    marks, skipped = fill_gaps([c for c, _ in lanes], [wd for _, wd in lanes], pitch) \
        if interp else ([(c, wd, False) for c, wd in lanes], [])
    if want and len(marks) != want:
        print("!! 声明 %d 条泳道，实测+补位 %d 条（缺的在配置里用 lanes.x_frac 显式给位置）"
              % (want, len(marks)))
    bands = []
    for c, wd, _it in marks:
        half = max(3, int(wd))
        xs = range(max(0, int(c - half)), min(w, int(c + half) + 1))
        prof = [sum(1 for x in xs if m_strict[y * w + x]) for y in range(max(0, ytop), min(h, y1 + 1))]
        bands.append([(r[0], r[1]) for r in clusters(prof, ytop, max(2, int(0.25 * (2 * half))))])
    cx_s = [c for c, _, _ in marks]
    return {"lanes": [(round(c, 1), wd) for c, wd, _ in marks],
            "interp": [it for _, _, it in marks],
            "bands": bands, "pitch": (round(pitch, 2) if pitch else None),
            "y_range": (ytop, y1), "gel_median": med, "band_thr": round(band_thr, 1),
            "lane_thr": round(lane_thr, 1), "dropped": dropped, "skipped": skipped,
            "pitch_row": pitch_row,
            "measured": sum(1 for _, _, it in marks if not it)}


# ---------------------------------------------------------------- 旋转 / 裁切


def rotate_pt(x, y, cx, cy, ang_deg):
    a = math.radians(ang_deg)
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = x - cx, y - cy
    return cx + dx * ca - dy * sa, cy + dx * sa + dy * ca


def rotated_rect_bbox(rect, angle, size, size_after):
    """rect=(x0,y0,x1,y1) 原图坐标；angle 用 magick -rotate 语义；返回旋转后坐标系里的轴对齐框。"""
    W0, H0 = size
    W1, H1 = size_after
    cx, cy = W0 / 2.0, H0 / 2.0
    nx, ny = W1 / 2.0, H1 / 2.0
    xs, ys = [], []
    for px, py in ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])):
        a = math.radians(angle)
        ca, sa = math.cos(a), math.sin(a)
        dx, dy = px - cx, py - cy
        rx, ry = dx * ca - dy * sa, dx * sa + dy * ca
        xs.append(rx + nx)
        ys.append(ry + ny)
    return (min(xs), min(ys), max(xs), max(ys)), (W1, H1)


def load_dims_after(path, angle):
    W0, H0 = identify(path)
    if abs(angle) < 1e-6:
        return (W0, H0), (W0, H0)
    W1, H1 = identify(path, ["-rotate", "%.4f" % angle])
    return (W0, H0), (W1, H1)


# ---------------------------------------------------------------- 渲染


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
            cmd += ["-strokewidth", "%.1f" % max(1.0, px * 0.07), "-fill", outline,
                    "-annotate", "+%d+%d" % (int(round(x)), int(round(y))), text]
        cmd += ["-stroke", "none"]
    for x, y, text, size_pt, color in items:
        px = max(6, int(round(size_pt)))            # pointsize=pt 在 72 dpi 下就是 px
        cmd += ["-fill", color, "-annotate", "+%d+%d" % (int(round(x)), int(round(y))), text]
    return cmd


def demo_configs():
    return {
        "markers": {
            "size": 9, "color": "#000000", "gap": 4, "marker_lane": 1,
            "labels": [{"text": "250"}, {"text": "150"}],
        },
        "lanes": {"size": 9, "color": "#000000", "gap": 6, "align": "center", "labels": ["M", "1"]},
    }


def _sibling(rel):
    """同一个 pack 里隔壁 skill 的文件（预设、label_gel.py）。只单独装本 skill 时找不到。"""
    skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # <skill>/
    pack = os.path.dirname(skill)
    for cand in (os.path.join(skill, rel), os.path.join(pack, "doing-sds-page", rel)):
        if os.path.exists(cand):
            return cand
    return None


_LABEL_GEL = []


def label_gel():
    """隔壁 doing-sds-page 的 label_gel.py，拿来复用（lane_pitch / 配置口径）。

    两边都只依赖 stdlib，所以直接按路径 import；找不到就返回 None，
    lane_pitch 走本文件里的等价实现（见 pitch_of_lanes）。
    """
    if _LABEL_GEL:
        return _LABEL_GEL[0]
    path = _sibling(os.path.join("scripts", "label_gel.py"))
    mod = None
    if path:
        import importlib.util
        spec = importlib.util.spec_from_file_location("label_gel_shared", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    _LABEL_GEL.append(mod)
    return mod


def lane_pitch(lane_x):
    """泳道带宽 = 相邻泳道中心距的中位数（与 label_gel.py 同一函数；那里找不到就本地算）。"""
    mod = label_gel()
    if mod is not None:
        return mod.lane_pitch(lane_x)
    if len(lane_x) < 2:
        return 0.0
    gaps = sorted(b - a for a, b in zip(lane_x, lane_x[1:]))
    return gaps[len(gaps) // 2]


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


def load_cfg(path, what):
    """与隔壁 label_gel.py 同一套 schema 与报错口径（字段名、含义一致）。"""
    if not os.path.exists(path):
        raise RuntimeError("找不到%s：%s（用 --markers / --lanes 指到你的配置）" % (what, path))
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    labels = cfg.get("labels") or []
    if not labels:
        raise RuntimeError("%s 里没有 labels" % path)
    return cfg, labels


def as_entry(e, defaults):
    """labels 项可以是字符串，也可以是 {text,size,color,font}（同 label_gel.py）。"""
    if isinstance(e, str):
        return {"text": e}
    return dict(e)


def pos_or_frac(v, gel_a, gel_b, scale=1.0):
    """配置里的位置：<1 当「占胶面宽度的比例」，≥1 当输出像素（两种写法都能用，见文档）。"""
    v = float(v)
    return (gel_a + v * (gel_b - gel_a)) * scale if v < 1 else v


def preset_dir_candidates():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [p for p in (os.path.join(here, "assets", "markers"),
                        os.path.join(os.path.dirname(here), "doing-sds-page", "assets", "markers"))]


def gel_dir_candidates():
    skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(skill, "assets", "gels")]


def load_gel_preset(name):
    """读 assets/gels/<名>.json：胶体物理尺寸（cm）。width_cm 定标，height_cm 只核对。"""
    for d in gel_dir_candidates():
        p = os.path.join(d, name + ".json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
            if not cfg.get("width_cm"):
                raise RuntimeError("%s 里没有 width_cm" % p)
            return cfg, p
    have = []
    for d in gel_dir_candidates():
        if os.path.isdir(d):
            have += sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith(".json"))
    raise RuntimeError("没有这个胶体预设：%s（可选：%s）" % (name, "、".join(have) or "无"))


def parse_gel_cm(s):
    v = [float(t) for t in s.lower().replace("x", " x ").replace("*", " x ").split() if t != "x"]
    if len(v) != 2:
        raise argparse.ArgumentTypeError("--gel-cm 要写成 宽x高，如 8.6x7.2（cm）")
    return {"width_cm": v[0], "height_cm": v[1], "_source": "--gel-cm 命令行给定"}


def load_preset(name):
    for d in preset_dir_candidates():
        p = os.path.join(d, name + ".json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f), p
    raise RuntimeError("找不到预设 %s；找过:\n  %s" % (name, "\n  ".join(preset_dir_candidates())))


# ---------------------------------------------------------------- 主流程


def _rect_arg(s):
    if not s:
        return None
    v = [float(t) for t in s.replace(",", " ").split()]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("--rect 要 4 个数：x0,y0,x1,y1（原图像素）")
    return tuple(v)


def cmd_probe(args):
    info = detect_gel(args.image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("cross_cue"):
        print("两条判据的胶框（分析尺度）: dark=%s / chroma=%s" % (
            info["cross_cue"].get("dark"), info["cross_cue"].get("chroma")))
    ang = args.angle if args.angle is not None else info["angle_vertical"]
    lb = lanes_and_bands(args.image, info["bbox_analysis"], ang, args.longest,
                         cue=args.cue, thr=args.ink_thr)
    print("角度: 竖边拟合 %.3f°；本次用 %.3f°" % (info["angle_vertical"], ang))
    print("判据：胶内中位数 %s；条带行阈值 %.1f（严格）· 泳道阈值 %.1f（宽松，淡带也算）" % (
        lb["gel_median"], lb["band_thr"], lb["lane_thr"]))
    print("间距估计 %.1f px（量自第 %s 行：条带最密的一行）；噪声候选丢掉 %d 条；差值对不上整数格未补 %s" % (
        lb["pitch"] or 0, lb.get("pitch_row"), lb["dropped"], lb["skipped"] or "无"))
    print("泳道数 %d（实测 %d + 补位 %d），中心 x（分析尺度）: %s" % (
        len(lb["lanes"]), lb["measured"], len(lb["lanes"]) - lb["measured"],
        [c for c, _ in lb["lanes"]]))
    if len(lb["lanes"]) > lb["measured"]:
        print("  补位（无像素证据，按相邻间距均分）: %s" % (
            [c for (c, _), it in zip(lb["lanes"], lb["interp"]) if it]))
    for i, ((c, wid), bands) in enumerate(zip(lb["lanes"], lb["bands"]), 1):
        print("  泳道 %2d x=%7.1f 宽=%3d 条带 %2d 行: %s" % (
            i, c, wid, len(bands), [int(b[0]) for b in bands]))
    if args.debug:
        draw_debug(args.image, info, lb, args.debug)
        print("debug 覆盖图:", args.debug)
    return 0


def cmd_render(args):
    info = detect_gel(args.image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
    ang = args.angle if args.angle is not None else round(info["angle_vertical"], 3)
    if args.no_rotate:
        ang = 0.0
    (W0, H0), (W1, H1) = load_dims_after(args.image, ang)

    # 两个配置：schema 与隔壁 label_gel.py 一致（字段名与含义都对齐，见 references）
    if args.preset:
        mk, _preset_path = load_preset(args.preset)
        mk_labels = mk["labels"]
        print("marker 预设 %s ← %s" % (args.preset, _preset_path))
    elif args.markers:
        mk, mk_labels = load_cfg(args.markers, "marker 配置")
    else:
        raise RuntimeError("要 --markers <json> 或 --preset <名>（render --list-presets 看有哪些）")
    ln = ln_labels = None
    if args.lanes:
        ln, ln_labels = load_cfg(args.lanes, "泳道配置")
    font = args.font or pick_font(mk.get("font") or (ln or {}).get("font"))
    dpi = args.dpi
    gel = parse_gel_cm(args.gel_cm) if args.gel_cm else load_gel_preset(args.gel)[0]
    print("[i] 胶体预设 %s：宽 %.2f cm × 高 %.2f cm（%s）；输出 %d dpi" % (
        gel.get("name", "命令行"), gel["width_cm"], gel["height_cm"], gel.get("_source", ""), dpi))

    gel_bb, _ = rotated_rect_bbox(info["rect_orig"], ang, (W0, H0), (W1, H1))
    # 定标：让胶面横跨 width_cm/2.54×dpi 个像素 —— 成品 PNG 就是**实际尺寸**
    scale = (gel["width_cm"] / 2.54 * dpi) / (gel_bb[2] - gel_bb[0])
    implied_h = gel["width_cm"] * (gel_bb[3] - gel_bb[1]) / (gel_bb[2] - gel_bb[0])
    if abs(implied_h - gel["height_cm"]) > 0.10 * gel["height_cm"]:
        print("[!] 探测框的比例推出来高度 %.2f cm，预设写 %.2f cm（差 %.0f%%）—— "
              "按宽度定标不受影响；要按你的数就用 --gel-cm，或调 --margin-frac / --rect。"
              % (implied_h, gel["height_cm"], abs(implied_h - gel["height_cm"]) / gel["height_cm"] * 100))
    a2o = info["image"][0] / float(info["analysis"][0])
    ymid = (info["bbox_analysis"][1] + info["bbox_analysis"][3]) / 2.0

    def rot_pt(x, y):
        """原图坐标 → 旋转后画布坐标（与 magick -rotate 同一套矩阵）。"""
        rx, ry = rotate_pt(x, y, W0 / 2.0, H0 / 2.0, ang)
        return rx + (W1 - W0) / 2.0, ry + (H1 - H0) / 2.0

    lb = lanes_and_bands(args.image, info["bbox_analysis"], ang, args.longest,
                         cue=args.cue, thr=args.ink_thr,
                         want=len(ln_labels) if ln_labels else None)
    lane_x = [rot_pt(c * a2o, ymid * a2o)[0] for c, _ in lb["lanes"]]
    lane_w = [wd * a2o for _, wd in lb["lanes"]]
    print("[i] 泳道 %d 条（实测 %d + 补位 %d），间距 %.1f px(分析) ; %s" % (
        len(lane_x), lb["measured"], len(lane_x) - lb["measured"], lb["pitch"] or 0,
        font or "magick 默认字体"))

    # ---- 泳道名（schema 同 label_gel.py：gap = 文字底边离胶面顶边，align 相对泳道中心）
    items = []
    if ln_labels:
        size = float(ln.get("size", 16))          # px（成品像素；label_gel 那边是画布 pt）
        gap = float(ln.get("gap", 0))
        align = ln.get("align", "center")
        color = ln.get("color", "#000000")
        xf = ln.get("x_frac")
        if xf:
            if len(xf) != len(ln_labels):
                raise RuntimeError("lanes.x_frac 有 %d 个，labels 有 %d 个" % (len(xf), len(ln_labels)))
            xs = [pos_or_frac(f, gel_bb[0], gel_bb[2]) for f in xf]
        else:
            if len(ln_labels) > len(lane_x):
                raise RuntimeError("lanes.labels 有 %d 条，只探到 %d 条泳道（缺的用 lanes.x_frac 显式给位置）"
                                   % (len(ln_labels), len(lane_x)))
            xs = lane_x[: len(ln_labels)]
        for e, x in zip(ln_labels, xs):
            en = as_entry(e, ln)
            t = en.get("text", "")
            if not t:
                continue
            s = float(en.get("size", size))
            tw, _th = text_extent(font, round(s), t)
            tw = tw / scale
            ax = x - tw / 2.0 if align == "center" else (x if align == "left" else x - tw)
            ay = gel_bb[1] - gap / scale - 0.21 * s / scale               # 底边落在胶顶之上 gap
            items.append((ax, ay, t, s, en.get("color", color)))

    # ---- marker 分子量（schema 同 label_gel.py：marker_lane / marker_lane_x / lane_width /
    #      lane_edge=inner / gap = 文字与泳道**带边**的间距；左右按泳道区中线判）
    if mk_labels:
        lw = float(mk["lane_width"]) if mk.get("lane_width") else lane_pitch(lane_x)
        if mk.get("marker_lane_x") is not None:
            mx = pos_or_frac(mk["marker_lane_x"], gel_bb[0], gel_bb[2])
        else:
            idx = int(mk.get("marker_lane", 1))
            if idx == 0:
                mx = lane_x[0] - lw
            elif 1 <= idx <= len(lane_x):
                mx = lane_x[idx - 1]
            else:
                raise RuntimeError("markers 的 marker_lane 超出泳道数量（探到 %d 条）" % len(lane_x))
        mid = (min(lane_x) + max(lane_x)) / 2.0
        left_side = mx < mid
        edge = mx if mk.get("lane_edge") == "inner" else (mx - lw / 2.0 if left_side else mx + lw / 2.0)
        gap = float(mk.get("gap", 4))                  # px
        size = float(mk.get("size", 16))               # px
        print("[i] 泳道带宽 ≈%.1f px(分析)（%s）；marker 泳道 x=%.1f（泳道区中线 %.1f 的%s边）"
              "→ 带边 %.1f → %s对齐，锚点离带边 %g px（成品像素）" % (
                  lw / a2o, "来自 markers.lane_width" if mk.get("lane_width") else
                  "相邻泳道中心距的中位数", mx, mid, "左" if left_side else "右", edge,
                  "右" if left_side else "左", float(mk.get("gap", 4))))
        idx = 0
        if mk.get("marker_lane_x") is None:
            idx = int(mk.get("marker_lane", 1)) - 1
            if idx < 0:
                idx = 0
        elif mk.get("marker_lane") is not None:
            idx = int(mk["marker_lane"]) - 1
        else:
            idx = max(range(len(lb["bands"])), key=lambda i: len(lb["bands"][i]))
            print("[i] marker_lane 未给 → 自动选条带最多的第 %d 条（%d 条带）"
                  % (idx + 1, len(lb["bands"][idx])))
        idx = max(0, min(idx, len(lb["bands"]) - 1))
        rows = [b[0] for b in lb["bands"][idx]]
        print("[i] marker 用第 %d 条泳道（x=%.1f，%d 条带）" % (idx + 1, lane_x[idx], len(rows)))
        yf = mk.get("y_frac")
        if yf:
            if len(yf) != len(mk_labels):
                raise RuntimeError("markers.y_frac 有 %d 个，labels 有 %d 个" % (len(yf), len(mk_labels)))
            gy0, gy1 = info["bbox_analysis"][1], info["bbox_analysis"][3]
            rows = [gy0 + f * (gy1 - gy0) for f in yf]
        elif len(rows) != len(mk_labels):
            print("!! 条带数对不上：探测到 %d 条，配置有 %d 个分子量 —— 按位置配前 %d 个（占位）。"
                  % (len(rows), len(mk_labels), min(len(rows), len(mk_labels))))
            print("   要正式出图：给 markers.y_frac 显式写每条带位置，或换对预设后重跑。")
            rows = rows[: len(mk_labels)]
        _, my = rot_pt(info["bbox_analysis"][0] * a2o, rows[0] * a2o) if rows else (0, 0)
        for row, e in zip(rows, mk_labels):
            en = as_entry(e, mk)
            t = en.get("text", "")
            if not t:
                continue
            s = float(en.get("size", size))
            tw, _th = text_extent(font, round(s), t)
            tw = tw / scale
            _, yy = rot_pt(info["bbox_analysis"][0] * a2o, row * a2o)
            ax = (edge - gap / scale - tw) if left_side else (edge + gap / scale)
            ay = yy + 0.35 * s / scale                      # 文字竖直中心对条带
            items.append((ax, ay, t, s, en.get("color", mk.get("color", "#000000"))))

    # ---- 外扩留白（按文字真实包围盒）→ 裁切 → 写字
    base_m = args.margin_frac * max(gel_bb[2] - gel_bb[0], gel_bb[3] - gel_bb[1])
    pad = max(6.0, base_m * 0.4)
    need = {"l": 0.0, "r": 0.0, "t": 0.0, "b": 0.0}
    for ax, ay, t, s, _c in items:
        tw, th = text_extent(font, round(s), t)
        tw, th = tw / scale, th / scale
        need["l"] = max(need["l"], gel_bb[0] - ax)
        need["r"] = max(need["r"], ax + tw - gel_bb[2])
        need["t"] = max(need["t"], gel_bb[1] - (ay - 0.79 * s / scale))
        need["b"] = max(need["b"], (ay + 0.21 * s / scale) - gel_bb[3])
    L = base_m + max(0.0, need["l"] - base_m + pad)
    R = base_m + max(0.0, need["r"] - base_m + pad)
    T = base_m + max(0.0, need["t"] - base_m + pad)
    B = base_m + max(0.0, need["b"] - base_m + pad)
    bx0, by0 = max(0.0, gel_bb[0] - L), max(0.0, gel_bb[1] - T)
    bx1, by1 = min(float(W1), gel_bb[2] + R), min(float(H1), gel_bb[3] + B)
    crop_w, crop_h = bx1 - bx0, by1 - by0

    base = [MAGICK, args.image, "-auto-orient"]
    if abs(ang) > 1e-6:
        base += ["-rotate", "%.4f" % ang]
    base += ["-crop", "%dx%d+%d+%d" % (int(round(crop_w)), int(round(crop_h)),
                                       int(round(bx0)), int(round(by0))), "+repage"]
    if abs(scale - 1.0) > 1e-9:
        base += ["-resize", "%dx%d" % (int(round(crop_w * scale)), int(round(crop_h * scale)))]
    base += ["+profile", "*"]        # 注意别用 -strip：IM7 里它会把后面设的 dpi 一起丢掉（实测）
    out_items = [((ax - bx0) * scale, (ay - by0) * scale, t, s, c) for ax, ay, t, s, c in items]
    cmd = annotate_cmd(base, out_items, font, None if args.no_outline else args.outline)
    cmd += ["-units", "PixelsPerInch", "-density", str(dpi), args.out]
    _run(cmd)
    gel_w_px, gel_h_px = (gel_bb[2] - gel_bb[0]) * scale, (gel_bb[3] - gel_bb[1]) * scale
    print("输出 %s（%d×%d px, %d dpi；%d 条文字）" % (
        args.out, int(round(crop_w * scale)), int(round(crop_h * scale)), dpi, len(out_items)))
    print("[i] 实际尺寸：胶面 %.2f×%.2f cm（%d×%d px）· 成品 %.2f×%.2f cm（含标签留白）" % (
        gel["width_cm"], implied_h, int(round(gel_w_px)), int(round(gel_h_px)),
        crop_w * scale / dpi * 2.54, crop_h * scale / dpi * 2.54))
    if args.debug:
        draw_debug(args.image, info, lb, args.debug)
        print("debug 覆盖图:", args.debug)
    return 0


def draw_debug(image, info, lb, out, crop=None, scale=1.0):
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
    cmd += ["-resize", "1200x1200", out]
    _run(cmd)


# ---------------------------------------------------------------- doctor


# 各平台**推荐**位置（只是推荐，不是本机的值；本机选过的位置记在本机，见 env_root()）
ENV_ROOTS = {
    "darwin": ["/Applications/DoingSDSPage", "~/DoingSDSPage"],
    "win32": ["C:\\ProgramData\\envs\\DoingSDSPage", "~/DoingSDSPage"],
    "linux": ["~/Applications/DoingSDSPage", "~/DoingSDSPage"],
}

ENV_CFG = "~/.config/doing-sds-page-photo/env.json"


def env_root():
    """本机选定的环境安装位置 → (路径, 来源)。

    优先级：`--env-root`（调用方自己传）> 环境变量 `GEL_PHOTO_ENV_ROOT` > 本机配置文件
    `~/.config/doing-sds-page-photo/env.json` 的 `env_root` > 没有（那就得问用户）。

    这台机器选的位置**不进 skill**：skill 里只有推荐表和「先问、记在本机、以后读出来」的流程，
    换机器/换人重新问一次即可。
    """
    v = os.environ.get("GEL_PHOTO_ENV_ROOT")
    if v:
        return os.path.abspath(os.path.expanduser(v)), "环境变量 GEL_PHOTO_ENV_ROOT"
    p = os.path.expanduser(ENV_CFG)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                v = (json.load(f) or {}).get("env_root")
            if v:
                return os.path.abspath(os.path.expanduser(v)), p
        except Exception as e:
            return None, "配置文件 %s 读不了：%s" % (p, e)
    return None, None


def cmd_doctor(args):
    """环境自检：能跑就一行行打出来，缺什么直接说怎么补。"""
    import platform
    import shutil
    bad = 0
    print("python : %s (%s)" % (sys.executable, platform.python_version()))
    mg = shutil.which(MAGICK)
    if not mg:
        print("magick : 缺 —— ImageMagick 7 没装（macOS: brew install imagemagick / "
              "Windows: winget install ImageMagick.ImageMagick / Linux: apt install imagemagick）")
        bad += 1
    else:
        ver = _run([MAGICK, "-version"], False).strip().splitlines()[0]
        print("magick : %s\n         %s" % (mg, ver))
        if "ImageMagick 7" not in ver:
            print("         !! 本 skill 按 ImageMagick 7 的用法写的（-annotate/-rotate 行为），6.x 请自测")
    f = pick_font()
    print("字体   : %s" % (f or "没找到 —— 用 --font 指一个 .ttf/.ttc"))
    if not f:
        bad += 1
    found = [d for d in preset_dir_candidates() if os.path.isdir(d)]
    print("预设   : %s" % (found[0] if found else
                          "没找到预设目录（只装了本 skill？用 --markers 指自己的 JSON）"))
    key = "darwin" if sys.platform == "darwin" else ("win32" if os.name == "nt" else "linux")
    root, src_ = (os.path.abspath(os.path.expanduser(args.env_root)), "--env-root") if args.env_root else env_root()
    if root:
        if os.path.isdir(root):
            print("环境根 : %s  存在，%s（来源：%s）" % (root, "可写" if os.access(root, os.W_OK) else "不可写", src_))
            if not os.access(root, os.W_OK):
                bad += 1
        else:
            print("环境根 : %s  不存在 —— 要先建（mkdir -p）再往里装东西（来源：%s）" % (root, src_))
            bad += 1
    else:
        print("环境根 : 本机还没定。本 skill 只需系统 python3 + ImageMagick，**不需要**往环境根装东西；"
              "要记就先问用户用哪个位置（推荐：%s），\n"
              "         然后把答案记在本机（\n"
              "           mkdir -p ~/.config/doing-sds-page-photo && \n"
              "           printf '{\"env_root\": \"<用户选的位置>\"}' > ~/.config/doing-sds-page-photo/env.json\n"
              "         ），**不要写进 skill**。" % " 或 ".join(ENV_ROOTS[key]))
        if src_:
            print("         （%s）" % src_)
    print("selftest: 跑 `python3 %s selftest` 验算法（10 项）" % os.path.basename(__file__))
    return 1 if bad else 0


# ---------------------------------------------------------------- selftest


def selftest():
    import tempfile
    ok = 0

    # 1) PPM 解析
    d = b"P6\n# c\n3 1\n255\n" + bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])
    w, h, body = _parse_ppm_for_test(d)
    assert (w, h, len(body)) == (3, 1, 9), (w, h, len(body))
    ok += 1

    # 2) magick -rotate 方向 + 本项目 rotate_pt 的矩阵一致（实测锚定，不是猜的）
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "s.png")
        _run([MAGICK, "-size", "200x100", "xc:white", "-fill", "black",
              "-draw", "rectangle 0,0 20,10", src])
        rot = os.path.join(td, "r.png")
        _run([MAGICK, src, "-rotate", "10", rot])
        w, h, rgb = read_ppm(rot, None)
        g = to_gray(w, h, rgb)
        xs = [i % w for i in range(w * h) if g[i] < 100]
        ys = [i // w for i in range(w * h) if g[i] < 100]
        cxm, cym = sum(xs) / len(xs), sum(ys) / len(ys)
        # 原图中心 (100,50)、(20,10) 矩形中心 (10,5)；新画布中心 = (w/2, h/2)
        ex, ey = rotate_pt(10, 5, 100, 50, 10)
        ex, ey = ex + (w / 2.0 - 100.0), ey + (h / 2.0 - 50.0)
        assert abs(cxm - ex) < 2.0 and abs(cym - ey) < 2.0, ("rotate 方向/矩阵不符", (cxm, cym), (ex, ey))
        ok += 1

    # 3) 峰簇：三个 run 的中心与宽度
    prof = [0, 0, 5, 9, 5, 0, 0, 0, 4, 4, 0, 7, 7, 7]
    cl = clusters(prof, 0, 4)
    assert len(cl) == 3, cl
    assert abs(cl[0][0] - 3) < 0.2 and cl[1][1] == 2 and cl[2][0] > cl[1][0], cl
    ok += 1

    # 4) 平面背景 + 暗度判据：亮背景上的暗块进 mask，块外不进；块大到块内没有背景像素也要成立
    w, h = 300, 300
    gray = bytearray([205] * (w * h))
    for y in range(60, 240):
        for x in range(60, 240):
            gray[y * w + x] = 180          # 假胶：比背景暗 25 灰阶
    blue = bytearray(w * h)
    plane = bg_plane(w, h, gray)[:3]
    m = ink_mask(w, h, gray, blue, plane, "dark")
    assert m[150 * w + 150] == 1 and m[10 * w + 10] == 0, (plane, m[150 * w + 150], m[10 * w + 10])
    assert m[62 * w + 62] == 1 and m[58 * w + 150] == 0
    # 蓝度判据：灰胶的蓝度≈0 ⇒ chroma 通道不该选中它
    m2 = ink_mask(w, h, gray, blue, plane, "chroma")
    assert m2[150 * w + 150] == 0
    ok += 1

    # 5) 连通域：最大的块胜出、贴边的不算、像素表对得上面积
    m = bytearray(w * h)
    for y in range(30, 70):
        for x in range(30, 70):
            m[y * w + x] = 1
    for x in range(0, 40):
        m[0 * w + x] = 1          # 贴边
    comps = components(m, w, h)
    assert comps[0][1] == (30, 30, 69, 69) and comps[0][0] == len(comps[0][2]) == 1600, comps[0][:2]
    ok += 1

    # 6) 旋转框 bbox 里 → 裁切后胶面仍是正的（用 magick 实跑校验：旋转 8° 的黑框 bbox 与预测差 < 3 px）
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "s2.png")
        _run([MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(40)",
              "-draw", "rectangle 120,90 320,240", src])
        info = detect_gel(src, 400)
        (W0, H0), (W1, H1) = load_dims_after(src, info["angle_vertical"])
        bb, _ = rotated_rect_bbox(info["rect_orig"], info["angle_vertical"], (W0, H0), (W1, H1))
        out = os.path.join(td, "o.png")
        cmd = [MAGICK, src, "-rotate", "%.4f" % info["angle_vertical"],
               "-crop", "%dx%d+%d+%d" % (int(bb[2] - bb[0]), int(bb[3] - bb[1]), int(bb[0]), int(bb[1])),
               "+repage", "-colorspace", "gray", "-depth", "8", "pgm:-"]
        d = _run(cmd)
        parts = d.split(b"\n", 3)
        cw, ch = map(int, parts[1].split())
        px = parts[3]
        dark = [i for i in range(cw * ch) if px[i] < 200]  # noqa: E501
        ys = [i // cw for i in dark]
        assert abs((min(ys) - 0) - 0) <= 3, "裁切上边留白异常 %s" % min(ys)
        assert abs((ch - 1 - max(ys)) - 0) <= 3, "裁切下边留白异常 %s" % (ch - 1 - max(ys))
        ok += 1

    # 7) 写字：dpi 真进 PNG（pHYs），且 pointsize 不被 density 二次放大
    with tempfile.TemporaryDirectory() as td:
        o = os.path.join(td, "t.png")
        _run([MAGICK, "-size", "400x100", "xc:white", "-density", "72", "-font", pick_font(),
              "-pointsize", "20", "-fill", "black", "-annotate", "+10+40", "Ag1y"] +
             ["+profile", "*", "-units", "PixelsPerInch", "-density", "300", o])
        raw = open(o, "rb").read()
        i = raw.find(b"pHYs")
        assert i > 0, "PNG 里没有 pHYs（dpi 没写进去）"
        ppm_x = int.from_bytes(raw[i + 4 : i + 8], "big")
        assert abs(ppm_x - 11811) <= 1, "pHYs 不是 300 dpi: %d" % ppm_x
        w, h, rgb = read_ppm(o, None)
        g = to_gray(w, h, rgb)
        rows = [y for y in range(h) if any(g[y * w + x] < 128 for x in range(w))]
        assert 17 <= rows[-1] - rows[0] + 1 <= 23, "字号被 density 放大了: 墨高 %d px（20 pt 应约 19）" % (
            rows[-1] - rows[0] + 1)
        ok += 1

    # 8) 补位：差值对得上整数格才补，对不上就报出来不补
    marks, skipped = fill_gaps([0, 10, 40], [4, 4, 4], 10)
    assert [round(c, 1) for c, _, _ in marks] == [0, 10, 20, 30, 40], marks
    assert [it for _, _, it in marks] == [False, False, True, True, False]
    assert not skipped
    marks2, skipped2 = fill_gaps([0, 10, 35], [4, 4, 4], 10)
    assert len(marks2) == 3 and len(skipped2) == 1 and skipped2[0][3] == 2.5, (marks2, skipped2)
    ok += 1

    # 9) 间距量自「条带最密的一行」，不量整块胶列剖面：造一行每 20 px 一条带
    w2, h2 = 200, 60
    m = bytearray(w2 * h2)
    for x in (30, 50, 70, 90, 110):
        for y in range(20, 30):
            for dx in range(-2, 3):
                m[y * w2 + x + dx] = 1
    for y in range(40, 45):                      # 另加一条竖纹（不该被当成间距来源）
        m[y * w2 + 30] = 1
    p, row = pitch_from_row(m, w2, 0, w2 - 1, 0, h2 - 1)
    assert abs(p - 20) <= 0.5, (p, row)
    assert row == 20, row
    ok += 1

    # 10) 端到端：5 条泳道（其中一条只有很淡的带）+ 一道竖纹 → 必须正好 5 条，竖纹不算泳道
    with tempfile.TemporaryDirectory() as td:
        src_img = os.path.join(td, "synthetic_gel.png")
        lanes_x = (120, 150, 180, 210, 240)
        cmd = [MAGICK, "-size", "400x300", "xc:white", "-fill", "gray(180)",
               "-draw", "rectangle 100,80 340,260"]
        for i, x in enumerate(lanes_x):
            v = 165 if i == 2 else 120
            cmd += ["-fill", "gray(%d)" % v, "-draw", "rectangle %d,180 %d,190" % (x - 6, x + 6)]
        cmd += ["-fill", "gray(172)", "-draw", "rectangle 300,80 302,260", src_img]   # 竖纹
        _run(cmd)
        lb = lanes_and_bands(src_img, (100, 80, 340, 260), 0.0, longest=400)  # 别放大：rect 是原图坐标
        got = [c for c, _ in lb["lanes"]]
        assert len(got) == 5, ("泳道数不对", got, lb["dropped"])
        for want_x, g in zip(lanes_x, got):
            assert abs(want_x - g) <= 3, (lanes_x, got)
        assert all(abs(g - 301) > 6 for g in got), ("竖纹被当成泳道", got)
        assert lb["measured"] == 5 and not any(lb["interp"])
        ok += 1

    print("selftest %d/10 通过" % ok)
    return 0


def _parse_ppm_for_test(d):
    import io
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
    return int(toks[1]), int(toks[2]), d[i + 1 :]


# ---------------------------------------------------------------- CLI


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--list-presets" in argv:            # 提前拦：这个开关不该被 image/--out 的必填卡住
        for d in preset_dir_candidates():
            if os.path.isdir(d):
                print("\n".join(sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith(".json"))))
                return 0
        print("(没有预设目录)")
        return 0
    ap = argparse.ArgumentParser(description="SDS-PAGE 照片 → 裁切正畸 → 配置标注 → PNG")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_detect(a):
        a.add_argument("--longest", type=int, default=700, help="分析缩略图长边（默认 700）")
        a.add_argument("--cue", choices=("dark", "chroma", "union"), default="dark",
                       help="认胶的判据：dark=比背景暗（通用）/ chroma=蓝度（偏蓝光下的染色胶）/ union")
        a.add_argument("--ink-thr", type=float, default=None, help="dark 判据的阈值（默认 max(6, 背景//20)）")
        a.add_argument("--rect", help="跳过自动找胶，直接给原图像素矩形 x0,y0,x1,y1")

    p = sub.add_parser("probe", help="只探测：胶框/角度/泳道/条带")
    p.add_argument("image")
    p.add_argument("--angle", type=float, default=None, help="强制用这个角度（度，正=顺时针）")
    p.add_argument("--debug", help="出一张带检测结果的覆盖图")
    add_detect(p)
    p.set_defaults(func=cmd_probe)

    r = sub.add_parser("render", help="出成品 PNG")
    r.add_argument("image")
    r.add_argument("--out", required=True)
    r.add_argument("--markers", help="marker 配置 JSON")
    r.add_argument("--preset", help="用内置预设名（先 --list-presets）")
    r.add_argument("--lanes", help="泳道配置 JSON")
    r.add_argument("--dpi", type=int, default=96, help="成品 PNG 的 dpi（默认 96；像素数 = 胶面 cm/2.54×dpi）")
    r.add_argument("--gel", default="mini-gel", help="胶体尺寸预设（assets/gels/*.json，默认 mini-gel）")
    r.add_argument("--gel-cm", help="直接给胶体尺寸 宽x高（cm），如 8.6x7.2，压过 --gel")
    r.add_argument("--angle", type=float, default=None)
    r.add_argument("--no-rotate", action="store_true")
    r.add_argument("--margin-frac", type=float, default=0.05, help="四周外扩（占胶面长边比例，默认 0.05）")
    r.add_argument("--font", help="字体文件路径（默认 Arial；配置里的 font 只在是路径时才用）")
    r.add_argument("--outline", default="#ffffff", help="文字白边颜色（默认 #ffffff，照片上防糊）")
    r.add_argument("--no-outline", action="store_true", help="不要白边")
    r.add_argument("--debug", help="再出一张检测覆盖图")
    r.add_argument("--list-presets", action="store_true")
    add_detect(r)
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("selftest")
    s.set_defaults(func=lambda a: selftest())

    d = sub.add_parser("doctor", help="环境自检：python / magick / 字体 / 预设目录 / 环境安装位置")
    d.add_argument("--env-root", help="你选的环境安装根目录（本 skill 自己的 venv / 便携版 ImageMagick / 缓存放这儿）")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
