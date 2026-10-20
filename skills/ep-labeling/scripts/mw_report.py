#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mw_report：用 marker 泳道的条带位置标定，估计样品泳道里每条自动探到的带的分子量。

标定：log10(MW) 对条带行 y（record 里是裁切后胶面的 cm；旧式 lb 是分析 px，两者只差仿射）。
两种方法（--mw-method）：
    logfit  对 log10(MW) ~ y 作一次最小二乘直线；超出 marker 范围会外推，并标 extrapolated。
    interp  相邻 marker 带之间对 log10(MW) 线性内插；范围外不外推，给 None。
多管 marker 泳道（lanes.json 的 marker_lanes）：每管各拟一条，样品泳道取左右最近的两管按距离反比加权。
"""
import math
import re


def parse_mw(text):
    """'250 kD' → [250.0]；'21/20 kD' → [21.0, 20.0]。解析不出数字返回 []。"""
    return [float(t) for t in re.findall(r"\d+(?:\.\d+)?", str(text))]


def marker_pairs(mk_labels, span, rows, as_entry):
    """(y, MW) 配对。标签盖 1 条带 → 取其第一个数字；盖 2 条带且文字里恰有 2 个数 → 各配各的；
    其余多带标签 → 取这组带的 y 均值配第一个数字。无法解析的标签跳过。"""
    pairs, ri = [], 0
    for e, k in zip(mk_labels, span):
        grp = rows[ri:ri + k]
        ri += k
        nums = parse_mw(as_entry(e, {}).get("text", ""))
        if not nums or not grp:
            continue
        if k == 1:
            pairs.append((grp[0], nums[0]))
        elif len(nums) == len(grp):
            pairs.extend(zip(grp, nums))
        else:
            pairs.append((sum(grp) / len(grp), nums[0]))
    return pairs


def fit_logmw(pairs):
    """log10(MW) = a + b·y 的最小二乘；返回 (a, b, R²)。至少 3 点。"""
    n = len(pairs)
    ys = [p[0] for p in pairs]
    ls = [math.log10(p[1]) for p in pairs]
    my, ml = sum(ys) / n, sum(ls) / n
    sxx = sum((y - my) ** 2 for y in ys)
    if sxx == 0:
        raise ValueError("marker 条带位置全相同，无法拟合")
    b = sum((y - my) * (l - ml) for y, l in zip(ys, ls)) / sxx
    a = ml - b * my
    ss_tot = sum((l - ml) ** 2 for l in ls)
    ss_res = sum((l - (a + b * y)) ** 2 for y, l in zip(ys, ls))
    return a, b, (1.0 - ss_res / ss_tot) if ss_tot else 1.0


def _log_at(cal, y, method):
    """cal = {'pairs': 按 y 升序, 'fit': (a,b,r2)} → (log10 MW 或 None, 是否外推)。"""
    ps = cal["pairs"]
    inside = ps[0][0] <= y <= ps[-1][0]
    if method == "logfit":
        a, b, _ = cal["fit"]
        return a + b * y, not inside
    if not inside:
        return None, True
    for (y0, m0), (y1, m1) in zip(ps, ps[1:]):
        if y0 <= y <= y1:
            t = 0.0 if y1 == y0 else (y - y0) / (y1 - y0)
            return math.log10(m0) + t * (math.log10(m1) - math.log10(m0)), False
    return None, True


def build_cal(pairs):
    ps = sorted(pairs)
    return {"pairs": ps, "fit": fit_logmw(ps) if len(ps) >= 3 else None}


def estimate(y, x, cals, method):
    """cals = [(泳道 x, cal)]；取左右最近各一管按距离反比加权。→ (MW kDa 或 None, extrapolated)。"""
    left = [c for c in cals if c[0] <= x]
    right = [c for c in cals if c[0] > x]
    pick = []
    if left:
        pick.append(max(left, key=lambda c: c[0]))
    if right:
        pick.append(min(right, key=lambda c: c[0]))
    vals = []
    for cx, cal in pick:
        lg, ex = _log_at(cal, y, method)
        if lg is not None:
            vals.append((1.0 / max(1e-6, abs(x - cx)), lg, ex))
    if not vals:
        return None, True
    tw = sum(v[0] for v in vals)
    return 10 ** (sum(v[0] * v[1] for v in vals) / tw), any(v[2] for v in vals)


def report(rec, lane_labels, marker_idx, primary_idx, mk_labels, span, as_entry,
           method="logfit"):
    """生成报告 dict。

    rec：stage 3 之后产的探测记录（record JSON）—— y 是裁切后胶面的 cm，x 是 cm。
         也接受旧式 lb dict（lanes 为 (x, width) 元组、bands 为 (y, width, depth) 元组），
         两者只是坐标口径不同，标定数学不变（log10(MW) ~ y 是仿射的，R²/斜率在 cm 里同样成立）。
    lane_labels：lanes.json 的 labels 文本（按泳道序，可短于泳道数）；
    marker_idx：marker 泳道的 0 起**原始泳道号**集合；primary_idx：配置标定过的那管（0 起原始号）。

    与旧版的两点语义差异（spec §7 / T5）：
      - record 的泳道 x/y 一律取 cm；
      - record 里 estimated 的泳道 **不跳过**（旧 lb 的 interp 是「完全没像素证据」，语义不同）——
        只要它有标定、有标签、有带，就照常报告，只在 notes 里注明位置是估的。
    """
    is_record = isinstance(rec.get("bands"), dict)
    need = sum(span)
    if is_record:
        lane_nos = [int(ln["index"]) for ln in rec["lanes"]]
        lanes_x = [float(ln["x_cm"]) for ln in rec["lanes"]]
        estimated = [bool(ln.get("estimated")) for ln in rec["lanes"]]

        def _rows(i0):
            return [float(b["y_cm"]) for b in rec["bands"].get(str(i0 + 1), [])]

        band_rows = [_rows(n - 1) for n in lane_nos]
        band_depths = [[float(b.get("depth", 0.0)) for b in rec["bands"].get(str(n), [])]
                       for n in lane_nos]
    else:
        lane_nos = list(range(1, len(rec["lanes"]) + 1))
        lanes_x = [float(c) for c, _ in rec["lanes"]]
        estimated = [bool(v) for v in rec.get("interp", [False] * len(lanes_x))]

        def _rows(i0):
            return [float(b[0]) for b in rec["bands"][i0]]

        band_rows = [[float(b[0]) for b in bs] for bs in rec["bands"]]
        band_depths = [[float(b[2]) for b in bs] for bs in rec["bands"]]

    cals, notes = [], []
    for i in sorted(marker_idx):
        if is_record:
            # i 是**原始**泳道号（0 起）；record 里 lanes 只含保留泳道，先找它在版式里的位置。
            pos = lane_nos.index(i + 1) if (i + 1) in lane_nos else None
            if pos is None:
                notes.append("marker_lanes 含第 %d 条，不在 record 的 lanes 里" % (i + 1))
                continue
            x_m = lanes_x[pos]
        else:
            if i >= len(lanes_x):
                notes.append("marker_lanes 含第 %d 条，超出探到的 %d 条泳道" % (i + 1, len(lanes_x)))
                continue
            x_m = lanes_x[i]
        ys = _rows(i)
        if i != primary_idx and len(ys) != need:
            notes.append("marker 泳道 %d 探到 %d 条带，与标签所需 %d 不符，未用于标定" % (i + 1, len(ys), need))
            continue
        # 主标定泳道也直接吃 record 的 y（不再从渲染时可能被 y_px 覆盖的 rows 传进来）。
        pairs = marker_pairs(mk_labels, span, ys, as_entry)
        if len(pairs) < 3:
            notes.append("marker 泳道 %d 可用标定点 <3，未用于标定" % (i + 1))
            continue
        cals.append((x_m, build_cal(pairs), i))
    if not cals:
        return {"method": method, "calibration": [], "lanes": [], "notes": notes + ["没有可用的 marker 标定，未出报告"]}
    out = []
    for pos, rows in enumerate(band_rows):
        label = lane_labels[pos] if pos < len(lane_labels) else ""
        if lane_nos[pos] - 1 in marker_idx or not str(label).strip():
            continue
        if not is_record and estimated[pos]:
            notes.append("泳道 %d（%s）是补位的估计位置，没有报告" % (lane_nos[pos], label))
            continue
        if not rows:
            continue
        if is_record and estimated[pos]:
            notes.append("泳道 %d（%s）的位置是估计值（x 停在先验格点），分子量照常报告"
                         % (lane_nos[pos], label))
        top = max(range(len(rows)), key=lambda k: band_depths[pos][k])
        items = []
        for k, y in enumerate(rows):
            mw, ex = estimate(y, lanes_x[pos], [(c, cal) for c, cal, _ in cals], method)
            items.append({"y": round(y, 1), "mw_kda": None if mw is None else round(mw, 1),
                          "extrapolated": bool(ex), "depth": round(band_depths[pos][k], 1),
                          "strongest": k == top})
        out.append({"lane": lane_nos[pos], "label": str(label), "bands": items})
    return {"method": method,
            "calibration": [{"marker_lane": i + 1, "points": [[round(y, 1), m] for y, m in cal["pairs"]],
                             "r2": None if cal["fit"] is None else round(cal["fit"][2], 4)}
                            for _, cal, i in cals],
            "lanes": out, "notes": notes}


def format_report(rep):
    lines = ["[MW] 方法 %s；标定：%s" % (rep["method"], "；".join(
        "泳道 %d（%d 点%s）" % (c["marker_lane"], len(c["points"]),
                               "" if c["r2"] is None else ", R²=%.4f" % c["r2"]) for c in rep["calibration"]) or "无")]
    for ln in rep["lanes"]:
        parts = []
        for b in ln["bands"]:
            mw = "超出 marker 范围" if b["mw_kda"] is None else "%.1f kDa%s" % (b["mw_kda"], "*" if b["extrapolated"] else "")
            parts.append("%s%s" % ("[主] " if b["strongest"] else "", mw))
        lines.append("  泳道 %2d %-8s %s" % (ln["lane"], ln["label"], " | ".join(parts)))
    if any(b["extrapolated"] for ln in rep["lanes"] for b in ln["bands"]):
        lines.append("  * = 超出 marker 范围的外推值，仅供参考")
    lines += ["  注：" + n for n in rep["notes"]]
    return "\n".join(lines)
