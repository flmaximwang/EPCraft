#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage 2：在 stage 1 的冻结胶面上拟合泳道格点（先验驱动，不读条带）。

管线是严格串行的（spec §2 决策 3）：胶面 → 泳道 → 条带，每一级只读上一级的冻结输出。
本模块只做第二级：

    stage 1 的冻结输出（gel_box / angle / px_per_cm / lane_pitch_cm）
      ├─ 把 gel_box 逆变换回分析尺度（geometry.rect_to_analysis）
      ├─ 先验间距 pitch_prior = lane_pitch_cm × px_per_cm / (image_w / analysis_w)
      ├─ 胶面列剖面（宽松阈值：中位数 − 1.5·1.4826·MAD）
      ├─ 拟合格点：(pitch ∈ 先验 ±5%, offset) 网格搜索，最大化每条泳道窗口内的剖面和
      ├─ 每道细化：在 ±0.4·pitch 内找局部峰；峰不够显著就留在格点上并标 estimated
      └─ 恒输出 lane_count 条（淡道、空道都保留，绝不丢弃、不补位凑数）

**不读条带**：本模块只有列剖面（每列有多少像素低于暗阈值），没有行剖面、没有 band rows、
没有「条带最密的一行」。泳道的 measured/estimated 与 x 细化都只由列剖面决定。
本模块也不 import lane_detect（那里有 stage 3 的条带代码）：stage 2 不能依赖 stage 3。
"""
from geometry import load_dims_after, rect_to_analysis, rotate_pt
from magick_io import identify, read_ppm, to_gray


# ---------------------------------------------------------------- 列剖面

def _column_profile(gray, w, box, ignore_top):
    """胶面内每列「低于宽松阈值」的像素数。

    阈值口径与旧泳道算法一致（胶内中位数 − max(3, 1.5·1.4826·MAD)），这样只有一条淡带的
    泳道也能在剖面上露头；上缘 ignore_top 那段（上样孔/梳齿）不参与，免得孔壁被当泳道。
    返回 (prof, thr, med, mad, y_top, rows)；prof 的下标 0 对应 box 的 x0。
    """
    x0, y0, x1, y1 = box
    top = y0 + int(round((y1 - y0) * ignore_top))
    vals = sorted(gray[y * w + x] for y in range(top, y1 + 1) for x in range(x0, x1 + 1))
    n = len(vals)
    if n < 20:
        raise RuntimeError("stage 2：胶面太小（%d 个像素），量不出列剖面" % n)
    med = vals[n // 2]
    mad = sorted(abs(v - med) for v in vals)[n // 2]
    thr = med - max(3.0, 1.5 * 1.4826 * mad)
    prof = [0] * (x1 - x0 + 1)
    for y in range(top, y1 + 1):
        row = y * w
        for x in range(x0, x1 + 1):
            if gray[row + x] < thr:
                prof[x - x0] += 1
    return prof, thr, med, mad, top, y1 - top + 1


def _runs(prof, thr):
    """连续 >= thr 的列 → [(加权中心, 宽度, 峰值, 起, 止)]（下标是 prof 下标）。"""
    runs, cur = [], []
    for i, v in enumerate(prof):
        if v >= thr:
            cur.append((i, v))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    out = []
    for r in runs:
        tot = sum(v for _, v in r)
        c = sum(i * v for i, v in r) / tot if tot else float(r[0][0])
        out.append((c, r[-1][0] - r[0][0] + 1, max(v for _, v in r), r[0][0], r[-1][0]))
    return out


# ---------------------------------------------------------------- 格点拟合

def _lattice(n, cx, pitch, off=0.0):
    """n 条等距格点的中心：以胶面中心 cx 对称，间距 pitch，整体平移 off。"""
    mid = (n + 1) / 2.0
    return [cx + (i - mid) * pitch + off for i in range(1, n + 1)]


def _score(prof, x0, xs, pitch, cap=None):
    """格点评分 = 每条泳道中心 ±0.2·pitch 窗口内列剖面的**和**。

    为什么用和而不是窗口最大值：淡道往往摊在好几个列上（每条列只比背景暗一点），
    求和能把「几条略暗的列」累积成证据；而最大值会被一根 1 px 的孤立划痕骗走。
    窗口固定宽度（∝ 间距），所以浓道不会仅因为更宽就无限加分。

    cap = 单列计数的上限（= 胶面有效高度的一半）。一整列从上到下都暗的是**竖纹**，不是泳道：
    真泳道的条带只占高度的一部分，而竖纹会在某一列贡献满高计数。不封顶的话，一个 2–3 px
    的竖纹就能把只有 5 条泳道的格点拽偏 5%。封顶只影响「整列全暗」的列，正常泳道远低于它。
    """
    half = max(1, int(round(0.2 * pitch)))
    total = 0.0
    for xi in xs:
        a = int(round(xi)) - x0
        lo, hi = max(0, a - half), min(len(prof) - 1, a + half)
        if hi >= lo:
            seg = prof[lo:hi + 1]
            total += sum(min(v, cap) for v in seg) if cap else sum(seg)
    return total


def _fit_pitch_offset(prof, x0, cx, n, pitch_prior, cap=None):
    """在先验 ±5% 内网格搜索 (pitch, offset)，返回评分最高的组合。"""
    best = (-1.0, pitch_prior, 0.0)
    for k in range(9):
        pitch = pitch_prior * (0.95 + 0.10 * k / 8.0)
        step = 0.25
        m = int(round(0.5 * pitch / step))
        for j in range(-m, m + 1):
            off = j * step
            if abs(off) > 0.5 * pitch + 1e-9:
                continue
            s = _score(prof, x0, _lattice(n, cx, pitch, off), pitch, cap)
            if s > best[0]:
                best = (s, pitch, off)
    if best[0] <= 0:
        # 整块胶没有任何暗列（空胶/纯噪声）：pitch 不可辨，直接用先验，免得落到 0.95×先验
        # 再报一条假的「差 5%」warning。
        return pitch_prior, 0.0, 0.0
    return best[1], best[2], best[0]


def _refine_one(prof, x0, xi, pitch, prom_min):
    """单条泳道：在 xi ± 0.4·pitch 内找局部峰并细化（不越界）。

    峰 = 该窗口内「高于半显著度」的连续段的加权中心（sub-pixel）。用半显著度而不是
    全局宽松阈值：相邻两条淡道可能被宽松阈值连成一个宽 run（实测右半边 34 px 宽的
    一片里其实有两条泳道），取半显著度能把它们按中间的谷分开。
    显著度 = 窗口峰值 − 两侧边缘基线；不够 prom_min 就返回格点位置（estimated）。
    """
    ncol = len(prof)
    half = 0.4 * pitch
    lo = max(0, int(round(xi - half)) - x0)
    hi = min(ncol - 1, int(round(xi + half)) - x0)
    seg = prof[lo:hi + 1]
    peak = max(seg) if seg else 0
    base = min(seg[:2] + seg[-2:]) if seg else 0
    prom = float(peak - base)
    if peak <= 0 or prom < prom_min:
        e = prof[min(ncol - 1, max(0, int(round(xi)) - x0))] if ncol else 0
        return xi, False, float(e), None

    # 半显著度以上的连续段（从窗口内的峰值向两侧长）
    imax = lo + seg.index(peak)
    cut = base + 0.5 * (peak - base)
    a = imax
    while a > lo and prof[a - 1] >= cut:
        a -= 1
    b = imax
    while b < hi and prof[b + 1] >= cut:
        b += 1
    tot = sum(prof[a:b + 1])
    if tot <= 0:
        return xi, False, float(prof[imax]), None
    c = sum((i + x0) * prof[i] for i in range(a, b + 1)) / float(tot)
    return c, True, prom, b - a + 1


# ---------------------------------------------------------------- stage 2

def find_lanes(path, ga, lane_count, longest=700, ignore_top=0.12):
    """stage 2：在 stage 1 的冻结胶面上拟合泳道格点。

    path = 原图路径（只用于读灰度分析缩略图）；ga = gel_area.compute() 的返回
           （用 gel_box / px_per_cm / angle，以及 compute 附带的 lane_pitch_cm / canvas）。
    lane_count = 输出泳道数（整块预设的 lane_count，或 --gel-lanes N）。

    返回 dict：
      lanes   : [{"index", "x_analysis", "x_canvas", "width", "width_canvas",
                  "measured", "evidence"}] —— 恒 lane_count 条，从左到右
      pitch   : 拟合出来的间距（analysis px）
      pitch_prior : 先验间距（analysis px）
      evidence: 格点的总证据（float）
      box_analysis : 本 stage 实际用的分析尺度矩形 (x0,y0,x1,y1)
      checks  : [{"name": "pitch", "ok": bool, "detail": ...}]
    """
    w, h, rgb = read_ppm(path, longest)
    gray = to_gray(w, h, rgb)
    # 原图尺寸：read_ppm 给的是分析尺度，换算比例需要原图宽；分析尺度就是刚读进来的 w×h。
    W0, H0 = identify(path)
    info = {"path": path, "image": (W0, H0), "analysis": (w, h)}
    canvas = ga.get("canvas")
    if canvas is None:
        canvas = load_dims_after(path, ga["angle"])[1]

    # ---- 1) 冻结胶面 → 分析尺度；超出画面的部分 clamp（stage 1 允许框出画布）
    bx0, by0, bx1, by1 = rect_to_analysis(ga["gel_box"], ga["angle"], info, canvas)
    x0, y0 = max(0, bx0), max(0, by0)
    x1, y1 = min(w - 1, bx1), min(h - 1, by1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise RuntimeError("stage 2：冻结胶面落在分析缩略图外（%s），没法拟合泳道"
                           % ((bx0, by0, bx1, by1),))
    box = (x0, y0, x1, y1)
    n = int(lane_count)
    if n < 1:
        raise RuntimeError("stage 2：lane_count 必须 ≥ 1（收到 %r）" % (lane_count,))

    # ---- 2) 先验间距（画布 px/cm → 分析 px/cm）。绝不从条带最密行量间距。
    a2x = W0 / float(w)
    a2y = H0 / float(h)
    pitch_cm = float(ga.get("lane_pitch_cm") or 0.0)
    if pitch_cm:
        pitch_prior = pitch_cm * float(ga["px_per_cm"]) / a2x
    else:
        # 兜底：预设没带 lane_pitch_cm 时，用「胶面宽 / 泳道数」当先验（只在测试/旧调用里出现）。
        pitch_prior = (x1 - x0) / float(n)

    # ---- 3) 列剖面 + 格点拟合
    prof, _thr, _med, _mad, _ytop, rows = _column_profile(gray, w, box, ignore_top)
    cx = (x0 + x1) / 2.0
    cap = 0.5 * rows                  # 竖纹封顶：一整列全暗的投票不许超过半个高度
    pitch_fit, _off, _best = _fit_pitch_offset(prof, x0, cx, n, pitch_prior, cap)

    # 4) 按「胶面左边 + (i−0.5)·pitch」重排，并把它居中到胶面中心。
    #    两个写法只在「胶面宽 ≠ n·pitch」时差一个常数（每个边缘差一半），差很小时等价；
    #    这里选居中的那个：胶面是物理胶面，泳道应当左右对称，余量两边各一半。
    edge = [x0 + (i - 0.5) * pitch_fit for i in range(1, n + 1)]
    shift = cx - sum(edge) / float(n)
    lattice = [v + shift for v in edge]

    # ---- 5) 每道细化 + 宽度；显著度阈值 = max(3, 10% 峰值列计数)（明确写死，可解释）
    # 显著度阈值 = max(3, 10% 峰值列计数)。峰值也要扣掉竖纹：整列全暗的竖纹封顶到 cap，
    # 否则一根竖纹会把阈值抬高到正常淡道（本例 11 个像素）全被判成 estimated。
    raw_peak = max(prof) if prof else 0
    cmax = min(raw_peak, cap) if cap else raw_peak
    prom_min = max(3.0, 0.10 * cmax)
    runs = _runs(prof, max(2, int(0.05 * raw_peak))) if raw_peak else []
    wmin, wmax = 0.4 * pitch_fit, 1.0 * pitch_fit
    lanes = []
    y_mid_a = (y0 + y1) / 2.0
    for i, xi in enumerate(lattice, 1):
        x, measured, evidence, lobe_w = _refine_one(prof, x0, xi, pitch_fit, prom_min)
        if measured and lobe_w is not None:
            wid = lobe_w
        else:
            # 没峰：取该处（格点）所在的宽松 run 的宽；没有 run 就给 0.6·pitch
            wid = 0.6 * pitch_fit
            for c, rw, _pk, ra, rb in runs:
                if ra <= xi - x0 <= rb:
                    wid = rw
                    break
        wid = min(wmax, max(wmin, float(wid)))
        # ---- 6) 分析尺度 → 旋转画布（与 render 的 rotate_pt 同一套矩阵）
        ox, oy = x * a2x, y_mid_a * a2y
        rx, ry = rotate_pt(ox, oy, W0 / 2.0, H0 / 2.0, ga["angle"])
        x_canvas = rx + (canvas[0] - W0) / 2.0
        lanes.append({
            "index": i,
            "x_analysis": round(x, 2),
            "x_canvas": x_canvas,
            "width": wid,
            "width_canvas": wid * a2x,
            "measured": bool(measured),
            "evidence": round(float(evidence), 2),
        })

    # ---- 7) 核对：拟合 pitch vs 先验（>5% 只 warning，不抛异常）
    dev = abs(pitch_fit - pitch_prior) / pitch_prior if pitch_prior else 0.0
    ok = dev <= 0.05
    if not ok:
        print("[!] pitch: 拟合 %.2f px，先验 %.2f px（差 %.1f%%）"
              % (pitch_fit, pitch_prior, dev * 100.0))
    checks = [{"name": "pitch", "ok": ok,
               "detail": "%.2f vs %.2f px, %.1f%%" % (pitch_fit, pitch_prior, dev * 100.0)}]

    return {
        "lanes": lanes,
        "pitch": round(pitch_fit, 2),
        "pitch_prior": round(pitch_prior, 2),
        "evidence": float(_score(prof, x0, lattice, pitch_fit, cap)),
        "box_analysis": box,
        "checks": checks,
    }
