#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mw_report：分子量的数学（标定点配对、log 拟合、估计）与报告 v2 的输出。

标定：log10(MW) 对 marker 条带线的 y（胶面 cm）作一次最小二乘直线（logfit）；超出 marker 范围会外推，
并标 extrapolated。报告 v2 本身由 profiles.analyze 生成（spec §7），这里只负责终端表与写盘。
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


# ---------------------------------------------------------------- 报告 v2（spec §7，由 profiles.analyze 生成）

def format_report_v2(rep):
    """v2 报告的终端表：标定一行；每条有名字、非 marker、未裁掉的泳道一行（[主] = 显著度最大）。"""
    c = rep["calibration"]
    lines = ["[MW] 方法 %s；marker 泳道 %d，%d 点，R²=%.4f" % (c["method"], c["marker_lane"], len(c["points"]), c["r2"])]
    shown = [ln for ln in rep["lanes"]
             if not (ln["marker"] or ln["trimmed"] or not (ln["label"] or "").strip())]
    for ln in shown:
        parts = []
        for b in ln["bands"]:
            mw = "超出 marker 范围" if b["mw_kda"] is None else "%.1f kDa%s" % (b["mw_kda"], "*" if b["extrapolated"] else "")
            parts.append("%s%s (SNR %.0f)" % ("[主] " if b["strongest"] else "", mw, b["snr"]))
        lines.append("  泳道 %2d %-8s %s" % (ln["lane"], ln["label"], " | ".join(parts) or "（无条带）"))
    if any(b["extrapolated"] for ln in shown for b in ln["bands"]):        # 只为表里出现的带加注
        lines.append("  * = 超出 marker 范围的外推值，仅供参考")
    lines += ["  注：" + n for n in rep["notes"]]
    return "\n".join(lines)


def write_v2(rep, path):
    """写 v2 报告：UTF-8、不转义中文、紧凑（剖面数组很长），末尾换行。"""
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
