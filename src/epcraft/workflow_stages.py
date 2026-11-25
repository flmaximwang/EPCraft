#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workflow_stages —— 分阶段画布流程的后三步（ep-gq1.2）。

    crop-to-workbench     读**活文档里改过的** Crop 裁切框 → 按新框裁出胶面图 → 建 Workbench
                          （整胶、积分窗、泳道线、trim、marker 带线、样品带刻度、泳道号、ep-meta）。
                          只碰 Workbench 画板与 gel/window/lanes/trimmed/analysis/sample bands/
                          marker bands/ep-meta 这些 Workbench 自己的图层；Figure / Profiles 不碰、
                          不导 PNG。
    workbench-to-profile  读活文档的 Workbench → 重新分析 → 只重建 Profiles 画板上的 profiles 图层
    workbench-to-figure   同上 → 只重建 Figure 画板上的胶段（gel 图层 gel seg）与标签（labels 图层）

派生两步共用 figure.plan_sync 的增量计划（保留用户的标签文字 / 字号 / 角度与走过的位置），但**只落地
属于目标画板的操作**：plan_sync 原来会顺手重建 analysis / sample bands（属于 Workbench）与给 Workbench
的线重新上色、改 profiles 图层。这里按 (画板名, 图层名) 过滤。plan_sync 把画板下标写死成 0 Figure /
2 Profiles；计划在一份**画板顺序规范化**的文档副本上做（用户拖动过画板顺序也对），落地前把 artboard
操作的下标换成活文档里按名字找到的下标。

两个派生命令都不导 PNG、不动磁盘以外的画布；--dry-run 只打印计划，不存盘、不写临时快照到源文件。
"""
import copy
import json
import os

from . import crop
from . import figure
from . import gel_session
from . import profiles
from . import sync_exec
from . import vc_control
from . import vc_file
from . import workbench
from . import workflow_start
from .geometry import load_dims_after
from .lane_select import parse_keep
from .text_layout import window_deg


# plan_sync 写死的画板下标 → 画板名（它只发这两个画板的 artboard 操作）
_PLAN_INDEX_NAMES = {0: "Figure", 2: "Profiles"}
_CANON_BOARDS = ("Figure", "Workbench", "Profiles", "Crop")

# 每个派生阶段能落地的对象归属
_STAGE_BOARDS = {"profile": {"Profiles"}, "figure": {"Figure"}}
_STAGE_LAYERS = {"profile": {"profiles"}, "figure": {"gel", "labels"}}

# Workbench 自己管的内容图层：crop-to-workbench 重新播种时清空它们（Figure 的胶段留在 gel 图层）
_WB_LAYERS = ("gel", "window", "lanes", "trimmed", "analysis", "sample bands", "marker bands")


# ---------------------------------------------------------------- 活文档 / 离屏读

def _dpi(args):
    dpi = int(getattr(args, "dpi", 300))
    if dpi <= 0:
        raise RuntimeError("--dpi 要正整数（收到 %r）" % dpi)
    return dpi


def _gel(cm, key):
    try:
        return float(cm[key])
    except (TypeError, KeyError, ValueError):
        raise RuntimeError("ep-meta 的 gel 缺 %s（先跑 img-to-crop）" % key)


def _live_snapshot(args, path):
    """--dry-run 用：读活文档但**不存盘**（连临时快照也不做，免得动活文档的路径 / dirty 状态）。"""
    port = int(getattr(args, "port", vc_control.DEFAULT_PORT))
    client, _launched = vc_control.connect(port, launch=True)
    gel_session.ensure_open(client, path)
    return client, client.document_json(), (vc_file.read(path)[1] if os.path.exists(path) else {})


def _read_live(args, path, dry_run):
    """→ (client, document, blobs)。非 dry-run 先把活文档里的未存改动存盘（保住用户的编辑）。"""
    if dry_run:
        return _live_snapshot(args, path)
    client, document, blobs = workflow_start.live_document(args, path)
    gel = vc_file.gel_node(document)
    if gel is not None and gel["kind"]["key"] not in blobs:
        raise RuntimeError("活文档里的胶面图不在磁盘那份里 —— 先在 VectorCraft 里存盘，再跑这一步")
    return client, document, blobs


# ---------------------------------------------------------------- crop-to-workbench

def cmd_crop_to_workbench(args):
    path = os.path.abspath(args.doc)
    if not os.path.isfile(path):
        raise RuntimeError("文档不存在：%s —— 先跑 `epcraft workflow init-canvas %s`" % (path, path))
    dpi = _dpi(args)
    if not getattr(args, "markers", None):
        raise RuntimeError("要 --markers <json>（单个配置，或预设库 + --preset）")

    # 先把活文档里改过的 Crop 框存盘，再离线读最新内容（之后的写入都基于它）
    client = workflow_start._prepare_write(args, path)
    builder = workflow_start.load_builder(path)
    document = builder["top"]["document"]
    workflow_start.require_init(document)
    meta = workflow_start.load_meta(document)

    photo = meta.get("photo")
    if not photo or not os.path.isfile(photo):
        raise RuntimeError("ep-meta 里的照片 %r 找不到 —— 重新跑 img-to-crop" % photo)
    want_sha = (meta.get("crop") or {}).get("photo_sha1")
    if want_sha and crop.photo_sha1(photo) != want_sha:
        raise RuntimeError("照片 %s 换过了（sha1 与 img-to-crop 时不同 —— Crop 框是相对旧照片画的）"
                           "—— 把旧照片放回去，或重新跑 img-to-crop" % photo)
    gel = meta.get("gel") or {}
    width_cm, height_cm = _gel(gel, "width_cm"), _gel(gel, "height_cm")
    lane_count = int(_gel(gel, "lane_count"))
    pitch = _gel(gel, "lane_pitch_cm")
    face_aspect = height_cm / width_cm

    frame, errs = crop.read_frame(document, meta, face_aspect)
    if errs:
        raise RuntimeError("Crop 画板有 %d 处要改（什么都没动）：\n- %s" % (len(errs), "\n- ".join(errs)))
    if frame is None:
        raise RuntimeError("读不到 Crop 裁切框 —— 先跑 `epcraft workflow img-to-crop`")

    size0 = (meta.get("crop") or {}).get("photo_size") or []
    if len(size0) != 2:
        raise RuntimeError("ep-meta 的 crop 缺 photo_size（不是本流程写的文档？）—— 重新跑 img-to-crop")
    ang, box = crop.stage1_from_frame(frame, size0, lambda a: load_dims_after(photo, a)[1], face_aspect)
    png = crop.crop_face(photo, ang, box, width_cm, height_cm, dpi)
    want_px = (int(round(width_cm / 2.54 * dpi)), int(round(height_cm / 2.54 * dpi)))
    if vc_file.png_size(png) != want_px:
        raise RuntimeError("裁出的胶面图 %dx%d px，期望 %dx%d（%d dpi）—— 检查 preset 宽高与 dpi"
                           % (vc_file.png_size(png) + want_px + (dpi,)))
    print("[i] 按 Crop 框重新裁切：转 %.3f°、%d×%d px（%d dpi）" % (ang, want_px[0], want_px[1], dpi))

    names, angles, mk_labels, cand, mk_note = gel_session.lanes_and_markers(args, lane_count)
    trim = parse_keep(args.trim_lanes, lane_count) if args.trim_lanes else []
    lane_angle, marker_angle = window_deg(args.label_angle), window_deg(args.marker_label_angle)
    if args.profiles not in ("stack", "strips"):
        raise RuntimeError("--profiles 只收 stack / strips（收到 %r）" % args.profiles)
    patch = {"stage": "crop-to-workbench", "profiles_layout": args.profiles,
             "label_angle": {"lane": lane_angle, "marker": marker_angle},
             "lane_angles": [a if a is not None else lane_angle for a in angles]}
    if not lane_angle and not marker_angle:
        patch.pop("label_angle")                         # 0 不写（与 render 一致）
    if not any(patch["lane_angles"]):
        patch.pop("lane_angles")
    meta.update(patch)

    model, sd = _seed_model(png, width_cm, height_cm, lane_count, pitch, names, mk_labels, cand, trim, meta)
    print("[i] 泳道种子：%s，%d 道，孔距 %.3f cm，首道中心 %.3f cm" % (
        {"wells": "按加样孔定相", "even": "看不清孔，预设间距居中"}.get(sd["source"], sd["source"]),
        lane_count, sd["pitch_cm"], sd["first_cm"]))
    conf = model["meta"]["marker_confidence"]
    print("[i] marker 泳道 %d：%d 条 band 线%s" % (
        model["marker_lane"], len(model["bands"]), "" if conf.get("ok") else "（低置信：%s）" % conf.get("gate")))
    for n in conf.get("notes") or []:
        print("[i] %s" % n)

    raw = {}
    report = gel_session.mark_low_confidence(
        model, profiles.analyze(model, png, names, overrides={}, raw=raw))
    model["meta"].update(gel_session.meta_state(model, raw))   # bands / peak_auto（下一次差分的基准）
    peaks = [(ln["lane"], b["y_cm"]) for ln in report["lanes"] if not ln["marker"] for b in ln["bands"]]

    key, w, h = vc_file.add_image(builder, png)
    _clear_workbench(builder, document)
    layers = {n: vc_file.layer_by_name(document, n) for n in
              ("gel", "window", "lanes", "trimmed", "analysis", "sample bands", "marker bands", "ep-meta")}
    original = _board(document, "Workbench")             # 保留原画板（id / 顺序不动）
    fr = workbench.add_workbench(builder, layers, model, key, w, h, x0=0.0, y0=0.0, peaks=peaks, tags=True)
    _drop_last_board(document, "Workbench")              # add_workbench 追加的那块同名画板去掉
    _set_rect(original, fr["artboard"])
    model["meta"]["dpi"] = dpi                        # 实际裁切用的 dpi（下次派生要一致）
    model["meta"].setdefault("crop", {})["frame_px"] = [list(p) for p in frame]   # 用户改过的框
    workflow_start.set_meta(builder, model["meta"])

    vc_file.write(builder, path)
    r = client.open(path)
    if r.get("warnings"):
        print("[!] VectorCraft 打开时提示：%s" % r["warnings"])
    print("Workbench 已按 Crop 框重建：%d 条泳道（%s），%d 条 band 线，%d 个样品带刻度" % (
        len(model["lanes"]), mk_note, len(model["bands"]), len(peaks)))
    print("下一步：epcraft workflow workbench-to-profile %s" % path)
    return 0


def _seed_model(png, width_cm, height_cm, lane_count, pitch, names, mk_labels, cand, trim, meta):
    """按 marker 候选道逐个建模型：第一条过闸门的赢；全不过 → 断崖比值最好的那条，标低置信。"""
    err, tried = None, []
    for ml in cand:
        try:
            m, sd = workbench.from_image(png, width_cm, height_cm, lane_count, pitch, names, mk_labels, ml,
                                         trim=trim, meta=dict(meta))
        except workbench.WorkbenchError as e:
            err = e
            if len(cand) > 1:
                print("[!] marker 泳道 %d 建不出模型：%s" % (ml, str(e).splitlines()[0]))
            continue
        tried.append((ml, m, sd))
        if m["meta"]["marker_confidence"]["ok"]:
            return m, sd
        if len(cand) > 1:
            print("[!] marker 泳道 %d 没过闸门（%s）：低置信"
                  % (ml, m["meta"]["marker_confidence"].get("gate")))
    if not tried:
        raise err if err else RuntimeError("建不出 Workbench 模型（没有 marker 候选泳道）")
    ml, model, sd = max(tried, key=lambda t: (gel_session.gate_ratio(t[1]["meta"]["marker_confidence"]),
                                              len(t[1]["bands"])))
    conf = model["meta"]["marker_confidence"]
    conf["ok"] = False
    conf.setdefault("notes", []).append(
        "没有一条 marker 泳道过闸门：选了断崖比值最好的泳道 %d —— 低置信，可人工核对" % ml)
    print("[!] 低置信：没有一条 marker 泳道过闸门 —— band 线放在剖面上最强的局部极小上，MW 请人工核对")
    return model, sd


def _clear_workbench(builder, document):
    """清空 Workbench 管的内容层（gel 图层保留 Figure 的胶段 gel seg；ep-meta 清空待重建）。"""
    for name in _WB_LAYERS:
        node = vc_file.layer_by_name(document, name)
        if node is None:
            continue
        kids = vc_file.lay_children(node)
        if name == "gel":
            keep = [n for n in kids if (n.get("name") or "").startswith("gel seg ")]
            kids[:] = keep
        else:
            kids.clear()
    lay = vc_file.layer_by_name(document, "ep-meta")
    if lay is not None:
        vc_file.lay_children(lay).clear()


def _board(document, name):
    """同名画板 dict（重播种时保留它的 id 与顺序）。"""
    for ab in document.get("artboards") or []:
        if ab.get("name") == name:
            return ab
    raise RuntimeError("文档里没有 %s 画板" % name)


def _drop_last_board(document, name):
    """去掉最后追加的一块同名画板（add_workbench 追加的那块），原画板保留。"""
    abs_ = document.get("artboards") or []
    for i in range(len(abs_) - 1, -1, -1):
        if abs_[i].get("name") == name:
            abs_.pop(i)
            return
    raise RuntimeError("文档里没有 %s 画板" % name)


def _set_rect(ab, rect):
    x, y, w, h = (float(v) for v in rect)
    ab["rect"] = {"x0": x, "y0": y, "x1": x + w, "y1": y + h}
    return ab


# ---------------------------------------------------------------- 派生阶段

def _common_model(args, path, dry_run):
    """读活文档 → 模型、报告、布局、client。两个派生阶段共用。"""
    client, document, blobs = _read_live(args, path, dry_run)
    workflow_start.require_init(document)
    gel = vc_file.gel_node(document)
    if gel is None:
        raise RuntimeError("Workbench 还没有胶面图 —— 先跑 `epcraft workflow crop-to-workbench`")
    key = gel["kind"]["key"]
    if key not in blobs:
        raise RuntimeError("磁盘上的文档里没有胶面图字节 —— 让 VectorCraft 存盘后再跑")
    png = blobs[key]
    model, notes = workbench.from_document(document)
    for n in notes:
        print("[i] %s" % n)
    meta = model["meta"]
    layout = meta.get("profiles_layout") or figure.PROFILE_LAYOUT
    if layout not in ("stack", "strips"):
        layout = figure.PROFILE_LAYOUT

    reset_lanes, reset_all, warns = gel_session.peak_reset_lanes(model, getattr(args, "reset_peaks", None))
    for n in warns:
        print("[!] %s" % n)
    overrides, warns = gel_session.peak_override_state(model, workbench.sample_ticks(document), reset_lanes)
    for n in warns:
        print("[!] %s" % n)
    if overrides:
        meta["peak_overrides"] = overrides
    else:
        meta.pop("peak_overrides", None)
    raw = {}
    report = gel_session.mark_low_confidence(
        model, profiles.analyze(model, png, [None] * len(model["lanes"]),
                                overrides=meta.get("peak_overrides"), raw=raw))
    meta.update(gel_session.meta_state(model, raw))
    return client, document, model, report, layout, reset_lanes, reset_all


def _canonical(document):
    """画板顺序规范化的副本：Figure / Workbench / Profiles / Crop 按这个顺序排前面，其余照原顺序。"""
    doc = copy.deepcopy(document)
    by = {}
    for ab in doc["artboards"]:
        by.setdefault(ab.get("name"), []).append(ab)
    head = [by[n].pop(0) for n in _CANON_BOARDS if by.get(n)]
    head_ids = {id(ab) for ab in head}
    rest = [ab for ab in doc["artboards"] if id(ab) not in head_ids]
    doc["artboards"] = head + rest
    return doc


def _stale_gel_segs(document, model):
    """Figure 里对不上当前 Workbench 胶面图的 `gel seg ` 剪切组 → [(id, 名字)]。

    plan_sync 只会移动已有胶段的图对象，**从不**换 key / 尺寸 / 比例；Crop 框改过并重播种后，Figure
    里的胶段可能还嵌着旧胶面图（旧像素、旧尺寸），导出永远是旧的。判据：内嵌图与 Workbench 那张 gel
    图的 key、像素尺寸、每像素 pt 全对上才算新鲜；对不上就整段删掉，让 plan_sync 按当前图重建。
    """
    gel = vc_file.gel_node(document)
    if gel is None:
        return []
    k = gel["kind"]
    out = []
    lay = vc_file.layer_by_name(document, "gel")
    for n in (vc_file.lay_children(lay) if lay else []):
        if not (n.get("name") or "").startswith("gel seg "):
            continue
        img = next((c for c in n["kind"].get("children") or [] if c["kind"].get("type") == "image"), None)
        if img is None:
            out.append((n["id"], n.get("name")))
            continue
        ik = img["kind"]
        same = (ik.get("key") == k.get("key") and ik.get("width") == k.get("width")
                and ik.get("height") == k.get("height")
                and abs(float(ik["xf"][0]) - float(k["xf"][0])) <= 1e-6 * max(1.0, abs(float(k["xf"][0]))))
        if not same:
            out.append((n["id"], n.get("name")))
    return out


def _plan(args, document, model, report, layout, rebuild_stale_gel=False):
    """→ (ops, meta)：plan_sync 的计划，artboard 下标已换成活文档里的名字下标。

    rebuild_stale_gel=True（Figure）：先在副本里删掉嵌着旧胶面图的 `gel seg `，并把对应的 delete
    操作放在计划最前面 —— plan_sync 看不到它们，会用当前 Workbench 的图重建；活文档里那几段也因此
    真的被清掉（不会留下新旧两套）。
    """
    base = _canonical(document)
    deletes = []
    if rebuild_stale_gel:
        stale = _stale_gel_segs(document, model)
        if stale:
            lay = vc_file.layer_by_name(base, "gel")
            kids = vc_file.lay_children(lay)
            kids[:] = [n for n in kids if n["id"] not in {i for i, _nm in stale}]
            deletes = [{"op": "delete", "id": i} for i, _nm in stale]
            print("[i] Figure 里有 %d 个胶段嵌着旧的胶面图（%s）—— 按当前 Workbench 重建"
                  % (len(stale), "、".join(nm for _i, nm in stale)))
    planned, meta = figure.plan_sync(base, model, report, layout=layout)
    live = {ab.get("name"): i for i, ab in enumerate(document["artboards"])}
    out = list(deletes)
    for op in planned:
        if op["op"] == "artboard":
            name = _PLAN_INDEX_NAMES.get(op["index"])
            if name is None or name not in live:
                continue
            out.append(dict(op, index=live[name]))
        else:
            out.append(op)
    return out, meta


def _layer_of(document):
    """对象 id → 所在顶层图层名。"""
    out = {}
    for lay in document.get("layers") or []:
        for _l, n in vc_file.walk({"layers": [lay]}):
            out[n["id"]] = lay.get("name")
    return out


def _stage_filter(ops, document, stage):
    """只留目标画板 / 目标图层的操作（别的画布原样不动）。"""
    where = _layer_of(document)
    boards, layers = _STAGE_BOARDS[stage], _STAGE_LAYERS[stage]
    keep = []
    for op in ops:
        kind = op["op"]
        if kind == "artboard":
            if op.get("index") in {i for i, ab in enumerate(document["artboards"])
                                   if ab.get("name") in boards}:
                keep.append(op)
        elif kind == "create":
            if op.get("layer") in layers:
                keep.append(op)
        elif kind in ("delete", "reshape", "move", "rename", "stroke"):
            if where.get(op.get("id")) in layers:
                keep.append(op)
        elif kind == "set_text":
            keep.append(op)                              # 只会是 ep-meta（plan_sync 只改那一处）
        # layer / relink / backfill 不属于单个画板：丢掉
    return keep


def _run_stage(args, stage, title):
    dry_run = bool(getattr(args, "dry_run", False))
    _dpi(args)                                           # 早报非法 dpi（派生阶段本身不裁切）
    path = os.path.abspath(args.doc)
    if not os.path.isfile(path):
        raise RuntimeError("文档不存在：%s" % path)
    client, document, model, report, layout, reset_lanes, reset_all = _common_model(args, path, dry_run)
    ops, meta = _plan(args, document, model, report, layout, rebuild_stale_gel=(stage == "figure"))
    ops = _stage_filter(ops, document, stage)
    if reset_lanes or reset_all:
        print("[i] --reset-peaks：清掉%s的样品带 override" % (
            "全部道" if reset_all else "道 %s " % "、".join(str(n) for n in sorted(reset_lanes))))
    if dry_run:
        print(json.dumps(ops, ensure_ascii=False, indent=1))
        print("[i] --dry-run：%s计划 %d 个操作（文档没动）" % (title, len(ops)))
        return 0
    if ops:
        done = sync_exec.apply_ops_live(client, ops, document)
        print("[i] %s：%s（一个撤销步「epcraft sync」，⌘Z 可撤回）" % (title, sync_exec.describe(done)))
    else:
        print("[i] %s 已经是最新的（0 个改动）" % title)
    client.save(path)
    print("已存盘 %s（没导 PNG）" % path)
    return 0


def cmd_workbench_to_profile(args):
    return _run_stage(args, "profile", "Profiles")


def cmd_workbench_to_figure(args):
    return _run_stage(args, "figure", "Figure")
