#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""胶面矩形内找泳道 x 与每条泳道的条带行（含补位、间距、并带检测）。"""
import math

from magick_io import read_ppm, to_gray
from gel_detect import ink_mask, bg_plane, morph, components


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


def snap_to_band(y, rows, spacing):
    """把手动给的 y 吸附到最近的**实测条带行**（容差 = 相邻行距的 40%）。

    手动标定时人对着 debug 图的标尺给个大概值就行，吸附负责落到带上。
    """
    if not rows:
        return y
    tol = max(2.0, 0.4 * (spacing or 8.0))
    best = min(rows, key=lambda r: abs(r - y))
    return best if abs(best - y) <= tol else y


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


def snap_to_anchors(cx, anchors, pitch):
    """把泳道中心吸附到锚点上（锚点 = 条带最密那一行的墨 run）。偏超过半个间距就不动。

    为什么需要它：整块胶的**列投影**会被「这条泳道整列上的零星杂点」带偏 —— 一条空泳道
    可能被一个 1 px 的孤立列尖峰钉在 308，而它的带其实在 305；旁边那条又因为宽 run 按
    自身宽度均分落在 319.6（真值 321.5）。两者一挤，标签看起来就「11 贴着 12」。
    一条泳道的 x 应该由**它的带在哪**决定，所以拿条带最密那行的 run 当锚点最直接。
    """
    if not anchors or not pitch:
        return list(cx)
    out = []
    for c in cx:
        a = min(anchors, key=lambda t: abs(t - c))
        out.append(a if abs(a - c) <= 0.5 * pitch else c)
    return out


def lanes_and_bands(path, rect, angle, longest=700, ignore_top=0.12, ignore_bottom=0.0,
                    cue="dark", thr=None, lane_mad_k=1.5, interp=True, want=None,
                    min_loose_ink=6, row_longest=1200):
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

    # 条带行（尤其梯子）在 700 尺度上分不开：另用一套更细的栅格扫行，再换算回分析尺度。
    # 泳道探测仍用 --longest（够用且快），只有行剖面吃到这个细栅格。
    rows_scale, rows_gray = 1.0, None
    if row_longest and row_longest > longest:
        w_hi, h_hi, rgb_hi = read_ppm(path, row_longest)
        if w_hi > w:
            rows_scale = w_hi / float(w)
            rows_gray = to_gray(w_hi, h_hi, rgb_hi)
            vals_hi = sorted(rows_gray[y * w_hi + x]
                             for y in range(int(y0 * rows_scale), min(h_hi, int(y1 * rows_scale) + 1))
                             for x in range(int(x0 * rows_scale), min(w_hi, int(x1 * rows_scale) + 1)))
            nn_hi = len(vals_hi) or 1
            med_hi = vals_hi[nn_hi // 2]
            mad_hi = sorted(abs(v - med_hi) for v in vals_hi)[nn_hi // 2]
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
    # 泳道中心用「条带最密那一行」的墨 run 校正：列投影会被整列上的零星杂点带偏，
    # 而一条泳道的 x 应该由它的带在哪决定（见 snap_to_anchors 的注释）。只有宽到像
    # 一条泳道的 run 才算锚点，免得把竖纹/边角噪声当锚点。
    if pitch and pitch_row is not None and lanes:
        prow = pitch_row * w
        am = [1 if m_loose[prow + x] else 0 for x in range(x0, x1 + 1)]
        anchors = [c for c, wd, _ in split_wide(merge_near(clusters(am, x0, 1), 3.0), pitch)
                   if wd >= 0.5 * pitch]
        if anchors:
            snapped = snap_to_anchors([c for c, _ in lanes], anchors, pitch)
            moved = [(round(b - a, 1)) for (a, _), b in zip(lanes, snapped) if abs(b - a) > 0.5]
            if moved:
                print("[i] 泳道中心按条带最密行(%d)的 %d 个锚点校正 %d 条：移动 %s"
                      % (pitch_row, len(anchors), len(moved), moved))
            lanes = list(zip(snapped, [wd for _, wd in lanes]))
    marks, skipped = fill_gaps([c for c, _ in lanes], [wd for _, wd in lanes], pitch) \
        if interp else ([(c, wd, False) for c, wd in lanes], [])
    if want and len(marks) != want:
        print("!! 声明 %d 条泳道，实测+补位 %d 条（缺的在配置里用 lanes.x_frac 显式给位置）"
              % (want, len(marks)))
    bands, bands_hi = [], []
    for c, wd, _it in marks:
        half = max(3, int(round(0.5 * wd)))     # ±半条泳道宽：±整条宽 = ±一个间距，
        bands.append(band_rows_from_gray(gray, w, c, half,          # 会把邻道的带卷进来
                                         max(0, ytop), min(h, y1 + 1), 1.0))
        if rows_gray is not None:
            half_hi = max(3, int(round(0.5 * wd * rows_scale)))
            bands_hi.append(band_rows_from_gray(rows_gray, w_hi, c * rows_scale, half_hi,
                                                int(round(ytop * rows_scale)),
                                                min(h_hi, int(y1 * rows_scale) + 1), rows_scale))
    cx_s = [c for c, _, _ in marks]
    if bands_hi:                       # 细栅格扫出来的行是唯一真相，换算回分析尺度对外给
        bands = [[(r[0] / rows_scale, r[1] / rows_scale, r[2]) for r in b] for b in bands_hi]
    row_spacing = None
    for b in bands:
        if len(b) >= 3:
            g = sorted(b[i + 1][0] - b[i][0] for i in range(len(b) - 1))
            row_spacing = g[len(g) // 2]
            break
    return {"lanes": [(round(c, 1), wd) for c, wd, _ in marks],
            "rows_scale": rows_scale,
            "row_spacing": row_spacing,
            "interp": [it for _, _, it in marks],
            "bands": bands, "pitch": (round(pitch, 2) if pitch else None),
            "y_range": (ytop, y1), "gel_median": med, "band_thr": round(band_thr, 1),
            "lane_thr": round(lane_thr, 1), "dropped": dropped, "skipped": skipped,
            "pitch_row": pitch_row,
            "measured": sum(1 for _, _, it in marks if not it)}
