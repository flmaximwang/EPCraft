#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""泳道种子：等距梳子，相位与间距由胶面顶上的加样孔定（V8）。

为什么不从条带/墨量里猜泳道：样品泳道常常是空的或极淡的（失效报告：样品带只深 3–4 灰度级），
列墨量剖面上没有足够的东西可锁；老 stage 2 在 IMG_1496 上因此整体偏了一道（梯子落进泳道 2）。
而加样孔**每一道都有**、不管上没上样：胶面图最上面一条（孔底）里，每个孔是一道深色的 U 形轮廓，
孔与孔之间是浅色的胶。梳子本来就是等距的，所以只拟合两个数：

    centre_k = c0 + k · pitch,  k = 0 … n−1

打分 = 各孔窗口（宽 FRAC·pitch）里的平均深度 − 窗口外的平均深度（窗外含两侧没有孔的边距，
所以整排梳子错位一道会被罚）。实测（2026-10-22，5 张 DGC 照片 + 用户在 IMG_1499 上手放的 15 道）：
间距 0.51–0.52 cm，与手放的中心平均差 0.015 cm、最大 0.042 cm（手放本身的间距抖动 ±0.03 cm）。

看不到孔（最优分 < MIN_WELL_DEPTH 灰度级）时退回「等距、按预设间距、整排居中」，并在结果里说明。
"""


STRIP_CM = 0.12          # 胶面最上面这一条里找孔底的 U 形（gel-prior：胶面上缘 = 孔底）
FRAC = 0.75              # 打分窗宽 = FRAC × pitch：要盖住 U 的两侧壁（孔宽 ≈ 0.7 pitch）；扫 0.6–0.8，
                         # 0.75 在合成孔（误差 0.005 cm）和 IMG_1499 手放（均 0.015、最大 0.042 cm）上都最好
WIDTH_FRAC = 0.7         # 种子泳道宽 = 0.7 × pitch（用户在 IMG_1499 上手放的宽/距 = 0.71）
PITCH_TOL = 0.20         # 间距搜索范围 = 预设 ±20%
PITCH_STEP_CM = 0.002
MIN_WELL_DEPTH = 20.0    # 最优分（孔内比孔外深多少灰度级）≥ 20 才算看见了孔：实测有孔 41–87，
                         # 同一张胶中部/孔下 0.4 cm 处（没有孔）0.9–9.9。相对比值不可用：
                         # 顶条的 MAD 被孔本身撑大，有孔/无孔的比值区间互相重叠（1.2–3.2 vs 0.3–1.6）


def strip_profile(gray, w, h, px_per_cm, depth_cm=STRIP_CM):
    """胶面顶条里每一列的「深度」= 顶条中位灰度 − 该列在顶条内的最暗值（U 形轮廓很细，取最暗不取平均）。"""
    rows = max(2, min(h, int(round(depth_cm * px_per_cm))))
    vals = sorted(gray[y * w + x] for y in range(rows) for x in range(w))
    bg = vals[len(vals) // 2]
    return [bg - min(gray[y * w + x] for y in range(rows)) for x in range(w)]


def fit_comb(prof, px_per_cm, n, pitch_lo_cm, pitch_hi_cm, frac):
    """→ (首道中心 cm, 间距 cm, 最优分, 全部分数列表)。穷举间距（步长 0.002 cm）× 相位（步长 1 px）。"""
    w = len(prof)
    pre = [0.0]
    for v in prof:
        pre.append(pre[-1] + v)
    tot = pre[-1]
    best, scores = None, []
    steps = int(round((pitch_hi_cm - pitch_lo_cm) / PITCH_STEP_CM))
    for s in range(steps + 1):
        p = pitch_lo_cm + s * PITCH_STEP_CM
        pp = p * px_per_cm
        half = frac * pp / 2.0
        x0 = half
        while x0 + (n - 1) * pp + half <= w:
            ins, cnt = 0.0, 0
            for k in range(n):
                c = x0 + k * pp
                a, b = int(round(c - half)), int(round(c + half))
                ins += pre[b] - pre[a]
                cnt += b - a
            score = ins / cnt - (tot - ins) / max(1, w - cnt)
            scores.append(score)
            if best is None or score > best[0]:
                best = (score, x0 / px_per_cm, p)
            x0 += 1.0
    if best is None:
        raise RuntimeError("胶面太窄：放不下 %d 道、间距 ≥ %.3f cm 的梳子" % (n, pitch_lo_cm))
    return best[1], best[2], best[0], scores


def seed(gray, w, h, px_per_cm, n_lanes, pitch_prior_cm):
    """胶面灰度图 → 等距泳道种子。

    返回 {"lanes": [(左 cm, 右 cm)…], "pitch_cm", "first_cm", "source": "wells"|"even", "depth", "note"}。
    depth = 最优分（孔内比孔外深多少灰度级）；低于 MIN_WELL_DEPTH → 看不到孔，退回居中等距。
    """
    W = w / float(px_per_cm)
    prof = strip_profile(gray, w, h, px_per_cm)
    lo, hi = pitch_prior_cm * (1 - PITCH_TOL), pitch_prior_cm * (1 + PITCH_TOL)
    hi = min(hi, W / max(1, n_lanes - 1 + FRAC))
    c0, p, best, _scores = fit_comb(prof, px_per_cm, n_lanes, lo, hi, FRAC)
    note = ""
    source = "wells"
    if min(p - lo, hi - p) < PITCH_STEP_CM / 2:
        note = ("孔距 %.3f cm 顶到了搜索边界（预设 %.3f cm ±%d%%）—— 核对预设的 lane_pitch_cm"
                % (p, pitch_prior_cm, int(PITCH_TOL * 100)))
    if best < MIN_WELL_DEPTH:
        p = pitch_prior_cm
        c0 = (W - (n_lanes - 1) * p) / 2.0
        source = "even"
        note = ("胶面顶上看不清加样孔（深 %.1f < %.0f 灰度级）：按预设间距 %.3f cm 整排居中放了 %d 道，"
                "请在 Workbench 上核对" % (best, MIN_WELL_DEPTH, p, n_lanes))
    half = WIDTH_FRAC * p / 2.0
    centres = [c0 + k * p for k in range(n_lanes)]
    lanes = [(round(max(0.0, c - half), 4), round(min(W, c + half), 4)) for c in centres]
    return {"lanes": lanes, "pitch_cm": round(p, 4), "first_cm": round(c0, 4), "source": source,
            "depth": round(best, 1), "note": note}
