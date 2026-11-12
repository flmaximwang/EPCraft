#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mw_report：分子量的数学（MW 槽位展开、两点插值、估计）与报告 v2 的输出。

marker 带「出生即 MW」（ep-bdt）：标签展开成 MW 槽位序列，第 k 条检出带（y 升序）直接挂第 k 个槽
（expand_marker_mws）—— 没有配对步骤。检出的条数比槽位少就只落前 k 条线，多了就按序挂满为止。
标定只有一种做法：相邻两条 marker 带（两点）之间按 log10(MW) 线性插值。**禁止全局拟合** ——
Tris-Tricine 胶上 log(MW) 对迁移距离不是直线，最小二乘会把 250 kD 压低四成、10 kD 抬高四成；
而且拟合出来的直线在标记点之间也不经过真实值。超出 marker 范围不外推，记 None + 超范围。
报告 v2 本身由 profiles.analyze 生成（spec §7），这里只负责终端表与写盘。
"""
import math
import re
import statistics


def parse_mw(text):
    """'250 kD' → [250.0]；'21/20 kD' → [21.0, 20.0]。解析不出数字返回 []。"""
    return [float(t) for t in re.findall(r"\d+(?:\.\d+)?", str(text))]


def expand_marker_mws(entries, as_entry):
    """marker 标签 → MW 槽位序列（一个槽 = 一条 band <MW> 线）。

    每个 label 占 max(1, rows) 个槽；parse_mw(text) 的数字按序填槽：第 k 条检出带（y 升序）直接挂
    第 k 个槽（ep-blz：没有配对步骤）。数字不够 → 尾部槽记 None（不落线）；数字有余 → 只用前 n 个。
    两种情况都出一条提示（槽位是 None 的地方照样按序往下数，后面的带不会因此挪位）。
    没有数字的标签整段是 None（调用方仍应当把它当配置错误拒绝：不知道那几个槽要挂什么 MW）。
    → (mws, notes)
    """
    mws, notes = [], []
    for e in entries:
        e = as_entry(e, {})
        text = e.get("text", "")
        n = max(1, int(e.get("rows", 1)))
        nums = parse_mw(text)
        mws.extend(nums[:n])
        if len(nums) < n:
            notes.append("marker 标签 %r 要 %d 行，只有 %d 个分子量数字：后面 %d 行没有 MW、不落线"
                         % (text, n, len(nums), n - len(nums)))
            mws.extend([None] * (n - len(nums)))
        elif len(nums) > n and n > 1:
            notes.append("marker 标签 %r 有 %d 个分子量数字但只占 %d 行：只挂前 %d 个"
                         % (text, len(nums), n, n))
        elif len(nums) > n:
            notes.append("marker 标签 %r 有 %d 个分子量数字但只占 1 行：只挂第一个（并带请写 "
                         "{\"text\": \"%s\", \"rows\": %d}）" % (text, len(nums), text, len(nums)))
    return mws, notes


SPLIT_GAP_RATIO = 0.4   # 劈峰判据：最小相邻间距 < 这个倍数 × 相邻间距中位数 → 提示「识别条带偏多」。
                        # 票里写的是 0.5，实测调低到 0.4：两张真梯子的最小间距 / 间距中位数都是 0.46
                        # （IMG_1586 用户手放的 11 条线、IMG_1496 的 11 条合成带），0.5 会对真梯子误报；
                        # 真劈成双峰的两条间距通常只有相邻带距的 1/3 以下。只提示，不并峰、不拦截。


def split_peak_note(ys):
    """检出 ≥3 条带时看间距模式：最小相邻间距 < SPLIT_GAP_RATIO × 相邻间距中位数 →
    「识别条带偏多」提示（点出最可疑的那一对），否则 None。不并峰、不拦（Q17：将错就错）。"""
    ys = sorted(float(y) for y in ys)
    if len(ys) < 3:
        return None
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    med = statistics.median(gaps)
    i = min(range(len(gaps)), key=lambda k: gaps[k])
    if gaps[i] >= SPLIT_GAP_RATIO * med:
        return None
    return ("识别条带偏多：y≈%.2f 与 y≈%.2f 疑似同一带劈成双峰，请微调" % (ys[i], ys[i + 1]))


def _log_at(cal, y):
    """cal = {'pairs': 按 y 升序} → (log10 MW 或 None, 是否超出 marker 范围)。

    只用**夹住 y 的那两条** marker 带做 log10(MW) 线性插值：标记点上的值就是标签值，
    两点之间按这条胶自己的局部斜率走。区间外给 None（不拿端点斜率外推 —— 不可信）。
    """
    ps = cal["pairs"]
    if len(ps) < 2:                            # 0 / 1 条标定线：插不出值，但唯一那个点仍留在报告里
        return None, True
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
            parts.append("%s%s%s (SNR %.0f)" % ("[主] " if b["strongest"] else "",
                                                "✎ " if b.get("manual") else "", mw, b["snr"]))
        lines.append("  泳道 %2d %-8s %s" % (ln["lane"], ln["label"], " | ".join(parts) or "（无条带）"))
    lines += ["  注：" + n for n in rep["notes"]]
    return "\n".join(lines)


def write_v2(rep, path):
    """写 v2 报告：UTF-8、不转义中文、紧凑（剖面数组很长），末尾换行。"""
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
