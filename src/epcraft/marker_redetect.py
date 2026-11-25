#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`sync --reset-marker`：按当前 Crop 框与当前泳道几何，在 marker 道重叫峰、重建 band <MW> 线（票 ep-ugj）。

为什么需要它：ladder 的检测只在 render 时做一次（profiles.pick_marker → band <MW> 线）。之后的 sync
只把现有线在 ±SNAP_CM 内吸到局部极小上。用户挪了 Crop 框、或在 Workbench 里调了泳道之后，整排线的 y
就一起偏了；6 条都吸不上时只能手工搬（crop probe 打 y → workbench export → 改 bands[].y_cm → import）。
实测 IMG_0108（10% Tris/Tricine、WJ103 10–250 kD）：重裁后原线落在 1.08–5.61 cm、6 条吸不上；按当前框
重检出 11 条（泳道 1 过闸门，断崖 2.30×）搬回去，主带 MW 从 21/20.9/21/22.9 变成 16.7/16.2/16.7/17.1 kD ——
差异足以改变结论，这一步不该靠手搬。

这里的取舍（界面上给 lead / 验收 agent 的口径）：

- 候选道只有 ep-meta.marker_lane 一条（没给 --lane 时）。render 的候选道来自 lanes.json 的
  marker_lanes，那份配置不进 ep-meta；拿现有泳道去猜会把梯子悄悄换到别的道上。`--lane N` = 第 N 条，
  无条件用它，并把它写成新的 marker 道（用户点名要的那条）。
- 闸门（pick_marker 的候选数 / 断崖 / 下限）只决定置信度，不决定成败：**全不过闸门也照常重建 band 线**，
  按「断崖比值降序 → 落线条数降序 → 道号升序」挑候选（本次候选只有一条时就是它自己），标低置信。
  排序写在这里，_lane_scores 的 docstring 是它的唯一出处 —— 以后 --lane 收一串道号也能直接用。
- 检出的带（y 升序）照「出生即 MW」挂 MW 槽（workbench.bands_from_rows）：第 k 条带 = 第 k 个槽。
  render / sync / 重检三处共用这一份配对语义，没有第二处。
- marker 道的剖面从 profiles.sample_lane 走（t27 的斜泳道采样，传整条 lane dict）：泳道几何一变
  （L(y)/R(y) 逐行积分、四个角点），重检自动跟着走，这里不用改。没有那个函数时退回只认 left/right 的
  profiles.lane_profile —— 矩形泳道两种口径逐像素一致。
- 改的是文档里的 band 线本身（marker bands 图层的线），不是 figure.plan_sync 的一部分：plan_sync 从不管
  这个图层（线归用户 / Workbench）。同 MW 的线改 y（reshape）、多的删、少的建；MW 一样 y 也一样就不动
  （幂等：重检结果与现有线一致时 0 个改动 → 整次 sync 也就 0 个操作）。
"""
from . import profiles
from . import vc_file
from . import workbench


TOL_CM = 0.005          # y 变动小于它就算「没动」（与 gel_photo._summary 同一口径）
TOL_PT = 0.01           # 线的两端 x / y 差小于它就不发 reshape（与 doc_edit 同一口径）
NOTE_WINDOW = "重检用当前积分窗 %.2f–%.2f cm（window 图层）"
_SAMPLE = getattr(profiles, "sample_lane", None)     # t27 的斜泳道采样（有就用，见模块 docstring）


def lane_of(model, lane):
    """要重检的泳道号 + 它的来源 → (道号, "--lane" 或 "ep-meta.marker_lane")。"""
    n = len(model["lanes"])
    if lane is None:
        want, source = int(model["marker_lane"]), "ep-meta.marker_lane"
    else:
        want, source = int(lane), "--lane"
    if not 1 <= want <= n:
        raise RuntimeError("--reset-marker --lane %d 超出 1–%d（现在只有 %d 条泳道）" % (want, n, n))
    return want, source


def marker_mws(model):
    """ep-meta 记的 MW 槽序列（marker_labels[].mws 拼起来），自上而下。

    不能拿 marker_labels[].text 重新展开：并带（`{"text": "21/20 kD", "rows": 2}`）的 rows 不写进
    ep-meta，重展开会把它当成一个槽、把 20 kD 丢掉。mws 是 render 当时展开好的结果，也是 Figure
    标签用的那一份（figure.targets）。没有 marker_labels 的旧文档退回现有 band 线的 MW。
    """
    mws = []
    for e in model["meta"].get("marker_labels") or []:
        mws.extend(float(m) for m in (e.get("mws") or []))
    if not mws:
        mws = [float(b["mw"]) for b in model["bands"]]
    return mws


def profile(model, png, lane):
    """marker 泳道的灰度剖面 → (ys_cm, gray, (w, h, px_per_cm), 提示行)。

    t27 的 profiles.sample_lane 在就用它（传整条 lane dict，斜泳道逐行积分）；不在就退回矩形口径。
    """
    w, h, gray = profiles.gel_gray(png)
    ppc = w / float(model["gel"]["width_cm"])
    if abs(h / ppc - model["gel"]["height_cm"]) > max(0.02, 2.0 / ppc):
        raise RuntimeError("胶面图 %dx%d px 与胶面 %.3f×%.3f cm 不成比例"
                           % (w, h, model["gel"]["width_cm"], model["gel"]["height_cm"]))
    top = max(0.0, model["window"]["top_cm"])
    bottom = min(model["gel"]["height_cm"], model["window"]["bottom_cm"])
    ln = model["lanes"][int(lane) - 1]
    if _SAMPLE is not None:
        ys, gs = _SAMPLE(gray, w, h, ppc, ln, top, bottom, model["gel"]["height_cm"])
    else:
        ys, gs = profiles.lane_profile(gray, w, h, ppc, ln["left_cm"], ln["right_cm"], top, bottom)
    return ys, gs, (w, h, ppc), NOTE_WINDOW % (top, bottom)


def _lane_scores(model, png, lane, need):
    """一条候选道上重叫一次峰 → {lane, rows, info, ratio, picked, note}。

    排序口径（候选道多于一条时用，见 _rank）：断崖比值降序 → 落线条数降序 → 道号升序。
    「落线条数」= pick_marker 返回的行数（它不过闸门也照样给前 need 名），所以一条道叫不出带时
    不会因为 ratio = None 被排到最前。
    """
    ys, gs, (w, h, ppc), note = profile(model, png, lane)
    rows, info = profiles.pick_marker(ys, gs, ppc, need)
    return {"lane": int(lane), "rows": [r["y_cm"] for r in rows], "info": info,
            "ratio": info.get("ratio"), "picked": len(rows), "note": note}


def _rank(scores):
    """候选道排序：断崖比值降序 → 落线条数降序 → 道号升序（只此一处，_lane_scores 里写了理由）。"""
    return sorted(scores, key=lambda s: (-(float(s["ratio"]) if s["ratio"] is not None else 0.0),
                                         -int(s["picked"]), int(s["lane"])))


def candidates(model, lane):
    """候选道号：指定了 --lane 就只有它；否则只有 ep-meta.marker_lane（见模块 docstring）。"""
    return [int(lane)]


def plan(model, png, lane=None, document=None):
    """重检计划（不碰文档；给了 document 才顺手算出 band 线操作）：{lane, source, bands, old, ops, ...}。

    model    = Workbench 模型（文档读回的那份；window / lanes / marker_lane / marker_labels 都要现行值）；
    png      = 当前 Crop 框裁出来的胶面图（重新裁过的话就是新的那张）；
    lane     = --lane N；None = ep-meta.marker_lane；
    document = vc_file 的 top（算 band 线的 reshape / create / delete 用它的原点）；None = 不算 ops。
    """
    want, source = lane_of(model, lane)
    mws = marker_mws(model)
    if not mws:
        raise RuntimeError("ep-meta 里没有 marker 标签的 MW 槽（marker_labels 空、也没有 band 线）—— 重新 render")
    need = len(mws)
    scores = _lane_scores_many(model, png, want, need)
    best = _rank(scores)[0]
    bands, notes, conf = workbench.bands_from_rows(best["rows"], mws, best["lane"], "重检出", best["info"])
    old = [{"mw": float(b["mw"]), "y_cm": float(b["y_cm"])} for b in model["bands"]]
    ops = None
    if document is not None:
        ops = band_ops(document, old, bands, model["lanes"][best["lane"] - 1],
                       height_cm=model["gel"]["height_cm"])
    return {"lane": best["lane"], "source": source, "bands": bands, "old": old, "ops": ops,
            "gate": conf.get("gate"), "confidence": conf, "notes": notes,
            "scores": scores,
            # 候选道多于一条、且挑中的不是点名的那条 → 才另说一句（单条候选时闸门那句已经说清了）
            "fallback": best["lane"] != want,
            "window_note": best["note"]}


def _lane_scores_many(model, png, want, need):
    """候选道逐条重叫峰（现在只有一条；--lane 将来收一串道号时这里就是循环）。"""
    return [_lane_scores(model, png, n, need) for n in candidates(model, want)]


def changes(old, new, tol=TOL_CM):
    """旧 band 线 → 新的一批，逐条给 (状态, MW, 旧 y, 新 y)；状态 = "same" / "moved" / "added" / "gone"。

    "moved" 的口径 = _moved（严格大于 tol）：与 band_ops 判「要不要 reshape」用的是同一个函数，
    所以打印说「不变」的带不会产生操作（幂等的前提）。
    """
    o = {float(b["mw"]): float(b["y_cm"]) for b in old}
    n = {float(b["mw"]): float(b["y_cm"]) for b in new}
    out = []
    for mw in sorted(set(o) | set(n)):
        if mw not in n:
            out.append(("gone", mw, o[mw], None))
        elif mw not in o:
            out.append(("added", mw, None, n[mw]))
        elif _moved(o[mw], n[mw], tol):
            out.append(("moved", mw, o[mw], n[mw]))
        else:
            out.append(("same", mw, o[mw], n[mw]))
    return out


def _moved(old_y, new_y, tol=TOL_CM):
    """y 变了多少才算「要动线」：检测出来的 y 是像素中心（1/ppc 的量化），文档里往返只差浮点噪声；
    比 tol 大一丁点就要改线，否则「打印说变了、操作数为 0」或者反过来。"""
    return abs(float(new_y) - float(old_y)) > tol


def band_ops(document, old, new, lane, tol=TOL_CM, height_cm=None):
    """marker bands 图层：把 old 那批线摆成 new（同 MW 改 y、多的删、少的建）。→ ops

    old / new = [{mw, y_cm}]。线的名字就是 MW（`band <MW>`），所以同 MW 的线只改几何（reshape），
    不重建对象 —— 撤销组的步数、对象的 id 都留得住。x 两端与 y 都由 workbench.band_spec 给，
    与 render 画出的线逐点一致（否则读回时 y 会差一点点，下次 sync 又要动）。
    「要不要 reshape」与 changes() 的 "moved" 同一口径（_moved，cm），泳道横向变了另算（TOL_PT）。
    """
    gx, gy = vc_file.doc_origin(document)
    lay = vc_file.layer_by_name(document, "marker bands")
    have = {}
    for n in (vc_file.lay_children(lay) if lay else []):
        mw = workbench.band_mw(n.get("name"))
        if mw is not None and mw not in have:
            have[mw] = n
    ops, want = [], {float(b["mw"]): float(b["y_cm"]) for b in new}
    for mw in sorted(want):
        sp = workbench.band_spec({"mw": mw, "y_cm": want[mw]}, lane, gx, gy, height_cm=height_cm)
        cur = have.pop(mw, None)
        if cur is None:
            ops.append({"op": "create", "layer": "marker bands", "spec": sp})
            continue
        (ax, ay), (bx, by) = vc_file.line_ends(cur)
        y_cm = ((ay + by) / 2.0 - gy) / vc_file.PT_PER_CM
        if _moved(y_cm, want[mw], tol) or max(abs(ax - sp["a"][0]), abs(bx - sp["b"][0])) > TOL_PT:
            ops.append({"op": "reshape", "id": cur["id"], "points": [list(sp["a"]), list(sp["b"])],
                        "closed": False})
    ops += [{"op": "delete", "id": n["id"]} for mw, n in sorted(have.items())]
    return ops


def _best_score(rk):
    """挑中那条道的评分（gate_line / fallback 行都用它；候选只有一条时就是唯一那条）。"""
    for s in rk["scores"]:
        if int(s["lane"]) == int(rk["lane"]):
            return s
    return (rk["scores"] or [{}])[0]


def format_plan(rk):
    """重检计划 → 给用户看的几行（[i] / [!] 前缀由这里带，调用方只 print）。"""
    src = "（%s）" % rk["source"]
    out = ["[i] marker 重检：泳道 %d%s" % (rk["lane"], src)]
    if rk["fallback"]:
        out.append("[!] 没有候选道过闸门：按断崖比值选泳道 %d（断崖 %s 倍）—— 低置信，照常执行"
                   % (rk["lane"], _fmt(_best_score(rk).get("ratio"))))
    for st, mw, oy, ny in changes(rk["old"], rk["bands"]):
        if st == "same":
            out.append("[i] band %s：y %.4f cm（不变）" % (_mw(mw), ny))
        elif st == "moved":
            out.append("[i] band %s：y %.4f → %.4f cm（Δ %+.4f）" % (_mw(mw), oy, ny, ny - oy))
        elif st == "added":
            out.append("[i] band %s：新建（y %.4f cm）" % (_mw(mw), ny))
        else:
            out.append("[i] band %s：删除（旧 y %.4f cm）" % (_mw(mw), oy))
    out.append(gate_line(rk))
    if not rk["confidence"].get("ok"):
        out.append("[!] marker 低置信：%s" % (rk["confidence"].get("gate") or "闸门不过"))
    return out


def gate_line(rk):
    """闸门诊断一行：picked/need、断崖倍数、第 need 名与第 need+1 名显著度（缺哪个写 —）。"""
    info = _best_score(rk).get("info") or {}
    need = int(rk["confidence"].get("need") or 0)
    got = int(rk["confidence"].get("picked") or 0)
    parts = ["picked %d/%d" % (got, need)]
    if info.get("ratio") is not None:
        parts.append("断崖 %.2f 倍" % float(info["ratio"]))
    elif info.get("next") is None:
        parts.append("断崖 —（没有第 %d 名）" % (need + 1))
    else:
        parts.append("断崖 —")
    parts.append("第 %d 名显著度 %s" % (need, _fmt(info.get("nth"))))
    if info.get("next") is not None:
        parts.append("第 %d 名 %s" % (need + 1, _fmt(info.get("next"))))
    body = "，".join(parts)
    if rk["confidence"].get("ok"):
        return "[i] 闸门 通过：%s" % body
    return "[!] 闸门不过：%s（%s）" % (rk["gate"] or "闸门不过", body)


def _mw(mw):
    return "%g" % float(mw)


def _fmt(v):
    return "—" if v is None else "%.2f" % float(v)
