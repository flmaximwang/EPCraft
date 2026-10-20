#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage 3：给定**冻结的泳道 x**，求每条泳道的条带行。

管线是严格串行的（spec §2 决策 3）：胶面 → 泳道 → 条带，每一级只读上一级的冻结输出。
本模块只做第三级：

    stage 2 的冻结输出（每条泳道的 x，分析尺度）
      ├─ 泳道内沿 x 平均的行剖面（ImageJ 的 Plot Profile 口径）
      ├─ top-hat（减宽窗基线）后取局部极大 → (y, 半高宽, 深度)
      └─ 另用更细的栅格扫行，换算回分析尺度（梯子在高分子量端才分得开）

**不回流**：本模块不返回任何泳道位置、不改泳道、不按条带证据丢弃泳道。条带行永远只
向下游（mw_report / 记录 / 出图）走；stage 2 已经冻结，条带结果不参与它的任何判断。
本模块也不 import stage 2/4（lane_fit / lane_select / gel_photo）：stage 3 不能反向依赖。

旧算法里的 pitch_from_row / snap_to_anchors / fill_gaps / split_wide / evidence 验伪 /
want 补位全部留在 T3 之前；stage 3 只读冻结的泳道。条带算法本身自 T3 起未改动（T4 只搬家）。
"""
from magick_io import read_ppm, to_gray


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


def lane_row_profile(gray, w, c, half, ya, yb):
    """泳道内**沿 x 平均**的行剖面（ImageJ 的 Plot Profile 就是这么量的）。

    比「每行数几个低于阈值的像素」强在哪：淡带可能一个像素都不过阈值，却仍是平均灰度上
    一个清楚的包；两条挨着的带之间那个谷也不会被抹掉。实测 Tris-Tricine 的 21/20 kD：
    二值计数只给 1 条，沿 x 平均给 2 条。
    """
    xa, xb = max(0, int(round(c - half))), min(w - 1, int(round(c + half)))
    n = max(1, xb - xa + 1)
    return [sum(gray[y * w + x] for x in range(xa, xb + 1)) / float(n) for y in range(ya, yb)]


def peaks_in_profile(prof, lo, base_win, min_prom, min_sep):
    """平均剖面上找条带峰（先减宽窗基线 = top-hat，再取局部极大）。

    lo = 剖面第一行对应的 y；min_prom = 显著度下限（灰度级）；min_sep = 两峰最小间隔（格单位）。
    返回 [(行 y, 半高宽, 深度)]；半高宽就是条带粗细，两条并成一条时它明显更宽（见 fat_rows）；
    深度 = 平滑后的 top-hat 峰高（灰度级），越大带越浓（mw_report 用它挑最浓的带）。
    """
    n = len(prof)
    if n < 5:
        return []
    win = max(3, int(round(base_win)))
    ink = []
    for i in range(n):
        a, b = max(0, i - win), min(n, i + win + 1)
        ink.append(sum(prof[a:b]) / (b - a) - prof[i])          # 越暗 → 越大
    sm = [sum(ink[max(0, i - 1):min(n, i + 2)]) / len(ink[max(0, i - 1):min(n, i + 2)])
          for i in range(n)]                                   # 只做 3 行轻平滑：
    # 重平滑会把只有 2–3 px 高的窄带（高分子量那几条）直接抹平（实测 250 kD 就是这么丢的）。
    # 两端各留一段不取峰：top-hat 的基線在边界处被截断，胶的上下边缘渐变会被认成一条带
    # （实测最底部凭空多出一条「带」，其实是胶的下缘）。顶边另由 ignore_top 挡一道。
    a_margin, b_margin = max(2, int(0.5 * win)), win
    cands = [i for i in range(1, n - 1)
             if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] >= min_prom
             and (sm[i] > sm[i - 1] or sm[i] > sm[i + 1])
             and a_margin <= i <= n - 1 - b_margin]
    keep = []
    for i in cands:                                            # 平台/锯齿 → 合并邻近候选
        if keep and i - keep[-1] <= min_sep:
            if sm[i] > sm[keep[-1]]:
                keep[-1] = i
        else:
            keep.append(i)
    out = []
    for i in keep:
        a, b = i, i
        while a > 0 and sm[a - 1] > sm[i] / 2.0:
            a -= 1
        while b + 1 < n and sm[b + 1] > sm[i] / 2.0:
            b += 1
        out.append((lo + i, max(1.0, b - a + 1.0), sm[i]))
    return out


def band_rows_from_gray(gray, w, c, half, ya, yb, scale):
    """一条泳道的条带行 = 沿 x 平均的剖面里的峰（`scale` = 该栅格相对分析尺度的倍数）。"""
    # 参数是拿本胶实测调出来的（行距 9.3 分析 px）：基线窗 ~2 倍行距（太宽会把并带那条的
    # 显著度吃掉 —— 实测 40 px 窗时 250 kD 的显著度从 24 掉到 -1.7）、显著度 ≥4 灰度级。
    return peaks_in_profile(lane_row_profile(gray, w, c, half, ya, yb), ya,
                            base_win=max(8, int(round(20 * scale))),
                            min_prom=4.0, min_sep=max(2.0, 3.0 * scale))


def group_rows(rows, span):
    """按「每条标签盖几条带」把行分组 → 每条标签的落点（组内均值）。

    `span[i]` = 第 i 条标签盖几条带（markers 里 `"rows": 2` 就是盖两条，21/20 kD 那种并着跑的）。
    """
    eff, ri = [], 0
    for k in span:
        grp = rows[ri:ri + k]
        ri += k
        if grp:
            eff.append(sum(grp) / float(len(grp)))
    return eff


def fat_rows(bands, k=1.6):
    """哪几行明显比同一条泳道的其他行厚 —— 一条行里塞了两条分子量接近的带（21/20 kD 这种）。

    返回 [(行 y, 厚/中位数)]。宽的行本来也可能只是条带粗，所以阈值给得松（1.6×），
    只当提示用，不拿来拒绝出图。
    """
    th = sorted(t[1] for t in bands)
    med = th[len(th) // 2] if th else 0.0
    if not med:
        return []
    return [(round(t[0], 1), round(t[1] / med, 1)) for t in bands if t[1] >= k * med]


def find_bands(path, box_analysis, lanes_analysis, longest=700, ignore_top=0.12,
               row_longest=1200, cue="dark", thr=None):
    """stage 3：只读冻结的泳道 x（分析尺度），返回每条泳道的条带行。

    box_analysis = stage 2 实际用的胶面矩形（分析尺度）；lanes_analysis = 左边→右边的 x 列表。
    返回 {"bands": [[(y_analysis, width, depth), ...]], "row_spacing", "y_range",
          "gel_median", "band_thr", "lane_thr", "rows_scale"}。
    本函数**不**返回任何泳道位置，也不改泳道；条带行永远不回流到 stage 2。
    `cue` 只为签名兼容保留（stage 3 只吃灰度；认胶的 cue 在 stage 1 已经用完）。
    """
    w, h, rgb = read_ppm(path, longest)
    gray = to_gray(w, h, rgb)
    x0, y0, x1, y1 = [int(round(v)) for v in box_analysis]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    top = y0 + int((y1 - y0) * ignore_top)
    vals = sorted(gray[y * w + x] for y in range(top, y1 + 1) for x in range(x0, x1 + 1))
    n = len(vals) or 1
    med = vals[n // 2] if vals else 0
    mad = sorted(abs(v - med) for v in vals)[n // 2] if vals else 0
    band_thr = med - max(8, 3.0 * 1.4826 * mad)
    lane_thr = thr if thr is not None else med - max(3, 1.5 * 1.4826 * mad)
    ytop, ybot = top, y1

    # 泳道半宽：stage 2 只给了 x，没有宽度；按旧口径从**列剖面**取该道所在 run 的宽
    # （旧算法也是 0.5×run 宽）。只用来定「沿 x 平均」的范围，不参与泳道位置/接受。
    # 宽 run 夹到一个间距（旧 split_wide 的效果），免得把邻道的带卷进来。
    pitch = None
    xs = [float(v) for v in lanes_analysis]
    if len(xs) >= 2:
        gaps = sorted(b - a for a, b in zip(xs, xs[1:]))
        pitch = gaps[len(gaps) // 2]
    cols = [0] * (x1 - x0 + 2)
    for y in range(ytop, ybot + 1):
        row = y * w
        for x in range(x0, x1 + 1):
            if gray[row + x] < lane_thr:
                cols[x - x0] += 1
    runs = merge_near(clusters(cols, x0, max(2, int(0.05 * (max(cols) or 1)))), 3.0)
    if pitch:
        runs = merge_near(runs, 0.7 * pitch)

    def _half_for(c):
        cand = [r for r in runs if r[0] - r[1] / 2.0 <= c <= r[0] + r[1] / 2.0]
        if not cand and runs:
            near = min(runs, key=lambda r: abs(r[0] - c))
            if abs(near[0] - c) <= 0.5 * (pitch or near[1]):
                cand = [near]
        wd = min(cand[0][1], pitch) if cand and pitch else (cand[0][1] if cand else (pitch or 18.0))
        return max(3, int(round(0.5 * wd)))

    # 条带行（尤其梯子）在 700 尺度上分不开：另用一套更细的栅格扫行，再换算回分析尺度。
    rows_scale, rows_gray = 1.0, None
    w_hi = h_hi = 0
    if row_longest and row_longest > longest:
        w_hi, h_hi, rgb_hi = read_ppm(path, row_longest)
        if w_hi > w:
            rows_scale = w_hi / float(w)
            rows_gray = to_gray(w_hi, h_hi, rgb_hi)

    bands, bands_hi = [], []
    for c in xs:
        half = _half_for(c)
        bands.append(band_rows_from_gray(gray, w, c, half, max(0, ytop), min(h, ybot + 1), 1.0))
        if rows_gray is not None:
            bands_hi.append(band_rows_from_gray(rows_gray, w_hi, c * rows_scale, half * rows_scale,
                                                int(round(ytop * rows_scale)),
                                                min(h_hi, int(round((ybot + 1) * rows_scale))),
                                                rows_scale))
    if bands_hi:                       # 细栅格扫出来的行是唯一真相，换算回分析尺度对外给
        bands = [[(r[0] / rows_scale, r[1] / rows_scale, r[2]) for r in b] for b in bands_hi]

    row_spacing = None
    for b in bands:
        if len(b) >= 3:
            g = sorted(b[i + 1][0] - b[i][0] for i in range(len(b) - 1))
            row_spacing = g[len(g) // 2]
            break
    return {"bands": bands,
            "rows_scale": rows_scale,
            "row_spacing": row_spacing,
            "y_range": (ytop, ybot),
            "gel_median": med,
            "band_thr": round(band_thr, 1),
            "lane_thr": round(lane_thr, 1)}
