#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gel_session —— 凝胶管线的会话与域状态（渲染 / 分阶段流程 / sync 共用），不依赖 CLI。

这里放的是「一次胶体会话」的东西，不属于任何画板模块：
    live session        VectorCraft 里开着哪份文档（doc_state / ensure_open）
    stage 1 与预设      参数 → 预设、认胶面 + 定形（gel_face）、裁切画板规格、泳道名与 marker 候选
    peak override 状态  样品带刻度差分（ep-meta 的 peak_auto / peak_overrides 口径，spec §7.5）
                      以及低置信标记、报告提示

gel_photo（CLI 与编排）从这里取，canvas_cli / workflow_start / workflow_stages / workbench_io 也从这里取；
本模块**不** import gel_photo，所以 CLI 层之间不再需要延迟 import 才能拿到这些工具函数。

与 profiles.analyze 的分工：analyze 只算，报告语义（低置信怎么标注、peak_auto 怎么记）在这里定，
这样 render / sync / 分阶段流程写的 ep-meta 是同一份口径。
"""
import argparse
import os

from . import crop
from . import profiles
from . import workbench
from .config import as_entry, load_cfg, load_gel_size, load_markers, parse_gel_cm
from .gel_area import compute
from .gel_detect import detect_gel
from .text_layout import window_deg


def rect_arg(s):
    """--rect 的解析：`x0,y0,x1,y1`（原图像素）→ 四元组；空 → None。"""
    if not s:
        return None
    v = [float(t) for t in s.replace(",", " ").split()]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("--rect 要 4 个数：x0,y0,x1,y1（原图像素）")
    return tuple(v)


def add_detect_args(a):
    """stage 1 的参数（probe / render / img-to-crop 共用；sync 补 Crop 画板时也拿它的默认值）。"""
    a.add_argument("--gel-size", help="胶体预设 JSON（必须给；如 data/gel_size.json）")
    a.add_argument("--gel-cm", help="覆盖预设里的宽x高（cm），如 8.6x6.25；不能单独使用")
    a.add_argument("--gel-lanes", type=int, default=None,
                   help="照片是裁过的、只含 N 条泳道时给 N：改按 lane_pitch_cm 的泳道间距定标")
    a.add_argument("--trim-top-cm", type=float, default=None, help="从胶面上缘再裁掉多少 cm（默认读预设）")
    a.add_argument("--trim-bottom-cm", type=float, default=None, help="从胶面下缘再裁掉多少 cm（默认读预设）")
    a.add_argument("--angle", type=float, default=None, help="强制用这个角度（度，正=顺时针）")
    a.add_argument("--no-rotate", action="store_true")
    a.add_argument("--longest", type=int, default=700, help="分析缩略图长边（默认 700）")
    a.add_argument("--cue", choices=("dark", "chroma", "union"), default="dark",
                   help="认胶的判据：dark=比背景暗（通用）/ chroma=蓝度（偏蓝光下的染色胶）/ union")
    a.add_argument("--ink-thr", type=float, default=None, help="dark 判据的阈值（默认 max(6, 背景//20)）")
    a.add_argument("--rect", help="跳过自动找胶，直接给原图像素矩形 x0,y0,x1,y1")


def default_detect_args():
    """stage 1 的默认参数（与 add_detect_args 同一份默认值）：旧文档的 ep-meta 里没记 render 参数。"""
    ap = argparse.ArgumentParser(add_help=False)
    add_detect_args(ap)
    return ap.parse_args([])


def preset_from_args(args):
    """T1：预设是强制先验。没有 --gel-size 时在检测之前就退出；--gel-cm 只覆盖预设里的宽高。"""
    if not args.gel_size:
        raise RuntimeError("gel preset required (--gel-size FILE)")
    gel = load_gel_size(args.gel_size)
    if getattr(args, "gel_cm", None):
        gel = parse_gel_cm(args.gel_cm, gel)
    preset = dict(gel)
    for key in ("trim_top_cm", "trim_bottom_cm"):
        v = getattr(args, key, None)
        if v is not None:
            preset[key] = v
    return preset


def _print_stage1(ga, preset, gel_lanes=None):
    """stage 1 的一行汇报（定标 / 定形）+ 交叉核对一行。失败项 compute 已经打过 [!]。"""
    tt = float(preset.get("trim_top_cm", 0.0) or 0.0)
    tb = float(preset.get("trim_bottom_cm", 0.0) or 0.0)
    if ga["source"]["px_per_cm"] == "lane_pitch":
        cal = ("%d 条 × %.3f cm = %.2f cm（裁过的照片；整块预设 %.2f cm 不适用）"
               % (int(gel_lanes), float(preset["lane_pitch_cm"]), ga["width_cm"], float(preset["width_cm"])))
    else:
        cal = "整块宽 %.2f cm" % ga["width_cm"]
    print("[i] 胶面（stage 1）：定标 = %s → %.2f px/cm；高 %.2f cm = 预设 %.2f − trim 上/下 %.2f/%.2f，"
          "anchor=%s（框来自 %s）" % (cal, ga["px_per_cm"], ga["height_cm"], float(preset["height_cm"]),
                                      tt, tb, preset.get("anchor", "bottom"), ga["source"]["box"]))
    checks = [c for c in ga["checks"] if c["name"] != "scale"]   # scale 核对要老 stage 2 的间距，已不再有
    if checks:
        print("[i] checks: %s" % " · ".join("%s %s (%s)" % (c["name"], "ok" if c["ok"] else "WARN", c["detail"])
                                            for c in checks))


def gel_face(image, preset, args, dpi):
    """stage 1：认胶面 → 按预设定形 → 正畸裁切成「胶面图」PNG 字节（dpi 像素/英寸，宽 = width_cm）。

    返回 (png 字节, ga, stage1)。这是老管线里唯一还留着的一级；泳道与条带都从这张图上来（V8）。
    stage1 = {"angle", "gel_box", "size0", "size1"}：Crop 画板要的量（crop.frame_from_stage1 的参数），
    size0 = 自动定向后的原图 (W0, H0)，size1 = 旋转画布 (W1, H1)。裁切本身是 crop.crop_face。
    """
    info = detect_gel(image, args.longest, args.cue, rect_arg(args.rect), args.ink_thr)
    ang = args.angle if args.angle is not None else round(info["angle_vertical"], 3)
    if args.no_rotate:
        ang = 0.0
    ga = compute(info, preset, ang, gel_lanes=args.gel_lanes, pitch_px=False)   # scale 核对改由孔距做
    print("角度: 竖边拟合 %.3f°；本次用 %.3f°" % (info["angle_vertical"], ang))
    _print_stage1(ga, preset, args.gel_lanes)
    png = crop.crop_face(image, ang, ga["gel_box"], ga["width_cm"], ga["height_cm"], dpi, warn=False)
    stage1 = {"angle": ang, "gel_box": tuple(ga["gel_box"]), "size0": tuple(info["image"]),
              "size1": tuple(ga["canvas"])}
    return png, ga, stage1


def check_pitch(sd, preset, tol=0.10):
    """定标核对：加样孔量出的孔距 vs 预设 lane_pitch_cm。孔距是在已定标的胶面上量的，差得多 =
    宽度定标或预设有一个不对。只警告，不改定标。"""
    if sd["source"] != "wells":
        return
    dev = abs(sd["pitch_cm"] - float(preset["lane_pitch_cm"])) / float(preset["lane_pitch_cm"])
    if dev > tol:
        print("[!] scale: 加样孔孔距 %.3f cm，预设 lane_pitch_cm %.3f cm（差 %.0f%%）—— 核对预设的 width_cm / "
              "lane_pitch_cm，或照片是不是裁过（--gel-lanes）" % (sd["pitch_cm"], float(preset["lane_pitch_cm"]), dev * 100))


def lanes_and_markers(args, lane_count):
    """两个配置 → (泳道名[lane_count], 每条泳道的标签角度或 None, marker 标签, marker 泳道候选[], 说明)。"""
    if not args.markers:
        raise RuntimeError("要 --markers <json>（单个配置，或 data/markers.json 这种预设库 + --preset）")
    mk, mk_labels, mk_note = load_markers(args.markers, args.preset)
    names, angles = [""] * lane_count, [None] * lane_count
    cand = []
    if args.lanes:
        cfg, labels = load_cfg(args.lanes, "泳道配置")
        if len(labels) > lane_count:
            raise RuntimeError("泳道配置有 %d 个名字，胶上只有 %d 条泳道" % (len(labels), lane_count))
        for i, e in enumerate(labels):                   # 每项：字符串，或 {"text", "angle"}
            ent = as_entry(e, {})
            names[i] = str(ent.get("text", ""))
            if ent.get("angle") is not None:
                angles[i] = window_deg(ent["angle"])
        cand = [int(v) for v in (cfg.get("marker_lanes") or [])]
    if mk.get("marker_lane") is not None and int(mk["marker_lane"]) not in cand:
        cand.append(int(mk["marker_lane"]))
    cand = [c for c in cand if 1 <= c <= lane_count] or [1]
    return names, angles, mk_labels, cand, mk_note


def crop_artboard(args, preset, stage1):
    """Crop 画板要的东西（crop-artboard spec §3）→ (build_document 的 crop 规格, ep-meta 的 crop 键)。
    frame_px = crop_face 裁到的那块（原图 px）。"""
    frame_px = crop.frame_from_stage1(stage1["gel_box"], stage1["angle"], stage1["size0"], stage1["size1"])
    jpeg, psize, _k = crop.preview(args.image)
    rect = rect_arg(args.rect)
    meta = {"photo_size": list(stage1["size0"]), "photo_sha1": crop.photo_sha1(args.image),
            "frame_px": [[x, y] for x, y in frame_px],     # 不取整：取整后的四角不再是严格矩形（vc_file.rect 会拒）
            "stage1": {"cue": args.cue, "rect": list(rect) if rect else None, "angle": args.angle,
                       "no_rotate": bool(args.no_rotate), "longest": args.longest, "ink_thr": args.ink_thr,
                       "gel_lanes": args.gel_lanes,
                       "trim_top_cm": float(preset.get("trim_top_cm", 0.0) or 0.0),
                       "trim_bottom_cm": float(preset.get("trim_bottom_cm", 0.0) or 0.0)}}
    spec = {"jpeg": jpeg, "preview_size": psize, "photo_size": tuple(stage1["size0"]),
            "frame_px": meta["frame_px"]}
    return spec, meta


# ---------------------------------------------------------------- 活文档

def doc_state(client, path):
    """VectorCraft 里是否开着**这份**文件：→ (下标或 None, 是否有未存改动)。

    文档列表只给文件名：别的文件夹里同名的 fig.vectorcraft 也叫这个名字。所以名字对上的逐个激活，
    用 document.inspect 的 path 核对完整路径（会把命中的那份切到前台）。
    """
    base, want = os.path.basename(path), os.path.realpath(path)
    for i, d in enumerate(client.call("ui.inspect").get("documents") or []):
        if d.get("title") != base:
            continue
        client.execute("document.activate", {"index": i})
        got = client.inspect(0).get("path")
        if got and os.path.realpath(got) == want:
            return i, bool(d.get("dirty"))
    return None, False


def ensure_open(client, path):
    """让 VectorCraft 打开并激活 path；返回文档下标（打不开 → RuntimeError）。"""
    idx, _dirty = doc_state(client, path)
    if idx is None:
        client.open(path)
        idx, _dirty = doc_state(client, path)
    if idx is None:
        raise RuntimeError("VectorCraft 没能打开 %s" % path)
    client.execute("document.activate", {"index": idx})
    return idx


# ---------------------------------------------------------------- 标定置信度与报告

LOW_CONFIDENCE_NOTE = "标定置信度低（marker 泳道的带位置没通过闸门，MW 请人工核对）"


def mark_low_confidence(model, report):
    """marker_confidence → 报告：ok = false 标 calibration.low_confidence，两种情况都把具体提示带进 notes。

    低置信时通用注放第一条；ok 时只并 notes、不加警告。具体提示（多带 / 缺带 / 疑似劈峰）都进
    report["notes"]（去重）—— fig.mw.json 的 notes 是用户唯一能看的地方，说清为什么不够，也记住
    「多出的带没落线」这类事实。返回 report。"""
    conf = model["meta"].get("marker_confidence") or {}
    notes = [n for n in (conf.get("notes") or []) if n]
    low = not conf.get("ok", True)
    if low:
        report["calibration"]["low_confidence"] = True
        if conf.get("gate"):
            notes.append(conf["gate"])
        report["notes"] = list(dict.fromkeys([LOW_CONFIDENCE_NOTE] + notes + list(report["notes"])))
    elif notes:
        report["notes"] = list(dict.fromkeys(notes + list(report["notes"])))
    return report


def gate_ratio(conf):
    """置信信息里断崖比值的数值（没有比值 = 0，排最后）。render 全不过闸门时拿它挑道。"""
    try:
        return float(conf.get("ratio") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def meta_state(model, raw):
    """写进 ep-meta 的「上一次」状态：sync 拿它说清楚这次改了什么（R² 不再有：标定不做全局拟合）。
    另记这一轮检测器叫出的样品带 `peak_auto`（**override 之前**，analyze 填的 raw）：下一次 sync 拿文档里的
    刻度跟它差分（spec §4.6、§7.5）—— 抑制掉的带也要留着，否则 sync 会把「用户删的」当成「检测器没叫」。"""
    return {"bands": [[b["mw"], b["y_cm"]] for b in model["bands"]],
            "peak_auto": peak_auto(raw, model["marker_lane"])}


def peak_auto(raw, marker_lane):
    """检测器叫出的样品带（**override 之前**）→ {"泳道号": [y_cm, …]}，cm 四位小数、自上而下。

    raw = profiles.analyze 填的 {泳道号: [y_cm…]}（抑制之前）。marker 道不画刻度（band <MW> 线已管它），
    也不记；没有带的道不列（缺键 = 空列表）。手加带不在 raw 里，所以不会混进 peak_auto。"""
    out = {}
    for lane, ys in raw.items():
        if marker_lane is not None and int(lane) == int(marker_lane):
            continue
        ys = sorted(round(float(y), 4) for y in ys)
        if ys:
            out[str(lane)] = ys
    return out


def ask_names(model):
    """打印还没有名字（标签是 "?"）的泳道号，提示 agent 去问用户。"""
    missing = [i for i, n in enumerate(model["meta"].get("lane_names") or [], 1)
               if n is None and i <= len(model["lanes"]) and not model["lanes"][i - 1]["trimmed"]]
    if missing:
        print("[?] 泳道 %s 还没有名字（标签是 \"?\"）—— 问用户，再改那几个标签的文字" % missing)


TICK_TOL_CM = profiles.MIN_SEP_CM       # 刻度和期望带配对的容差（0.05 cm：两条真带不会这么近，spec §7.5）


def norm_overrides(raw):
    """ep-meta 的 peak_overrides → {泳道号: {"suppress": [y…], "add": [y…]}}。

    泳道号转 int；y 四位小数、升序、去重；空列表和空道丢掉（缺键 = 全自动）。读坏的值忽略 ——
    ep-meta 是 agent 写的，用户手改坏了也不该让 sync 崩。"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            lane = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        ent = {}
        for key in ("suppress", "add"):
            vals, src = set(), v.get(key)
            for y in (src if isinstance(src, list) else []):
                try:
                    vals.add(round(float(y), 4))
                except (TypeError, ValueError):
                    continue
            if vals:
                ent[key] = sorted(vals)
        if ent:
            out[lane] = ent
    return out


def peak_reset_lanes(model, reset):
    """--reset-peaks 的道号 → (这一轮不差分的道号集合, 是否「全部道」, 警告行)。

    reset = None（没给）/ []（--reset-peaks 不带道号 = 全部道）/ [道号…]。
    不存在的道号警告并忽略（说清现在几条泳道）；重复的道号去重。"""
    if reset is None:
        return set(), False, []
    n = len(model["lanes"])
    if not reset:
        return set(range(1, n + 1)), True, []
    lanes, warns = set(), []
    for lane in sorted(set(reset)):
        if 1 <= lane <= n:
            lanes.add(lane)
        else:
            warns.append("--reset-peaks：泳道 %d 不存在（现在只有 %d 条泳道）—— 忽略" % (lane, n))
    return lanes, False, warns


def peak_override_state(model, ticks, reset_lanes=()):
    """文档里的样品带刻度 + ep-meta 的上一轮 peak_auto → (新的 peak_overrides, 警告行)。

    spec §7.5：刻度按中点 x 归道（名字不参与），marker 道 / 道外警告并忽略（原线由 plan_sync 保留）。
    每道 expected = peak_auto − 已有 suppress + 已有 add，与刻度按最近距离贪心配对（容差 MIN_SEP_CM）；
    期望有、刻度没有 → 自动带进 suppress / 手加带从 add 去掉；刻度没有期望 → 进 add
    （拖动 = 旧 y 进 suppress + 新 y 进 add）。上一轮的 suppress 全部保留 —— 包括对不上这一轮自动带的
    （检测器挪了位置时不能让用户删掉的带自己回来；失配的在 _summary 里说明）。
    文档还没有 peak_auto（本功能之前 render 的）→ 不差分、原样保留：刻度是检测器自己写的，反推不出意图。
    道数减少时超出新道数的 override 丢掉（道序号按 match_lanes 一一对应，见 §4.4）。
    reset_lanes = 这一轮 --reset-peaks 清掉的道：不拿文档里的刻度差分（那批线正是产生这些 override 的，
    差分会把刚清掉的立刻写回来），结果里也没有它们（= 回到纯自动）。
    """
    n = len(model["lanes"])
    kept = {lane: ent for lane, ent in norm_overrides(model["meta"].get("peak_overrides")).items()
            if 1 <= lane <= n and lane not in reset_lanes}
    warns, by_lane = [], {}
    for x, y, _n in ticks:
        lane = workbench.lane_at(model, x)
        if lane is None:
            warns.append("样品带刻度 y=%.2f cm 的中点不在任何泳道里 —— 忽略（原样保留）" % y)
        elif lane == model["marker_lane"]:
            warns.append("样品带刻度 y=%.2f cm 落在 marker 泳道 %d 里 —— 忽略（标定用 band <MW> 线，原样保留）"
                         % (y, lane))
        else:
            by_lane.setdefault(lane, []).append(float(y))
    auto = model["meta"].get("peak_auto")
    if not isinstance(auto, dict):
        return {str(lane): ent for lane, ent in kept.items()}, warns   # 没有差分基准（旧文档第一次 sync）
    out = {}
    for lane in range(1, n + 1):
        if lane in reset_lanes:
            continue                                   # --reset-peaks：这一轮直接回到纯自动
        vals = auto.get(str(lane))
        autos = [float(y) for y in vals if isinstance(y, (int, float))] if isinstance(vals, list) else []
        old = kept.get(lane, {})
        sup = list(old.get("suppress") or [])
        expected = [(y, "auto") for y in autos
                    if not any(abs(y - s) <= TICK_TOL_CM + 1e-9 for s in sup)]
        expected += [(y, "add") for y in old.get("add") or []]
        tick_ys = by_lane.get(lane, [])
        pairs = sorted((abs(t - e), ti, ei) for ti, t in enumerate(tick_ys)
                       for ei, (e, _k) in enumerate(expected) if abs(t - e) <= TICK_TOL_CM + 1e-9)
        used_t, used_e = set(), set()
        for _d, ti, ei in pairs:                   # 最近邻、贪心，从小到大；超过容差的不成对
            if ti not in used_t and ei not in used_e:
                used_t.add(ti)
                used_e.add(ei)
        new_sup = sup + [y for ei, (y, kind) in enumerate(expected) if kind == "auto" and ei not in used_e]
        new_add = [y for ei, (y, kind) in enumerate(expected) if kind == "add" and ei in used_e]
        new_add += [y for ti, y in enumerate(tick_ys) if ti not in used_t]
        ent = {}
        if new_sup:
            ent["suppress"] = sorted({round(y, 4) for y in new_sup})
        if new_add:
            ent["add"] = sorted({round(y, 4) for y in new_add})
        if ent:
            out[str(lane)] = ent                       # 键是泳道号字符串（spec §4.6）
    return out, warns
