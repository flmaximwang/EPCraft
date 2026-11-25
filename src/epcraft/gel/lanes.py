#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""泳道号语法与 Figure 的保留段。只算版式，不碰图像。

    --trim-lanes → parse_keep() 解析泳道号（1 起，逗号/区间）
    figure.targets → strips() 把没 trim 的泳道切成连续的「段」，边界取**相邻泳道中心的中点**
                   → layout() 给每段在 Figure 里的左边缘（段间留拼缝白）

坐标口径：lanes_x_cm / gel_width_cm 都是**未裁切胶面**上的 cm（原点 = 那个面的左上角）。
"""
import re


# ---------------------------------------------------------------- --trim-lanes

def parse_keep(spec, lane_count):
    """'1,2,8-10' → [1, 2, 8, 9, 10]（1 起、去重、升序）。

    越界 / 空 / 语法错一律 RuntimeError，并在消息里点明是**哪一段**不对。
    """
    n = int(lane_count)
    if n < 1:
        raise RuntimeError("--trim-lanes：胶面上的泳道数必须 ≥1（收到 %r）" % (lane_count,))
    if spec is None:
        raise RuntimeError("--trim-lanes：没有给泳道号")
    s = str(spec).strip()
    if not s:
        raise RuntimeError("--trim-lanes 是空的；省略该参数 = 一条都不 trim，或写成如 1,2,14-15")

    out = set()
    for raw in s.split(","):
        tok = raw.strip()
        if not tok:
            raise RuntimeError("--trim-lanes 里有一段是空的：%r（逗号之间不能没有泳道号）" % s)
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", tok)
        if not m:
            raise RuntimeError("--trim-lanes 里有一段写法不对：%r（只接受 3 或 8-10 这种，1 起）" % tok)
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        if b < a:
            raise RuntimeError("--trim-lanes 里区间反了：%r（左号必须 ≤ 右号）" % tok)
        for k in (a, b):
            if k < 1 or k > n:
                raise RuntimeError("--trim-lanes 里第 %d 条越界：本胶只有 1–%d 条泳道（那段是 %r）"
                                   % (k, n, tok))
        out.update(range(a, b + 1))
    return sorted(out)


# ---------------------------------------------------------------- 段与版式

def _runs(keep):
    """升序泳道号 → 连续段 [[a..b], ...]。"""
    ks = sorted(set(int(k) for k in keep))
    runs, cur = [], [ks[0]]
    for k in ks[1:]:
        if k == cur[-1] + 1:
            cur.append(k)
        else:
            runs.append(cur)
            cur = [k]
    runs.append(cur)
    return runs


def strips(keep, lanes_x_cm, gel_width_cm, lanes_x_bottom_cm=None):
    """保留的原始号 → 每段 {lanes, src_x0_cm, src_x1_cm[, src_x0_bottom_cm, src_x1_bottom_cm]}。

    边界一律取**相邻泳道中心的中点**（不是带边、不是固定半间距）：
        左 = 与左边那条（未保留）泳道中心的中点；第 1 条 → 0
        右 = 与右边那条（未保留）泳道中心的中点；最后一条 → gel_width_cm
    `lanes_x_cm` 是**胶顶那一行**的泳道中心（版式按它排，拼缝也按它算 —— 用户看到的顺序就是这一行）。
    泳道是梯形时再给 `lanes_x_bottom_cm`（胶底那一行的中心）：每段多写两个 `src_*_bottom_cm`，
    Figure 按这四个端点在源图里取四边形（未裁切胶面的 cm）。省略 = 矩形泳道，输出与旧版逐键相同。
    """
    if not keep:
        raise RuntimeError("stage 4：keep 为空，没有可裁的泳道")
    n = len(lanes_x_cm)
    if n < 1:
        raise RuntimeError("stage 4：没有冻结的泳道中心")
    ks = sorted(set(int(k) for k in keep))
    if ks[0] < 1 or ks[-1] > n:
        raise RuntimeError("stage 4：keep 越界（1–%d）：%s" % (n, ks))
    x = [float(v) for v in lanes_x_cm]
    xb = [float(v) for v in lanes_x_bottom_cm] if lanes_x_bottom_cm is not None else None
    if xb is not None and len(xb) != n:
        raise RuntimeError("stage 4：胶底那一行的泳道中心有 %d 个，胶顶有 %d 个" % (len(xb), n))
    width = float(gel_width_cm)
    out = []
    for run in _runs(ks):
        first, last = run[0], run[-1]
        x0 = 0.0 if first == 1 else (x[first - 2] + x[first - 1]) / 2.0
        x1 = width if last == n else (x[last - 1] + x[last]) / 2.0
        if x1 <= x0:
            raise RuntimeError("stage 4：第 %d–%d 条算出来的段宽 <= 0（%.4f..%.4f cm）"
                               % (first, last, x0, x1))
        seg = {"lanes": list(run), "src_x0_cm": round(x0, 6), "src_x1_cm": round(x1, 6)}
        if xb is not None:                                   # 梯形：胶底那行的同口径边界
            b0 = 0.0 if first == 1 else (xb[first - 2] + xb[first - 1]) / 2.0
            b1 = width if last == n else (xb[last - 1] + xb[last]) / 2.0
            if b1 <= b0:
                raise RuntimeError("stage 4：第 %d–%d 条在胶底算出来的段宽 <= 0（%.4f..%.4f cm）"
                                   % (first, last, b0, b1))
            seg.update(src_x0_bottom_cm=round(b0, 6), src_x1_bottom_cm=round(b1, 6))
        out.append(seg)
    return out


def layout(strips, gap_cm):
    """给每段加 dst_x0_cm：第 k 段左边 = 前面各段宽 + k×gap。返回 (strips, 总宽_cm)。"""
    gap = float(gap_cm)
    if gap < 0:
        raise RuntimeError("--splice-gap-cm 不能是负数（收到 %g）" % gap)
    out, cursor = [], 0.0
    for i, s in enumerate(strips):
        if i:
            cursor += gap
        d = dict(s)
        d["dst_x0_cm"] = round(cursor, 6)
        out.append(d)
        cursor += float(s["src_x1_cm"]) - float(s["src_x0_cm"])
    return out, round(cursor, 6)


# ---------------------------------------------------------------- marker 策略
