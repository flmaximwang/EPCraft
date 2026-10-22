#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage 1：探测 → 冻结的胶面矩形（形状由胶体预设决定）。

管线是严格串行的（spec §2 决策 3）：胶面 → 泳道 → 条带，每一级只读上一级的冻结输出。
本模块只做第一级，不碰泳道、条带、坐标记录：

    info（detect_gel 的返回）
      ├─ geometry.rotated_rect_bbox → 旋转后画布里的探测框
      ├─ 定标：探测框宽 / 物理宽（整块 = preset width_cm；裁过的 = gel_lanes × lane_pitch_cm）
      ├─ 定形：高 = (height_cm − trims) × px_per_cm，按 anchor 取边（宽仍由探测框决定）
      └─ 交叉核对：高度、尺度、出画布 —— 只打 warning，不改图

为什么高度要信预设、宽度信探测框：
- 宽度定标的前提是「画面里就是整块胶」（或用户说了只含 N 条泳道），这个假设给的是**尺度**；
- 探测框的上缘取的是胶板（孔底）那条边，不是上样孔之间那排齿（gel_detect.trim_bbox 的 top_cov），
  所以预设 height_cm 的口径 = **孔底到下缘的胶板高**；两者是同一条物理边，正常只差 1~2 个分析 px；
- 宽（尺度）沿用探测框，高（形状）由预设给，两者偏差 > HEIGHT_TOL（5%）就报出来，不改图。

本模块只依赖 magick_io / geometry（底层）——不 import lane_fit / band_fit：stage 1 不能反向依赖 stage 2/3。
"""
from geometry import load_dims_after, rotated_rect_bbox
from magick_io import read_ppm, to_gray

TOL = 0.10          # 尺度交叉核对的容差（spec §4.4：>10% 才报）
HEIGHT_TOL = 0.05   # 高度核对的容差（spec §4.4）：探测框上缘已是胶板物理边，参考照片偏差 <1%


# ---------------------------------------------------------------- 交叉核对

def cross_check_height(detected_h_cm, preset_h_cm, tol=HEIGHT_TOL):
    """探测框（未定形前）按宽度定标推出来的高 vs 预设 height_cm。只警告，不改图。"""
    dev = abs(detected_h_cm - preset_h_cm) / preset_h_cm if preset_h_cm else 0.0
    ok = dev <= tol
    if not ok:
        print("[!] height: 探测框按宽度定标推出来高 %.2f cm，预设 height_cm = %.2f cm（差 %.1f%%）"
              "—— 仍按预设定形，不改图" % (detected_h_cm, preset_h_cm, dev * 100.0))
    return {"name": "height", "ok": ok,
            "detail": "%.2f vs %.2f cm, %.1f%%" % (detected_h_cm, preset_h_cm, dev * 100.0)}


def cross_check_scale(px_per_cm, pitch_px, lane_pitch_cm, tol=TOL):
    """泳道间距（实测/估计，旋转画布 px）vs 当前定标推出的 px_per_cm × lane_pitch_cm。

    stage 1 先用 estimate_pitch_px() 做一次独立预估；T4 起 run_stages 在拿到 stage 2 的
    拟合 pitch 后**重跑本函数**，用它替换掉预估值（stage 1 的定标本身仍先冻结，不改）。
    只警告，不改定标。
    """
    from_pitch = px_per_cm * lane_pitch_cm
    dev = abs(pitch_px - from_pitch) / from_pitch if from_pitch else 0.0
    ok = dev <= tol
    if not ok:
        print("[!] scale: 泳道间距实测/估计 %.1f px，当前定标推出来 %.1f px"
              "（%.3f px/cm × %.3f cm，差 %.1f%%）—— 定标不改，先核泳道条数与裁切"
              % (pitch_px, from_pitch, px_per_cm, lane_pitch_cm, dev * 100.0))
    return {"name": "scale", "ok": ok,
            "detail": "%.1f vs %.1f px, %.1f%%" % (pitch_px, from_pitch, dev * 100.0)}


def _check_canvas(box, canvas):
    """胶面框可以超出旋转画布（调用方会 clamp）；超了就记一条 clipped 提示。"""
    W1, H1 = canvas
    x0, y0, x1, y1 = box
    out = {"l": max(0.0, -x0), "t": max(0.0, -y0), "r": max(0.0, x1 - W1), "b": max(0.0, y1 - H1)}
    clipped = any(v > 1e-6 for v in out.values())
    if clipped:
        print("[!] clipped: 胶面框有 %.1f px 落在旋转画布外（左 %.1f / 上 %.1f / 右 %.1f / 下 %.1f）"
              "—— 调用方会 clamp 到画布边" % (sum(out.values()), out["l"], out["t"], out["r"], out["b"]))
    detail = ("出画布：左 %.1f / 上 %.1f / 右 %.1f / 下 %.1f px（调用方 clamp）"
              % (out["l"], out["t"], out["r"], out["b"])) if clipped else "框在画布内"
    return {"name": "clipped", "ok": not clipped, "detail": detail}


# ---------------------------------------------------------------- 泳道间距的廉价预估

def _run_centres(prof, thr):
    """连续 >= thr 的 run → 加权中心（stage 1 不 import 泳道/条带模块，几行本地实现）。"""
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
        out.append(sum(i * v for i, v in r) / tot if tot else r[0][0])
    return out


def _merge_near(cs, d=3.0):
    """相邻中心 < d px 的 run 合回来（一条带被阈值切成几段）。"""
    out = []
    for c in cs:
        if out and c - out[-1] < d:
            out[-1] = (out[-1] + c) / 2.0
        else:
            out.append(c)
    return out


def estimate_pitch_px(info, angle=None, lane_mad_k=1.5, ignore_top=0.12):
    """stage 1 的**预估值**：胶框内宽松列剖面里 run 中心距的中位数（旋转画布 px）。

    只用来做尺度交叉核对，不参与定标；精度由 stage 2 的格点拟合复核（T3）。
    宽松阈值的取法与 stage 2 的列剖面一致（胶内中位数 − k·1.4826·MAD），
    这样「只有一条淡带」的泳道也能在列剖面上露头；上缘 ignore_top 那段（上样孔）不参与。

    `angle` 只为签名对称（旋转前后距离不变，所以用原图→分析的缩放换算即可）。
    """
    path = info.get("path")
    if not path:
        return 0.0
    w, h, rgb = read_ppm(path, max(info["analysis"]))
    gray = to_gray(w, h, rgb)
    x0, y0, x1, y1 = [int(round(v)) for v in info["bbox_analysis"]]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    top = y0 + int((y1 - y0) * ignore_top)
    vals = sorted(gray[y * w + x] for y in range(top, y1 + 1) for x in range(x0, x1 + 1))
    n = len(vals)
    if n < 20:
        return 0.0
    med = vals[n // 2]
    mad = sorted(abs(v - med) for v in vals)[n // 2]
    thr = med - max(3, lane_mad_k * 1.4826 * mad)
    cols = [0] * (x1 - x0 + 2)
    for y in range(top, y1 + 1):
        row = y * w
        for x in range(x0, x1 + 1):
            if gray[row + x] < thr:
                cols[x - x0] += 1
    cmax = max(cols) or 1
    cs = _merge_near(_run_centres(cols, max(2, int(0.05 * cmax))))
    if len(cs) < 2:
        return 0.0
    gaps = sorted(b - a for a, b in zip(cs, cs[1:]))
    s = info["image"][0] / float(info["analysis"][0])      # 分析尺度 → 原图/画布 px（旋转不改长度）
    return gaps[len(gaps) // 2] * s


# ---------------------------------------------------------------- stage 1

def compute(info, preset, angle, gel_lanes=None, pitch_px=None):
    """stage 1：探测结果 + 预设 → 冻结的胶面矩形。

    info   = detect_gel() 的返回（需要 path / image / analysis / rect_orig / bbox_analysis /
             override）；preset = load_gel_size() 的 dict（或 --gel-cm 覆盖后的副本）。
    angle  = 本次实际用的角度（magick -rotate 语义，正 = 顺时针）。
    gel_lanes = 照片是裁过的、只含 N 条泳道时给 N（可见宽 = N × lane_pitch_cm）。
    pitch_px  = 泳道间距（旋转画布 px）。T3 之后 render 不再从泳道算法拿它；
                不给就 estimate_pitch_px() 做一次廉价估计（只用于 scale 核对）；
                给 False = 不在这里核对（render/probe 改用 well_grid 量出的孔距核对，更准）。

    返回 dict：
      gel_box   : (x0, y0, x1, y1) 旋转后画布坐标（与 geometry.rotated_rect_bbox 同一套）。
                  **已经按预设定高、按 anchor 取边、trim 也扣掉了**，调用方不要再裁。
      angle     : 本次实际用的角度
      px_per_cm : 旋转画布 px / cm（= gel_box 宽 / 可见宽_cm）
      width_cm  : 这个 box 对应的物理宽（整块 = preset width_cm；裁过的 = N × lane_pitch_cm）
      height_cm : box 的物理高（= height_cm − trims）
      lane_pitch_cm : 预设的泳道中心距（stage 2 的先验间距用它，不再从条带量）
      canvas    : 旋转后的画布尺寸 (W1, H1)（stage 2 做逆变换时要用，免得重复外呼 magick）
      source    : {"px_per_cm": "width"|"lane_pitch", "height_cm": "preset", "box": "detect"|"rect"}
      checks    : [{"name", "ok", "detail"}] 交叉核对（height / scale / clipped）

    失败项在**这里**就打 `[!]`（spec §4.4：no silent correction beyond step 3）；调用方只汇总。
    """
    path = info.get("path")
    if not path:
        raise RuntimeError("gel_area.compute 需要 info['path']（detect_gel 的返回应带原图路径）")

    W0, H0 = info["image"]
    (_, _), (W1, H1) = load_dims_after(path, angle)
    box, _ = rotated_rect_bbox(info["rect_orig"], angle, (W0, H0), (W1, H1))
    box_w = box[2] - box[0]
    box_h = box[3] - box[1]

    # ---- 1) 定标：宽 → px_per_cm。裁过的照片按「可见宽」算，绝不拿整块 width_cm 去套。
    pitch_cm = float(preset.get("lane_pitch_cm") or 0.0)
    if gel_lanes:
        n = int(gel_lanes)
        if not pitch_cm:
            raise RuntimeError("裁过的照片要按泳道定标，但预设没有 lane_pitch_cm")
        width_cm = n * pitch_cm
        px_per_cm = box_w / width_cm
        src_scale = "lane_pitch"
    else:
        width_cm = float(preset["width_cm"])
        px_per_cm = box_w / width_cm
        src_scale = "width"

    # ---- 2) 定形：高 = (height_cm − trims) × px_per_cm，anchor 决定哪条边不动
    height_cm = float(preset["height_cm"])
    tt = float(preset.get("trim_top_cm") or 0.0)
    tb = float(preset.get("trim_bottom_cm") or 0.0)
    anchor = preset.get("anchor") or "bottom"
    face_h_cm = height_cm - tt - tb
    face_h_px = face_h_cm * px_per_cm
    x0, yd0, x1, yd1 = box
    if anchor == "top":
        y0 = yd0 + tt * px_per_cm
        y1 = y0 + face_h_px
    elif anchor == "center":
        cy = (yd0 + yd1) / 2.0
        y0 = cy - face_h_px / 2.0
        y1 = cy + face_h_px / 2.0
    else:                       # bottom（默认；config 已校验只有三种取值）
        y1 = yd1 - tb * px_per_cm
        y0 = y1 - face_h_px

    # ---- 3) 交叉核对：高度（探测框未定形前）、尺度（泳道间距）、出画布。只警告，不改图。
    checks = [cross_check_height(box_h / px_per_cm, height_cm)]
    if pitch_px is None:
        pitch_px = estimate_pitch_px(info, angle)
    elif pitch_px is False:
        pitch_px = None          # 调用方自己做 scale 核对（render/probe 用加样孔量出的孔距，见 gel_photo）
    if pitch_px and pitch_cm:
        checks.append(cross_check_scale(px_per_cm, float(pitch_px), pitch_cm))
    checks.append(_check_canvas((x0, y0, x1, y1), (W1, H1)))

    return {
        "gel_box": (x0, y0, x1, y1),
        "angle": angle,
        "px_per_cm": px_per_cm,
        "width_cm": width_cm,
        "height_cm": face_h_cm,
        "lane_pitch_cm": pitch_cm,
        "canvas": (W1, H1),
        "source": {"px_per_cm": src_scale, "height_cm": "preset",
                   "box": "rect" if info.get("override") else "detect"},
        "checks": checks,
    }
