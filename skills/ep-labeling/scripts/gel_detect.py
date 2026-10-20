#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认胶面：平面背景拟合 → 暗度/蓝度判据 → 连通域 → 竖边拟合倾角。"""
import math

from magick_io import identify, read_ppm, to_gray


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


def trim_bbox(cm, w, h, bbox, cov=0.5, top_cov=0.9):
    """按覆盖率裁掉光晕/残影造成的毛边。

    上边单独用 top_cov：胶顶不是一条边，是上样孔之间立着的一排「齿」（常被压弯），
    齿区每行的覆盖率随齿形乱跳，50% 那条线落在齿中间、没有物理意义（参考照片上
    就是这样凑出 6.70 cm 的）。第一条**接近满宽**的行才是胶板（孔底）的上缘。
    """
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
    tops = [y for y, c in rows.items() if c >= top_cov * rmax]
    xs = [x for x, c in cols.items() if c >= cov * cmax]
    return (min(xs), min(tops), max(xs), max(ys))


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
        return {"path": path, "image": (W0, H0), "analysis": (w, h), "scale": s, "cross_cue": cross,
                "bbox_analysis": tuple(int(round(v / s)) for v in rect), "rect_orig": rect,
                "angle_vertical": 0.0, "edge_fits": {}, "override": True}
    m = morph(morph(ink_mask(w, h, gray, blue, plane, cue, thr), w, h, "erode"), w, h, "dilate")
    comps = components(m, w, h)
    if not comps:
        # 照片被裁过、胶面顶到画面边时（裁到胶本身就必然如此），严格口径会把**胶**当成
        # 「贴边的背景」整块丢掉 → 退回允许贴边、取最大的那块（它仍是胶：上下那两条暗边都比它小）。
        comps = components(m, w, h, border_ok=True)
        if comps:
            print("[i] 胶面贴着画面边（照片裁过？）—— 退回「允许贴边」取最大块（占画面 %.1f%%）"
                  % (comps[0][0] * 100.0 / (w * h)))
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
        "path": path,
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
