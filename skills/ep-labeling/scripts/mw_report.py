#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mw_report：分子量的数学（标定点配对、两点插值、估计）与报告 v2 的输出。

标定只有一种做法：相邻两条 marker 带（两点）之间按 log10(MW) 线性插值。**禁止全局拟合** ——
Tris-Tricine 胶上 log(MW) 对迁移距离不是直线，最小二乘会把 250 kD 压低四成、10 kD 抬高四成；
而且拟合出来的直线在标记点之间也不经过真实值。超出 marker 范围不外推，记 None + 超范围。
报告 v2 本身由 profiles.analyze 生成（spec §7），这里只负责终端表与写盘。
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


def _log_at(cal, y):
    """cal = {'pairs': 按 y 升序} → (log10 MW 或 None, 是否超出 marker 范围)。

    只用**夹住 y 的那两条** marker 带做 log10(MW) 线性插值：标记点上的值就是标签值，
    两点之间按这条胶自己的局部斜率走。区间外给 None（不拿端点斜率外推 —— 不可信）。
    """
    ps = cal["pairs"]
    if y < ps[0][0] or y > ps[-1][0]:
        return None, True
    for (y0, m0), (y1, m1) in zip(ps, ps[1:]):
        if y0 <= y <= y1:
            t = 0.0 if y1 == y0 else (y - y0) / (y1 - y0)
            return math.log10(m0) + t * (math.log10(m1) - math.log10(m0)), False
    return None, True


def build_cal(pairs):
    """标定点（marker 带 y + 它的 MW），按 y 升序。只排序，不拟合任何曲线。"""
    ps = sorted(pairs)
    if len(ps) < 2:
        raise ValueError("marker 条带线不足 2 条，无法在两点之间插值")
    return {"pairs": ps}


def estimate(y, x, cals):
    """cals = [(泳道 x, cal)]；取左右最近各一管按距离反比加权。→ (MW kDa 或 None, 超出范围)。"""
    left = [c for c in cals if c[0] <= x]
    right = [c for c in cals if c[0] > x]
    pick = []
    if left:
        pick.append(max(left, key=lambda c: c[0]))
    if right:
        pick.append(min(right, key=lambda c: c[0]))
    vals = []
    for cx, cal in pick:
        lg, ex = _log_at(cal, y)
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
    lines = ["[MW] 方法 %s：相邻两条 marker 带之间按 log10(MW) 线性插值（不做全局拟合）；marker 泳道 %d，%d 点"
             % (c["method"], c["marker_lane"], len(c["points"]))]
    shown = [ln for ln in rep["lanes"]
             if not (ln["marker"] or ln["trimmed"] or not (ln["label"] or "").strip())]
    for ln in shown:
        parts = []
        for b in ln["bands"]:
            mw = "超出 marker 范围" if b["mw_kda"] is None else "%.1f kDa" % b["mw_kda"]
            parts.append("%s%s (SNR %.0f)" % ("[主] " if b["strongest"] else "", mw, b["snr"]))
        lines.append("  泳道 %2d %-8s %s" % (ln["lane"], ln["label"], " | ".join(parts) or "（无条带）"))
    lines += ["  注：" + n for n in rep["notes"]]
    return "\n".join(lines)


def write_v2(rep, path):
    """写 v2 报告：UTF-8、不转义中文、紧凑（剖面数组很长），末尾换行。"""
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
