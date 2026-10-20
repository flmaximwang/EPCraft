#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mw_report：用 marker 泳道的条带位置标定，估计样品泳道里每条自动探到的带的分子量。

标定：log10(MW) 对条带行 y（分析尺度）。Rf 对 y 是仿射关系，所以直接对 y 拟合，R² 相同。
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


def report(lb, lane_labels, marker_idx, primary_idx, primary_pairs, mk_labels, span, as_entry,
           method="logfit"):
    """生成报告 dict。
    lb：lanes_and_bands 的结果；lane_labels：lanes.json 的 labels 文本（按泳道序，可短于泳道数）；
    marker_idx：marker 泳道的 0 起下标集合；primary_idx/primary_pairs：配置标定过的那管及其 (y,MW)。"""
    lanes_x = [c for c, _ in lb["lanes"]]
    need = sum(span)
    cals, notes = [], []
    for i in sorted(marker_idx):
        if i >= len(lanes_x):
            notes.append("marker_lanes 含第 %d 条，超出探到的 %d 条泳道" % (i + 1, len(lanes_x)))
            continue
        if i == primary_idx:
            pairs = primary_pairs
        else:
            ys = [b[0] for b in lb["bands"][i]]
            if len(ys) != need:
                notes.append("marker 泳道 %d 探到 %d 条带，与标签所需 %d 不符，未用于标定" % (i + 1, len(ys), need))
                continue
            pairs = marker_pairs(mk_labels, span, ys, as_entry)
        if len(pairs) < 3:
            notes.append("marker 泳道 %d 可用标定点 <3，未用于标定" % (i + 1))
            continue
        cals.append((lanes_x[i], build_cal(pairs), i))
    if not cals:
        return {"method": method, "calibration": [], "lanes": [], "notes": notes + ["没有可用的 marker 标定，未出报告"]}
    out = []
    for i, bands in enumerate(lb["bands"]):
        label = lane_labels[i] if i < len(lane_labels) else ""
        if i in marker_idx or not str(label).strip():
            continue
        if lb["interp"][i]:
            notes.append("泳道 %d（%s）是补位的估计位置，没有报告" % (i + 1, label))
            continue
        if not bands:
            continue
        top = max(range(len(bands)), key=lambda k: bands[k][2])
        items = []
        for k, b in enumerate(bands):
            mw, ex = estimate(b[0], lanes_x[i], [(c, cal) for c, cal, _ in cals], method)
            items.append({"y": round(b[0], 1), "mw_kda": None if mw is None else round(mw, 1),
                          "extrapolated": bool(ex), "depth": round(b[2], 1), "strongest": k == top})
        out.append({"lane": i + 1, "label": str(label), "bands": items})
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
