#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Workbench 出分析：每条泳道的一维灰度剖面、条带、marker 标定、分子量（spec §7）。

输入只有两样：workbench 模型（cm，胶面左上角为原点）和文档里那张胶面图（PNG 字节）。
render（探测出来的线）和 sync（用户改过的线）走同一份代码。

1. 剖面：积分窗 [top, bottom] 内每一像素行，取 x ∈ [L, R] 的平均灰度（列中心落在区间里的像素）。
2. 基线与墨量：沿用 stage 3 实测调好的 top-hat —— 基线 = 宽窗滑动均值，ink = 基线 − 灰度
   （越暗越大）；窗宽、平滑、最小峰距改用 cm 表示，所以与胶面图的分辨率无关。
3. 条带：平滑后 ink 的局部极大，**局部**显著度（比两侧低点高多少）≥ K_NOISE × 该泳道背景噪声，
   **没有绝对墨量下限**（失效报告 §6 P2-2：本批样品带只深 3–4 灰度级，绝对阈值会把整条淡道丢掉）。
   两端留一段不取峰：top-hat 的基线在剖面端点被截断（conventions §2c 的老坑）。
4. 标定：`band <MW>` 线的 y ↔ MW，log10(MW) ~ y（或 interp），与 mw_report 同一套数学。

报告 v2 的坐标一律是胶面 cm（与 record 同口径），不存照片像素。
"""
import os
import tempfile

from magick_io import read_ppm, to_gray
from mw_report import build_cal, estimate


BASE_HALF_CM = 0.30      # top-hat 基线半窗（stage 3：20 分析 px ≈ 0.33 cm；再宽会吃掉并带的显著度）
SMOOTH_CM = 0.03         # 轻平滑半窗（stage 3：3 行 ≈ 0.03 cm；再重会抹平 2–3 px 高的窄带）
MIN_SEP_CM = 0.05        # 两峰最小间隔（并带 21/20 kD 的两条约隔 0.12 cm）
FLANK_CM = 0.15          # 局部显著度的两侧取低点范围（= 基线半窗的一半；IMG_1496 实测比整窗少叫背景纹理）
K_NOISE = 4.0            # 显著度 ≥ K_NOISE × 噪声
NOISE_FLOOR = 0.15       # 噪声估计的下限（灰度级）：合成纯色图噪声为 0，不能让门槛也为 0


# ---------------------------------------------------------------- 图像

def gel_gray(png):
    """PNG 字节 → (w, h, 灰度 bytearray)。经 magick 解码（PNG 的 5 种行滤波器都得会，不自己写）。"""
    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        w, h, rgb = read_ppm(path, None)
    finally:
        os.unlink(path)
    return w, h, to_gray(w, h, rgb)


# ---------------------------------------------------------------- 剖面与条带

def lane_profile(gray, w, h, px_per_cm, left_cm, right_cm, top_cm, bottom_cm):
    """→ (y_cm 列表, 平均灰度列表)。行/列都取「像素中心落在区间里」的；窄到一列都不含时取最近一列。"""
    def span(a_cm, b_cm, n):
        lo = max(0, int(-(-(a_cm * px_per_cm - 0.5) // 1)))       # ceil(a·ppc − 0.5)
        hi = min(n - 1, int((b_cm * px_per_cm - 0.5) // 1))         # floor(b·ppc − 0.5)
        if hi < lo:
            c = min(n - 1, max(0, int(round((a_cm + b_cm) / 2.0 * px_per_cm - 0.5))))
            lo = hi = c
        return lo, hi
    xa, xb = span(left_cm, right_cm, w)
    ya, yb = span(top_cm, bottom_cm, h)
    nx = float(xb - xa + 1)
    ys, gs = [], []
    for y in range(ya, yb + 1):
        row = y * w
        ys.append((y + 0.5) / px_per_cm)
        gs.append(sum(gray[row + x] for x in range(xa, xb + 1)) / nx)
    return ys, gs


def ink_trace(gs, px_per_cm):
    """top-hat：→ (baseline, ink, ink 平滑)。baseline = 半窗 BASE_HALF_CM 的滑动均值。"""
    n = len(gs)
    win = max(3, int(round(BASE_HALF_CM * px_per_cm)))
    sm_w = max(1, int(round(SMOOTH_CM * px_per_cm)))
    pre = [0.0]
    for v in gs:
        pre.append(pre[-1] + v)
    base, ink = [], []
    for i in range(n):
        a, b = max(0, i - win), min(n, i + win + 1)
        m = (pre[b] - pre[a]) / (b - a)
        base.append(m)
        ink.append(m - gs[i])
    pre = [0.0]
    for v in ink:
        pre.append(pre[-1] + v)
    sm = [(pre[min(n, i + sm_w + 1)] - pre[max(0, i - sm_w)]) / (min(n, i + sm_w + 1) - max(0, i - sm_w))
          for i in range(n)]
    return base, ink, sm


def noise_of(sm):
    """泳道噪声（灰度级）= 平滑 ink 围绕中位数的 MAD × 1.4826。

    量的是「背景纹理」（染色不匀、胶面颗粒），不是像素噪声：IMG_1496 实测，相邻行差分法
    几乎处处掉到下限 0.15，于是 1–2 灰度级的纹理全被当成带；改用本法后，真·淡带
    （泳道 5/6/13/14，显著度 1.6–3.6）的 SNR 为 4.1–8.7，空道/纹理峰 ≤3.7，K=4 正好分开。
    密集泳道（marker）里条带本身会把 MAD 撑大（实测 9.9 → 只叫出 11 条里的 8 条），所以迭代：
    去掉偏离中位数 3σ 的行（条带，以及 top-hat 在带两旁留下的负瓣）再量，最多 5 轮 —— 量的始终是背景。
    """
    vals = list(sm)
    sigma = NOISE_FLOOR
    for _ in range(5):
        if len(vals) < 5:
            break
        s = sorted(vals)
        med = s[len(s) // 2]
        sigma = max(NOISE_FLOOR, 1.4826 * sorted(abs(v - med) for v in vals)[len(vals) // 2])
        kept = [v for v in vals if abs(v - med) <= 3.0 * sigma]     # 两侧都剪：top-hat 在带旁留负瓣
        if len(kept) == len(vals):
            break
        vals = kept
    return sigma


def call_bands(ys, sm, px_per_cm, k=K_NOISE):
    """平滑 ink 上的峰 → ([{y_cm, prominence, snr, width_cm}], noise)。

    显著度 = 峰高减去两侧 FLANK_CM 内各自最低点中**较高**的那个（局部显著度），门槛 = k × noise。
    不用「高出 0」：浓带旁边 top-hat 留下负瓣，紧挨着的淡带整个泡在负瓣里（合成梯子上
    250 kD 的绝对高度是 −1.8，但比两侧高 3.8 —— 只看绝对高度就丢了）。
    """
    n = len(sm)
    noise = noise_of(sm)
    if n < 5:
        return [], noise
    win = max(3, int(round(BASE_HALF_CM * px_per_cm)))
    fw = max(2, int(round(FLANK_CM * px_per_cm)))
    a_margin, b_margin = max(2, int(0.5 * win)), win
    min_sep = max(2, int(round(MIN_SEP_CM * px_per_cm)))
    thr = k * noise
    cands = []
    for i in range(max(1, a_margin), min(n - 1, n - b_margin)):
        if not (sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and (sm[i] > sm[i - 1] or sm[i] > sm[i + 1])):
            continue
        prom = sm[i] - max(min(sm[max(0, i - fw):i + 1]), min(sm[i:min(n, i + fw + 1)]))
        if prom >= thr:
            cands.append((i, prom))
    keep = []
    for i, prom in cands:                                     # 平台/锯齿 → 合并邻近候选
        if keep and i - keep[-1][0] <= min_sep:
            if prom > keep[-1][1]:
                keep[-1] = (i, prom)
        else:
            keep.append((i, prom))
    out = []
    for i, prom in keep:
        half = sm[i] - prom / 2.0                             # 半高宽按局部显著度的一半量
        a, b = i, i
        while a > 0 and sm[a - 1] > half:
            a -= 1
        while b + 1 < n and sm[b + 1] > half:
            b += 1
        out.append({"y_cm": round(ys[i], 4), "prominence": round(prom, 2),
                    "snr": round(prom / noise, 1), "width_cm": round((b - a + 1) / px_per_cm, 4)})
    return out, noise


# ---------------------------------------------------------------- 整份报告

def analyze(model, png, lane_names, method="logfit"):
    """workbench 模型 + 胶面图 → 报告 v2 dict（spec §7）。lane_names 与 model["lanes"] 一一对应（None = 未命名）。"""
    w, h, gray = gel_gray(png)
    ppc = w / float(model["gel"]["width_cm"])
    if abs(h / ppc - model["gel"]["height_cm"]) > max(0.02, 2.0 / ppc):
        raise RuntimeError("胶面图 %dx%d px 与胶面 %.3f×%.3f cm 不成比例"
                           % (w, h, model["gel"]["width_cm"], model["gel"]["height_cm"]))
    top = max(0.0, model["window"]["top_cm"])
    bottom = min(model["gel"]["height_cm"], model["window"]["bottom_cm"])
    pairs = [(b["y_cm"], b["mw"]) for b in model["bands"]]
    cal = build_cal(pairs)
    if cal["fit"] is None:
        raise RuntimeError("marker 条带线不足 3 条，无法标定")
    a, b, r2 = cal["fit"]
    lanes, notes = [], []
    for i, ln in enumerate(model["lanes"], 1):
        ys, gs = lane_profile(gray, w, h, ppc, ln["left_cm"], ln["right_cm"], top, bottom)
        base, ink, sm = ink_trace(gs, ppc)
        called, noise = call_bands(ys, sm, ppc)
        cx = (ln["left_cm"] + ln["right_cm"]) / 2.0
        bands = []
        for bd in called:
            mw, ex = estimate(bd["y_cm"], cx, [(cx, cal)], method)
            bands.append(dict(bd, mw_kda=None if mw is None else round(mw, 2), extrapolated=bool(ex)))
        if bands:
            max(bands, key=lambda x: x["prominence"])["strongest"] = True
        name = lane_names[i - 1] if i - 1 < len(lane_names) else None
        lanes.append({
            "lane": i, "label": name, "marker": i == model["marker_lane"], "trimmed": bool(ln["trimmed"]),
            "left_cm": round(ln["left_cm"], 4), "right_cm": round(ln["right_cm"], 4),
            "noise": round(noise, 3),
            "profile": {"y_cm": [round(y, 4) for y in ys], "gray": [round(g, 2) for g in gs],
                        "baseline": [round(v, 2) for v in base], "ink": [round(v, 2) for v in ink]},
            "bands": [dict(x, strongest=x.get("strongest", False)) for x in bands]})
        if name is None:
            notes.append("泳道 %d 还没有名字" % i)
    return {
        "version": 2,
        "units": {"x": "cm from gel-face left", "y": "cm from gel-face top", "gray": "0-255 (lower = darker)",
                  "ink": "baseline - gray (higher = more stain)"},
        "gel": {"width_cm": model["gel"]["width_cm"], "height_cm": model["gel"]["height_cm"],
                "px_per_cm": round(ppc, 3)},
        "window": {"top_cm": round(top, 4), "bottom_cm": round(bottom, 4)},
        "calibration": {"method": method, "marker_lane": model["marker_lane"],
                        "points": [[round(y, 4), m] for y, m in cal["pairs"]],
                        "fit": {"log10_mw": "a + b*y", "a": round(a, 6), "b": round(b, 6)},
                        "r2": round(r2, 4)},
        "params": {"base_half_cm": BASE_HALF_CM, "smooth_cm": SMOOTH_CM, "min_sep_cm": MIN_SEP_CM,
                   "k_noise": K_NOISE},
        "lanes": lanes, "notes": notes}
