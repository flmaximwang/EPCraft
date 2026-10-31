#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Workbench 出分析：每条泳道的一维灰度剖面、条带、marker 标定、分子量（spec §7）。

输入只有两样：workbench 模型（cm，胶面左上角为原点）和文档里那张胶面图（PNG 字节）。
render（探测出来的线）和 sync（用户改过的线）走同一份代码。

1. 剖面：积分窗 [top, bottom] 内每一像素行，取 x ∈ [L, R] 的平均灰度（列中心落在区间里的像素）。
2. 基线与墨量：沿用 stage 3 实测调好的 top-hat —— 基线 = 宽窗滑动均值，ink = 基线 − 灰度
   （越暗越大）；窗宽、平滑、最小峰距改用 cm 表示，所以与胶面图的分辨率无关。
3. 条带：平滑后原始灰度的局部变暗，**局部**显著度（比两侧浅点深多少）≥ K_NOISE × 该泳道背景噪声，
   **没有绝对墨量下限**（失效报告 §6 P2-2：本批样品带只深 3–4 灰度级，绝对阈值会把整条淡道丢掉）。
   两端留一段不取峰：top-hat 的基线在剖面端点被截断（conventions §2c 的老坑）。
4. 标定：`band <MW>` 线的 y ↔ MW，**只做相邻两点之间的 log10(MW) 线性插值**（不做全局拟合），与 mw_report 同一套数学。

报告 v2 的坐标一律是胶面 cm（与 record 同口径），不存照片像素。
"""
import os
import tempfile

from magick_io import read_ppm, to_gray
from mw_report import build_cal, estimate


BASE_HALF_CM = 0.30      # top-hat 基线半窗（stage 3：20 分析 px ≈ 0.33 cm；再宽会吃掉并带的显著度）
SMOOTH_CM = 0.03         # 轻平滑半窗（stage 3：3 行 ≈ 0.03 cm；再重会抹平 2–3 px 高的窄带）
MIN_SEP_CM = 0.05        # 两峰最小间隔（并带 21/20 kD 的两条约隔 0.12 cm）
FLANK_CM = 0.15          # 局部显著度的两侧比较范围（= 基线半窗的一半；IMG_1496 实测比整窗少叫背景纹理）
K_NOISE = 4.0            # 显著度 ≥ K_NOISE × 噪声（只用于样品道；marker 道见 pick_marker）
GAP_RATIO = 2.0          # marker 道：第 N 名显著度 ≥ GAP_RATIO × 第 N+1 名（断崖闸门）。实测真梯子：IMG_1586
                         # 修好裁切框后 2.7×（旧裁剪 7.3×）、合成 IMG_1496 3.8×；非梯子道 ≤ 1.7× —— 取中间偏下
MARKER_MIN_PROM = 1.5    # marker 道：第 N 名显著度 ≥ 这么多灰度级（整道没墨量时别硬凑）
SNAP_CM = 0.06           # marker 道的 band 线吸附半径：线离最近的局部极小 ≤ 这么多就吸过去（用户手放的线偏 0.01–0.03）
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


def _peaks(gs, px_per_cm):
    """平滑原始灰度的全部局部极小（**不带门槛**）→ (dark, [(i, prominence)])，按 i 升序。

    call_bands 与 pick_marker 共用：样品道按噪声门槛筛，marker 道不筛、按显著度取前 N 名（ep-9ji）。
    平滑、FLANK、端点留白（两端各让开 top-hat 基线窗）都在这里，两边口径一致。
    """
    n = len(gs)
    if n < 5:
        return [], []
    sm_w = max(1, int(round(SMOOTH_CM * px_per_cm)))
    pre = [0.0]
    for v in gs:
        pre.append(pre[-1] - v)
    dark = [(pre[min(n, i + sm_w + 1)] - pre[max(0, i - sm_w)]) / (min(n, i + sm_w + 1) - max(0, i - sm_w))
            for i in range(n)]                                  # 平滑后的 −灰度：越暗越大
    win = max(3, int(round(BASE_HALF_CM * px_per_cm)))
    fw = max(2, int(round(FLANK_CM * px_per_cm)))
    a_margin, b_margin = max(2, int(0.5 * win)), win
    raw = []
    for i in range(max(1, a_margin), min(n - 1, n - b_margin)):
        if not (dark[i] >= dark[i - 1] and dark[i] >= dark[i + 1] and (dark[i] > dark[i - 1] or dark[i] > dark[i + 1])):
            continue
        raw.append((i, _prominence(dark, i, fw)))
    return dark, raw


def _prominence(dark, i, fw):
    """局部显著度：dark[i] 比两侧 FLANK_CM 内各自最浅点中**较深**的那个还深多少（与 fw 的算法同源）。"""
    n = len(dark)
    return dark[i] - max(min(dark[max(0, i - fw):i + 1]), min(dark[i:min(n, i + fw + 1)]))


def _merge_peaks(raw, px_per_cm):
    """平台/锯齿 → 把相隔 ≤ MIN_SEP_CM 的候选并成一条（留显著度大的）。"""
    min_sep = max(2, int(round(MIN_SEP_CM * px_per_cm)))
    keep = []
    for i, prom in raw:
        if keep and i - keep[-1][0] <= min_sep:
            if prom > keep[-1][1]:
                keep[-1] = (i, prom)
        else:
            keep.append((i, prom))
    return keep


def _band_row(ys, dark, px_per_cm, i, prom):
    """一条峰 → 报告里的带（y_cm / prominence / width_cm）。宽度 = 半高宽（相对局部显著度的一半）。"""
    n = len(dark)
    half = dark[i] - prom / 2.0
    a, b = i, i
    while a > 0 and dark[a - 1] > half:
        a -= 1
    while b + 1 < n and dark[b + 1] > half:
        b += 1
    return {"y_cm": round(ys[i], 4), "prominence": round(prom, 2),
            "width_cm": round((b - a + 1) / px_per_cm, 4)}


def measure_band(ys, gs, y_cm, px_per_cm, noise):
    """在一个给定 y 上按叫带的同一口径量手加带 → {y_cm, prominence, snr, width_cm}（spec §7.5）。

    y 用画出来的原值（不吸到最近的局部极小），指标在最接近的那一行上量。显著度 ≤ 0 记 0、带照常保留：
    用户在空白处补一条也是他的意思，看得出 SNR 为 0。
    """
    dark, _raw = _peaks(gs, px_per_cm)
    if not ys or not dark:
        return {"y_cm": round(float(y_cm), 4), "prominence": 0.0, "snr": 0.0, "width_cm": 0.0}
    fw = max(2, int(round(FLANK_CM * px_per_cm)))
    i = min(range(len(ys)), key=lambda k: abs(ys[k] - y_cm))
    prom = max(0.0, _prominence(dark, i, fw))
    row = _band_row(ys, dark, px_per_cm, i, prom)
    return {"y_cm": round(float(y_cm), 4), "prominence": row["prominence"],
            "snr": round(prom / noise, 1), "width_cm": row["width_cm"]}


def _override_ys(values):
    """override 里的 y 列表 → float 列表；读坏的非数字整段忽略（ep-meta 是 agent 写的，不该因此崩）。"""
    try:
        return [float(v) for v in (values or [])]
    except (TypeError, ValueError):
        return []


def pick_marker(ys, gs, px_per_cm, need, gap_ratio=GAP_RATIO, min_prom=MARKER_MIN_PROM):
    """marker 道 → (按 y 升序的 need 条带, info)。**不用噪声门槛**（spec ep-9ji D1/D2）。

    梯子道的带数由标签决定（need = 标签 rows 之和），所以不从「噪声 × k」反推「有几条」：
    取局部极小里显著度最高的 need 个。噪声在梯子道上量的是条带本身（IMG_1586 第 3 道
    σ = 17.2，其余道 0.4–2.2），任何 ≥1σ 的门槛都会丢掉淡带（20 kD 显著度只有 7.4）。
    剩下的风险是「取到的不是梯子带」（气泡、污点、坏道），用两条闸门代替原来的带数闸门：

    a) 候选数 ≥ need；
    b) 断崖：第 need 名 ≥ gap_ratio × 第 need+1 名（没有第 need+1 名视为通过）——
       真梯子与纹理之间有数量级的空当（IMG_1586 实测 7.38 / 1.01 = 7.3×）；
    c) 下限：第 need 名 ≥ min_prom 灰度级（整条道几乎没墨量时别硬凑）。

    **不过闸门也照样返回前 need 名**（排好序，可能不足 need 条），由 info["ok"] / info["reason"]
    说明可不可信：调用方拿它当草稿的起点（render 的 draft）、换下一条候选道，或直接报错。
    info 另带 ratio / nth / next（断崖两端的显著度），供终端打印与 ep-meta 的 draft.gate。
    """
    dark, raw = _peaks(gs, px_per_cm)
    need = int(need)
    ranked = sorted(_merge_peaks(raw, px_per_cm), key=lambda p: -p[1]) if raw else []
    info = {"ok": False, "reason": "", "n_cands": len(ranked), "need": need,
            "ratio": None, "nth": None, "next": None}
    if not dark:
        info["reason"] = "剖面太短"
        return [], info
    if need <= 0:
        info.update(ok=True, reason="need = 0")
        return [], info
    if len(ranked) < need:                                     # 闸门 a
        info["reason"] = "局部极小只有 %d 个，要 %d 个" % (len(ranked), need)
    top = ranked[:need]
    rows = sorted((_band_row(ys, dark, px_per_cm, i, prom) for i, prom in top), key=lambda r: r["y_cm"])
    if not top or not info["reason"]:
        nth = top[-1][1]
        nxt = ranked[need][1] if len(ranked) > need else None   # 闸门 b：断崖
        info.update(nth=round(nth, 2), next=None if nxt is None else round(nxt, 2),
                    ratio=None if not nxt else round(nth / nxt, 2))
        if nth < min_prom:                                     # 闸门 c：下限
            info["reason"] = "第 %d 名显著度 %.2f < 下限 %.2f 灰度级" % (need, nth, min_prom)
        elif nxt and nth < gap_ratio * nxt:
            info["reason"] = "断崖不够：第 %d 名 %.2f 只有第 %d 名 %.2f 的 %.2f 倍（要 %.1f 倍）" % (
                need, nth, need + 1, nxt, nth / nxt, gap_ratio)
    info["ok"] = not info["reason"]
    return rows, info


def snap_lines(lines, ys, gs, px_per_cm, snap_cm=SNAP_CM):
    """marker 道的 band 线 → 带有量测值的带（吸附到 ±snap_cm 内最近的局部极小）。

    marker 道的带是**用户（或 render）放好的线**，不再在这个剖面上叫峰（spec ep-9ji D4）：
    带数、MW、y 都由线给，剖面只补 prominence / width_cm 两个画图与报告用的量。
    找到极小 → y = 极小位置、snapped = True；找不到（线落在平地上）、或两条线抢同一个极小
    （离得远的那条让出）→ 在线自己的位置上量、snapped = False。
    **线本身不动**（标定仍用线的 y）—— 否则 render 后第一次 sync 就会生成移动操作。
    返回 (带列表 y 升序, 提示语列表)。
    """
    dark, raw = _peaks(gs, px_per_cm)
    merged = _merge_peaks(raw, px_per_cm)
    fw = max(2, int(round(FLANK_CM * px_per_cm)))
    n = len(ys)
    out, used, notes = [], set(), []
    for ln in sorted(lines, key=lambda b: b["y_cm"]):
        y0, mw = float(ln["y_cm"]), float(ln["mw"])
        i_line = min(n - 1, max(0, int(round(y0 * px_per_cm - 0.5)))) if n else 0
        best = None
        for i, prom in merged:
            d = abs(ys[i] - y0)
            if d <= snap_cm and (best is None or (d, -prom) < (best[0], -best[2])):
                best = (d, i, prom)
        if best and best[1] not in used:
            d, i, prom = best
            used.add(i)
            row, y, snapped = _band_row(ys, dark, px_per_cm, i, prom), round(ys[i], 4), True
        else:
            if best:
                notes.append("marker 带 %g kD 与相邻的线抢同一个局部极小，按线的位置量" % mw)
            else:
                notes.append("marker 带 %g kD 在 ±%.2f cm 内没有局部极小，按线的位置量" % (mw, snap_cm))
            row = _band_row(ys, dark, px_per_cm, i_line, _prominence(dark, i_line, fw)) if n else \
                {"y_cm": round(y0, 4), "prominence": 0.0, "width_cm": 0.0}
            y, snapped = round(y0, 4), False
        out.append({"y_cm": y, "prominence": row["prominence"], "width_cm": row["width_cm"],
                    "mw_kda": round(mw, 2), "extrapolated": False, "line_y_cm": round(y0, 4), "snapped": snapped})
    return out, notes


def call_bands(ys, gs, sm, px_per_cm, k=K_NOISE):
    """条带 → ([{y_cm, prominence, snr, width_cm}], noise)。

    gs = 原始平均灰度剖面，sm = 平滑后的 ink（只用来量噪声，见 noise_of）。
    峰与显著度都在**平滑后的原始灰度**上量：峰 = 灰度的局部极小（变暗处），显著度 = 比两侧
    FLANK_CM 内各自最浅点中**较深**的那个还深多少；门槛 = k × noise。
    为什么不在 ink 上量：top-hat 的滑动均值基线在浓带处下陷，于是浓带两旁的 ink 是负瓣、
    两条浓带之间的平背景反倒成了「鼓包」—— 合成梯子上 4 条带叫出 7 条（3 条是带间假峰）；
    原始灰度没有基线，没有负瓣也没有鼓包，同一例正好 4 条。也不用「高出 0」：浓带旁的淡带
    在 ink 上是负的（合成梯子的 250 kD 为 −1.8），在原始灰度上它仍是一个清楚的局部变暗。
    慢变化的背景（胶面不匀、胶底渐变）由「只跟 FLANK_CM 以内比」挡掉，两端各留一段不取峰。
    """
    n = len(sm)
    noise = noise_of(sm)
    if n < 5:
        return [], noise
    dark, raw = _peaks(gs, px_per_cm)
    thr = k * noise
    out = []
    for i, prom in _merge_peaks([p for p in raw if p[1] >= thr], px_per_cm):
        r = _band_row(ys, dark, px_per_cm, i, prom)
        out.append({"y_cm": r["y_cm"], "prominence": r["prominence"],
                    "snr": round(prom / noise, 1), "width_cm": r["width_cm"]})
    return out, noise


# ---------------------------------------------------------------- 整份报告

def analyze(model, png, lane_names, overrides=None, raw=None):
    """workbench 模型 + 胶面图 → 报告 v2 dict（spec §7）。lane_names 与 model["lanes"] 一一对应（None = 未命名）。

    overrides = {泳道号: {"suppress": [y_cm…], "add": [y_cm…]}}（键 str 或 int 都认；没列出的道全自动）：
    样品道里 suppress 去掉检测器叫出的、落在 MIN_SEP_CM 以内的带；add 在画出的 y 上按 call_bands 的同一口径
    量一条带（manual=True，不吸到最近的局部极小，spec §7.5）。raw 是 dict 时填入**抑制之前**的检测器叫带
    {泳道号: [y_cm…]}，供 ep-meta 的 peak_auto 跟下一轮文档差分（§4.6）。

    分子量只在**相邻两条 marker 带之间**按 log10(MW) 线性插值（mw_report._log_at）：标记点上的值就是标签值，
    点之间按这条胶的局部斜率走。**不做全局拟合**（Tris-Tricine 上直线拟合会把 250 kD 压低四成、10 kD 抬高四成）。
    超出 marker 范围的带记 mw_kda=None + extrapolated=True，不给数字；手加的带也一样。
    **marker 道不叫峰**（spec ep-9ji D4）：它的带 = `band <MW>` 线，吸附到 ±SNAP_CM 内最近的局部极小补
    prominence / width_cm（y_cm = 极小位置、line_y_cm = 线、snapped 标记贴没贴上），MW 就是线自己的标签值。
    样品道照旧按噪声门槛叫（band_source = "called"）；marker 道不收样品带 override（§4.3：画在那里的刻度忽略）。
    """
    w, h, gray = gel_gray(png)
    ppc = w / float(model["gel"]["width_cm"])
    if abs(h / ppc - model["gel"]["height_cm"]) > max(0.02, 2.0 / ppc):
        raise RuntimeError("胶面图 %dx%d px 与胶面 %.3f×%.3f cm 不成比例"
                           % (w, h, model["gel"]["width_cm"], model["gel"]["height_cm"]))
    top = max(0.0, model["window"]["top_cm"])
    bottom = min(model["gel"]["height_cm"], model["window"]["bottom_cm"])
    pairs = [(b["y_cm"], b["mw"]) for b in model["bands"]]
    if len(pairs) < 3:
        raise RuntimeError("marker 条带线不足 3 条，无法标定")
    cal = build_cal(pairs)
    ovs = overrides if isinstance(overrides, dict) else {}
    lanes, notes = [], []
    for i, ln in enumerate(model["lanes"], 1):
        ys, gs = lane_profile(gray, w, h, ppc, ln["left_cm"], ln["right_cm"], top, bottom)
        base, ink, sm = ink_trace(gs, ppc)
        marker = i == model["marker_lane"]
        if marker:            # 带由 band 线给（吸附最近的极小补量测），不在 marker 道上叫峰（spec ep-9ji D4）
            bands, mnotes = snap_lines(model["bands"], ys, gs, ppc)
            noise = noise_of(sm)                   # 照旧算、照旧写；它不参与 marker 道的任何判定
            notes.extend(mnotes)
        else:
            called, noise = call_bands(ys, gs, sm, ppc)
            if raw is not None:
                raw[i] = [b["y_cm"] for b in called]          # 抑制之前：peak_auto 的差分基准
            ov = ovs.get(i, ovs.get(str(i)))
            ov = ov if isinstance(ov, dict) else {}
            suppress = _override_ys(ov.get("suppress"))
            adds = _override_ys(ov.get("add"))
            merged = [(bd, False) for bd in called
                      if not any(abs(bd["y_cm"] - sy) <= MIN_SEP_CM + 1e-9 for sy in suppress)]
            merged += [(measure_band(ys, gs, ay, ppc, noise), True) for ay in adds]
            merged.sort(key=lambda x: x[0]["y_cm"])           # 自上而下（手加带照它的 y 落位）
            cx = (ln["left_cm"] + ln["right_cm"]) / 2.0
            bands = []
            for bd, manual in merged:
                mw, ex = estimate(bd["y_cm"], cx, [(cx, cal)])
                bands.append(dict(bd, mw_kda=None if mw is None else round(mw, 2),
                                  extrapolated=bool(ex), manual=manual))
        for b in bands:                                       # marker 道的带来自线，不是手加
            b.setdefault("manual", False)
        if bands:
            max(bands, key=lambda x: x["prominence"])["strongest"] = True
        name = lane_names[i - 1] if i - 1 < len(lane_names) else None
        lanes.append({
            "lane": i, "label": name, "marker": marker, "trimmed": bool(ln["trimmed"]),
            "band_source": "lines" if marker else "called",
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
        "calibration": {"method": "interp", "marker_lane": model["marker_lane"],
                        "points": [[round(y, 4), m] for y, m in cal["pairs"]]},
        "params": {"base_half_cm": BASE_HALF_CM, "smooth_cm": SMOOTH_CM, "min_sep_cm": MIN_SEP_CM,
                   "k_noise": K_NOISE, "snap_cm": SNAP_CM},
        "lanes": lanes, "notes": notes}
