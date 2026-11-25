#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gel_photo.py —— 凝胶电泳照片 → 可在 VectorCraft 里改的 .vectorcraft + 它导出的 .png + MW 报告

子命令
    render  <图> --out fig.vectorcraft [配置...]   认胶面 → 正畸裁切 → 按加样孔放泳道 → 叫带/标定 →
                                                  写 fig.vectorcraft，用 VectorCraft 导出 fig.png，写 fig.mw.json
    sync    fig.vectorcraft                       读用户在 VectorCraft 里改过的 Workbench（活文档）→ 重算 →
                                                  原地更新标签/曲线/Figure → 存盘 → 重新导出 fig.png / fig.mw.json
    probe   <图> [--gel-size ...] [--markers M]   只探测，打印胶面、泳道种子、每道叫出的带、
                                                  marker 候选道的前 N 名与闸门；不写文件
    doctor                                        环境自检（python / magick / 字体 / VectorCraft）
    selftest                                      不读用户文件、不开 app 的自检

依赖：系统 python3（纯 stdlib）+ ImageMagick 7（`magick`，读照片、正畸、裁切）+ VectorCraft 0.14
（`--control` 控制口；fig.png 由它导出，所以 png 永远就是用户在 app 里看到的那张）。
本文件只放 CLI 与编排；实现分在同目录模块：
    magick_io    magick 外呼 / PPM        gel_detect   认胶面          gel_area     stage 1 胶面区域
    geometry     旋转与位置口径           config       配置读取        text_layout  字体与文字量宽
    lane_select  泳道号语法、保留段       mw_report    MW 数学与报告    env_check    doctor
    vc_file      .vectorcraft 读写         vc_control   VectorCraft 控制口
    gel_session  会话与域状态：stage 1、泳道/刻度与活文档（不依赖本文件）
    workbench    Workbench 模型（建、画、读回、校验、泳道名匹配）
    profiles     泳道剖面、条带、标定、MW 报告 v2
    well_grid    泳道种子：按加样孔定相的等距梳子
    figure       Figure / Profiles 画板、整份文档、sync 增量计划（plan_sync）
    sync_exec    把 plan_sync 的操作落到活文档上（控制口）
    selftest     自检
本文件不带任何数据路径：预设与胶体尺寸由调用方用 --markers / --gel-size 显式给出。

坐标约定
    - 照片：「自动定向（-auto-orient）后」的原图像素；分析用缩略图（默认长边 700）找胶，几何量按比例回到原图。
    - 角度是 magick 的 `-rotate` 语义（正数 = 顺时针），与 geometry.rotate_pt() 同一套矩阵。
    - stage 1 之后一律是胶面 cm（原点 = 胶面左上角，y 向下）；文档里是 pt（1 cm = 72/2.54 pt）。
"""
import argparse
import json
import os
import shutil
import sys


from . import crop                                                                            # noqa: E402
from . import figure                                                                          # noqa: E402
from . import gel_session                                                                     # noqa: E402
from . import profiles                                                                        # noqa: E402
from . import vc_control                                                                      # noqa: E402
from . import vc_file                                                                         # noqa: E402
from . import workbench                                                                       # noqa: E402
from . import marker_redetect                                                                 # noqa: E402
from .env_check import cmd_doctor                                                       # noqa: E402
from .lane_select import parse_keep                                                     # noqa: E402
from .mw_report import format_report_v2, write_v2                                       # noqa: E402
from .text_layout import window_deg                                                     # noqa: E402

SKILL_VERSION = "vectorcraft-1"

# 已经去掉的参数 → 用什么代替（V6；给了就报这一句，不静默忽略）
REMOVED = {
    "--keep-lanes": "--trim-lanes（反过来写：要从 Figure 拿掉的泳道号），或在 VectorCraft 的 trimmed 图层里给那条泳道画一条短线（trim 标记）",
    "--svg": "fig.vectorcraft 本身就是矢量文档；要 SVG 就在 VectorCraft 里 File → Export",
    "--no-editor": "不再写 HTML 编辑器；编辑在 VectorCraft 里做",
    "--debug": "检测结果都在 fig.vectorcraft 的 Workbench 画板上（lanes / window / marker bands / sample bands / analysis 图层）",
    "--record": "探测记录就是 Workbench 上的线（加 ep-meta）；数值在 fig.mw.json 里",
    "--pair-force": "带与 MW 现在按 y 顺序直接对应（第 k 条带 = 第 k 个 MW），不再有「强制配对」；要改就在 Workbench 上改 band <MW> 线，然后 sync",
    "--marker-policy": "marker 泳道被 trim 时只用来标定、不画标签（固定行为）",
    "--margin-frac": "页边由 Figure 的页面规则自动推导",
    "--outline": "文字样式在 VectorCraft 里改",
    "--no-outline": "文字样式在 VectorCraft 里改",
    "--font": "字体在 VectorCraft 里改（默认 Arial）",
    "--mw-method": "MW 一律在相邻两条 marker 带之间做两点插值，没有可选方法（禁止全局拟合）",
    "--splice-gap-cm": "拼缝固定 0.15 cm",
}
_FLAG_ONLY = ("--svg", "--no-editor", "--pair-force", "--no-outline")


# ---------------------------------------------------------------- render

def cmd_render(args):
    _reject_removed(args)
    preset = gel_session.preset_from_args(args)
    dpi = args.dpi
    out = os.path.abspath(args.out if args.out.endswith(".vectorcraft")
                          else os.path.splitext(args.out)[0] + ".vectorcraft")
    stem = os.path.splitext(out)[0]
    lane_count = int(args.gel_lanes or preset["lane_count"])
    names, angles, mk_labels, cand, mk_note = gel_session.lanes_and_markers(args, lane_count)
    print("[i] 胶体预设 %s：%d 条泳道，孔距先验 %.3f cm；输出 %d dpi；marker 配置：%s" % (
        preset.get("name", "—"), lane_count, float(preset["lane_pitch_cm"]), dpi, mk_note))
    trim = parse_keep(args.trim_lanes, lane_count) if args.trim_lanes else []

    png, ga, stage1 = gel_session.gel_face(args.image, preset, args, dpi)
    crop_spec, crop_meta = gel_session.crop_artboard(args, preset, stage1)
    lane_angle, marker_angle = window_deg(args.label_angle), window_deg(args.marker_label_angle)
    meta = {"skill": "ep-craft", "skill_version": SKILL_VERSION, "photo": os.path.abspath(args.image),
            "gel_preset": preset.get("name", ""), "marker_preset": mk_note, "dpi": dpi,
            "profiles_layout": args.profiles,
            "label_angle": {"lane": lane_angle, "marker": marker_angle},
            "lane_angles": [a if a is not None else lane_angle for a in angles]}
    if not lane_angle and not marker_angle:
        meta.pop("label_angle")                          # 0 不写：默认角的文档与旧版逐字节一致
    if not any(meta["lane_angles"]):
        meta.pop("lane_angles")
    model, err, sd = None, None, None
    tried = []                           # [(泳道, 模型, 种子)]：闸门没过但照样建出模型的那些候选
    build = lambda ml: workbench.from_image(   # noqa: E731
        png, ga["width_cm"], ga["height_cm"], lane_count, float(preset["lane_pitch_cm"]), names, mk_labels, ml,
        trim=trim, meta=meta)
    for ml in cand:                      # 写了几条 marker 泳道就挨个试：第一条过闸门的赢（ep-bdt）
        try:
            m, s = build(ml)
        except workbench.WorkbenchError as e:       # 只剩结构性问题（模型不合法）会走到这里
            err = e
            if len(cand) > 1:
                print("[!] marker 泳道 %d 建不出模型：%s" % (ml, str(e).splitlines()[0]))
            continue
        conf = m["meta"]["marker_confidence"]
        tried.append((ml, m, s))
        if conf["ok"]:
            model, sd = m, s
            break
        if len(cand) > 1:
            print("[!] marker 泳道 %d 没过闸门（%s）：低置信" % (ml, conf["gate"]))
    if model is None and tried:
        # 全不过闸门 → 选断崖比值最好的那条道，整体标低置信（不凑数、不抛错、不提示拖线）
        ml, model, sd = max(tried, key=lambda t: (gel_session.gate_ratio(t[1]["meta"]["marker_confidence"]),
                                                  len(t[1]["bands"])))
        conf = model["meta"]["marker_confidence"]
        conf["ok"] = False
        conf["notes"].append("没有一条 marker 泳道过闸门：选了断崖比值最好的泳道 %d —— 低置信，可人工核对" % ml)
        print("[!] 低置信：没有一条 marker 泳道过闸门（泳道 %d：%s）。band 线照样放在剖面上最强的局部极小上，"
              "MW 可人工核对" % (ml, conf.get("gate") or "带数不够"))
    if model is None:
        raise err
    print("[i] 泳道种子：%s，%d 道，孔距 %.3f cm，首道中心 %.3f cm（孔深 %.0f 灰度级）%s" % (
        {"wells": "按加样孔定相", "even": "看不清孔，预设间距居中"}[sd["source"]], lane_count, sd["pitch_cm"],
        sd["first_cm"], sd["depth"], ("；" + sd["note"]) if sd["note"] else ""))
    conf = model["meta"]["marker_confidence"]
    how = "剖面最强的 %d 个局部极小%s%s" % (
        len(model["bands"]),
        "，第 %d 名 / 第 %d 名 = %.2f×" % (conf["need"], conf["need"] + 1, conf["ratio"])
        if conf.get("ratio") else "",
        "" if conf["ok"] else "（低置信：%s）" % (conf.get("gate") or "条数不足"))
    for n in conf["notes"]:
        print("[i] %s" % n)
    print("[i] marker 泳道 %d：%d 条 band 线（%s）" % (model["marker_lane"], len(model["bands"]), how))
    gel_session.check_pitch(sd, preset)

    raw = {}                                               # 检测器原始叫带（override 之前）→ peak_auto
    report = gel_session.mark_low_confidence(model, profiles.analyze(model, png, names, overrides={}, raw=raw))
    model["meta"].update(gel_session.meta_state(model, raw))
    model["meta"]["crop"] = crop_meta                   # ep-meta 与 plan_sync 写回的同一份（第一次 sync = 0 个操作）
    doc = figure.build_document(model, report, png, os.path.basename(stem), layout=args.profiles, crop=crop_spec)

    client, _launched = vc_control.connect(args.port, launch=True)
    idx, dirty = gel_session.doc_state(client, out)
    if idx is not None and dirty:
        raise RuntimeError("VectorCraft 里开着 %s 而且有没存的改动 —— 先在 app 里存盘（或另给 --out），"
                           "免得覆盖你的修改" % os.path.basename(out))
    if idx is not None:
        client.execute("document.activate", {"index": idx})
        client.execute("file.close", {})
    if os.path.exists(out):
        shutil.copyfile(out, out + ".bak")
        print("[i] 旧文件备份为 %s" % (out + ".bak"))
    vc_file.write(doc, out)
    r = client.open(out)
    if r.get("warnings"):
        print("[!] VectorCraft 打开时提示：%s" % r["warnings"])
    w, h, d = client.export_png(stem + ".png", 0, dpi)
    if not args.no_mw:
        write_v2(report, stem + ".mw.json")
        print(format_report_v2(report))
    print("输出 %s、%s（%d×%d px, %d dpi）%s" % (out, stem + ".png", w, h, round(d),
                                              "" if args.no_mw else "、%s.mw.json" % stem))
    print("[i] VectorCraft 控制口 %d 上开着这份文档：在 Workbench 画板上改泳道线 / 积分窗 / band 线，"
          "然后跑 `python3 -m epcraft workbench sync %s`" % (args.port, out))
    gel_session.ask_names(model)
    return 0


# ---------------------------------------------------------------- sync

def cmd_sync(args):
    from . import sync_exec
    path = os.path.abspath(args.doc)
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s（先 render）" % path)
    if getattr(args, "lane", None) is not None and not getattr(args, "reset_marker", False):
        raise RuntimeError("--lane 只配 --reset-marker 用（用哪条道重检 marker 梯子）；单给 --lane 没有意义")
    stem = os.path.splitext(path)[0]
    client, _launched = vc_control.connect(args.port, path=path, launch=True)
    idx, _dirty = gel_session.doc_state(client, path)
    if idx is None:
        client.open(path)
        idx, _dirty = gel_session.doc_state(client, path)
    client.execute("document.activate", {"index": idx})
    dj = client.document_json()
    _top, blobs = vc_file.read(path)                     # 胶面图从磁盘上那份读（gel 层锁定，图不会变）
    gel = vc_file.gel_node(dj)
    if gel is not None and gel["kind"]["key"] not in blobs:
        # 活文档的胶面图不在磁盘那份里（如撤销了上次的重新裁切）。--dry-run 不写盘，这里没法自己存一份，
        # 于是明确报错，而不是偷偷存盘之后再说「只打印不写」。
        if getattr(args, "dry_run", False):
            raise RuntimeError("活文档里的胶面图还没存进 %s —— --dry-run 不写盘；先在 VectorCraft 里存一下"
                               "（⌘S），或去掉 --dry-run 让 sync 存" % os.path.basename(path))
        _top, blobs = client.save(path)
    model, notes, cs = _read_all(dj, blobs, int(_doc_meta(dj).get("dpi") or args.dpi))
    for n in notes:
        print("[i] %s" % n)
    old = dict(model["meta"])
    for legacy in ("r2",):                      # 旧文档的 ep-meta 里有全局拟合的 R²：顺手清掉
        model["meta"].pop(legacy, None)
    if cs["crop"] is not None:
        model["meta"]["crop"] = cs["crop"]               # plan_sync 把整份 meta（含 crop）写回 ep-meta
    png = cs["png"]
    layout = old.get("profiles_layout") or figure.PROFILE_LAYOUT
    # 低置信标记**不会**因为拖过 band 线就自动清掉（ep-bdt 契约：拖线不认证标定）。
    # 用户认为标定没问题，就自己把 band 线摆好并在 ep-meta 里把 marker_confidence.ok 改成 true；
    # sync 只保证这一键存在且是 dict，读写的仍是文档里的那份。
    conf = model["meta"].get("marker_confidence")
    if not isinstance(conf, dict):
        model["meta"]["marker_confidence"] = {}
    # ---- marker 重检（--reset-marker，票 ep-ugj）：按当前 Crop 框 + 当前泳道几何在 marker 道重叫峰，
    #      直接换掉 band <MW> 线。必须在 analyze 之前：报告里的标定点 / 低置信标记都要用新线；
    #      meta_state 也必须在它之后（ep-meta.bands 记的是新 y，否则下一次 sync 又要动）。
    #      model 的 marker_lane 与 meta 的 marker_lane 是两份：peak_auto（meta_state）读的是前者。
    rk = None
    if getattr(args, "reset_marker", False):
        rk = marker_redetect.plan(model, cs["png"], lane=getattr(args, "lane", None), document=dj)
        for line in marker_redetect.format_plan(rk):
            print(line)
        model["bands"] = rk["bands"]
        model["marker_lane"] = rk["lane"]
        model["meta"]["marker_lane"] = rk["lane"]
        model["meta"]["marker_confidence"] = rk["confidence"]
    # ---- 样品带：文档里的刻度 vs 上一轮 peak_auto → 逐道 override（spec §7.5）。必须在 analyze 之前：
    #      这一轮的报告和重建的刻度都要用新 override；_meta_state 随后写的 peak_auto 仍是抑制之前的检测器叫带。
    #      --reset-peaks：被清的道这一轮不差分（文档里那批刻度正是产生这些 override 的旧线，差分会把
    #      刚清掉的立刻写回来），直接按纯自动重叫带；别的道照常差分。
    reset_lanes, reset_all, reset_warns = gel_session.peak_reset_lanes(model, args.reset_peaks)
    overrides, peak_warns = gel_session.peak_override_state(model, workbench.sample_ticks(dj), reset_lanes)
    for w in reset_warns + peak_warns:
        print("[!] %s" % w)
    if overrides:
        model["meta"]["peak_overrides"] = overrides
    else:
        model["meta"].pop("peak_overrides", None)        # 空的不写：缺键 = 全自动（与 render 一致）
    raw = {}                                               # 检测器原始叫带（override 之前）→ peak_auto
    report = gel_session.mark_low_confidence(model, profiles.analyze(model, png, [None] * len(model["lanes"]),
                                                overrides=model["meta"].get("peak_overrides"), raw=raw))
    model["meta"].update(gel_session.meta_state(model, raw))
    ops, meta = figure.plan_sync(dj, model, report, layout=layout)
    if rk is not None:
        # band 线的操作由重检自己出（plan_sync 从不管 marker bands 图层）。relink 之后、plan_sync 的
        # 操作之前：同一个撤销组里先摆线，再重建标签 / Profiles —— 标签用的是新 y。
        ops = rk["ops"] + ops
    ops = [op for op in cs["ops"] if op["op"] == "relink"] + ops + \
          [op for op in cs["ops"] if op["op"] != "relink"]          # relink 最先（sync_exec 也会保证）
    names = meta["lane_names"]
    report = dict(report, lanes=[dict(ln, label=names[ln["lane"] - 1]) for ln in report["lanes"]],
                  notes=[n for n in report["notes"] if not n.endswith("还没有名字")]
                  + ["泳道 %d 还没有名字" % ln["lane"] for ln in report["lanes"]
                     if names[ln["lane"] - 1] is None and not ln["trimmed"]])
    for line in _summary(old, model, report, cs["lines"], reset_lanes, reset_all, skip_bands=rk is not None):
        print("[i] %s" % line)
    if rk is not None and not rk["ops"]:
        print("[i] marker 重检：与现有 band 线一致（0 个改动）")
    if args.dry_run:
        print(json.dumps(_printable(ops), ensure_ascii=False, indent=1))
        return 0
    if ops:
        done = sync_exec.apply_ops_live(client, ops, dj)
        print("[i] 已更新：%s（一个撤销步「epcraft sync」，⌘Z 可整体撤回）" % sync_exec.describe(done))
    else:
        print("[i] 文档已经是最新的（0 个改动）")
    client.save(path)
    dpi = int(meta.get("dpi") or args.dpi)
    w, h, d = client.export_png(stem + ".png", 0, dpi)
    write_v2(report, stem + ".mw.json")
    print(format_report_v2(report))
    print("输出 %s（已存盘）、%s（%d×%d px, %d dpi）、%s.mw.json" % (path, stem + ".png", w, h, round(d), stem))
    model["meta"] = meta
    gel_session.ask_names(model)
    return 0


# ---------------------------------------------------------------- edit

def cmd_edit(args):
    """改哪些泳道进 Figure / 泳道名 / 用哪条泳道当 marker：一个撤销步「epcraft edit」，然后照常 sync。"""
    from . import doc_edit
    from . import sync_exec
    if args.keep and args.trim is not None:
        raise RuntimeError("--keep 与 --trim 只给一个")
    path = os.path.abspath(args.doc)
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s（先 render）" % path)
    client, _launched = vc_control.connect(args.port, path=path, launch=True)
    idx, _dirty = gel_session.doc_state(client, path)
    if idx is None:
        client.open(path)
        idx, _dirty = gel_session.doc_state(client, path)
    client.execute("document.activate", {"index": idx})
    dj = client.document_json()
    model, _notes = workbench.from_document(dj)
    n = len(model["lanes"])
    trim = None
    if args.keep:
        keep = set(parse_keep(args.keep, n))
        trim = [i for i in range(1, n + 1) if i not in keep]
    elif args.trim is not None:
        trim = parse_keep(args.trim, n) if args.trim.strip() else []
    names = {}
    if args.shift_labels:
        cur = doc_edit.current_names(dj, model)
        names.update({j: t for j, t in enumerate(doc_edit.shifted(cur, args.shift_labels), 1)})
    if args.labels is not None:
        lab = [t.strip() for t in args.labels.split(",")]
        if len(lab) > n:
            raise RuntimeError("--labels 有 %d 个名字，Workbench 上只有 %d 条泳道" % (len(lab), n))
        names.update({j: t for j, t in enumerate(lab, 1)})
    for item in args.label or []:
        k, sep, t = item.partition("=")
        if not sep or not k.strip().isdigit():
            raise RuntimeError("--label 要写成 泳道号=名字，如 --label 9=M（收到 %r）" % item)
        names[int(k)] = t
    ops, lines = doc_edit.plan_edit(dj, model, trim=trim, names=names, marker_lane=args.marker_lane)
    for line in lines:
        print("[i] %s" % line)
    if ops:
        done = sync_exec.apply_ops_live(client, ops, dj, label="epcraft edit")
        print("[i] 已改：%s（一个撤销步「epcraft edit」）" % sync_exec.describe(done))
    else:
        print("[i] 文档里已经是这样了（0 个改动）")
    if args.no_sync:
        return 0
    return cmd_sync(argparse.Namespace(doc=args.doc, port=args.port, dpi=args.dpi, dry_run=False,
                                       reset_peaks=None))


def _doc_meta(document):
    """ep-meta 文字对象的 JSON（读不出 → {}；from_document 会为此另记提示）。"""
    try:
        return vc_file.doc_meta(document)[1] or {}
    except RuntimeError:
        return {}


def _wb_items(err):
    """WorkbenchError → 条目列表（「Workbench 有 N 处要改」那种拆成条；别的整条算一项）。"""
    msg = str(err)
    head, sep, rest = msg.partition("：\n- ")
    if sep and head.startswith("Workbench 有 "):
        return rest.split("\n- ")
    return [msg]


def _read_all(dj, blobs, dpi):
    """活文档 → (模型, 提示, 裁切结果)：Workbench 与 Crop 画板一起读，错误一次全列出、什么都不动。

    裁切结果 = _crop_sync 的返回，另加 "png"：要分析的胶面图（重新裁过的，或文档里嵌着的那张）。"""
    gel = vc_file.gel_node(dj)
    try:
        model, notes = workbench.from_document(dj)
        errs = []
    except workbench.WorkbenchError as e:
        if gel is None:
            raise                                        # 连胶面图都没有：Crop 画板无从读起
        model, notes, errs = None, [], _wb_items(e)
    meta = model["meta"] if model else _doc_meta(dj)
    w, h = gel["kind"]["width"], gel["kind"]["height"]
    frame, cerrs = crop.read_frame(dj, meta, h / float(w))
    errs += ["Crop：%s" % e for e in cerrs]
    old_fp = (meta.get("crop") or {}).get("frame_px")
    delta = crop.frame_delta(old_fp, frame, w * 2.54 / dpi) if frame is not None and old_fp else None
    if frame is not None and not old_fp:
        errs.append("Crop：ep-meta 里没有 crop.frame_px（不是本 skill 写的 Crop 画板？）—— 重新 render")
    if delta is not None:
        errs += ["Crop：%s" % e for e in _photo_errors(meta)]
    if errs:
        raise workbench.WorkbenchError("Workbench 有 %d 处要改（什么都没动）：\n- %s" % (len(errs), "\n- ".join(errs)))
    png = blobs[gel["kind"]["key"]]
    cs = _crop_sync(dj, meta, frame, delta, gel, png, dpi)
    return model, notes + cs["notes"], dict(cs, png=cs["png"] or png)


def _photo_errors(meta):
    """重新裁切要读原图：ep-meta.photo 在、sha1 与 render 时一致 → []；否则一条错（不带 Crop： 前缀）。"""
    photo = meta.get("photo")
    if not photo or not os.path.isfile(photo):
        return ["照片 %s 找不到 —— 把原照片放回去，或重新 render" % photo]
    if crop.photo_sha1(photo) != (meta.get("crop") or {}).get("photo_sha1"):
        return ["照片 %s 换过了（sha1 与 render 时不同）—— 把原照片放回去，或重新 render" % photo]
    return []


def _gel_image_ids(document, gel):
    """用胶面图那个 images key 的所有图片 id：Workbench 的 gel 在前，再是 Figure 每个胶段里的。"""
    key = gel["kind"]["key"]
    lay = vc_file.layer_by_name(document, "gel")
    return [gel["id"]] + [n["id"] for _l, n in vc_file.walk({"layers": [lay]})
                          if n["kind"].get("type") == "image" and n["kind"].get("key") == key
                          and n["id"] != gel["id"]]


def _crop_sync(document, meta, frame, delta, gel, png, dpi):
    """Crop 画板这一步（crop-artboard spec §6、§7）→ {"png", "ops", "crop", "lines", "notes"}。

    - 框动过（delta 不是 None；照片已由 _photo_errors 核过）：按新框从原图重新裁 → png = 新胶面图
      （像素尺寸不变），ops = [relink]，crop = 新的 ep-meta.crop。
    - 框没动 → 全空（幂等）。
    - 没有 Crop 画板（frame 是 None 且没报错 = 旧文档）：照片还在且按默认参数重跑 stage 1 能裁出
      文档里那张胶面图 → ops = [backfill]、crop = 补写的 ep-meta.crop；否则一条提示，照旧 sync。"""
    out = {"png": None, "ops": [], "crop": None, "lines": [], "notes": []}
    wpx, hpx = gel["kind"]["width"], gel["kind"]["height"]
    width_cm, height_cm = wpx * 2.54 / dpi, hpx * 2.54 / dpi     # 正好回到原来的像素尺寸
    photo = meta.get("photo")
    old = meta.get("crop") or {}
    if frame is not None:
        if delta is None:
            return out
        from .geometry import load_dims_after
        ang, box = crop.stage1_from_frame(frame, old["photo_size"], lambda a: load_dims_after(photo, a)[1],
                                          hpx / float(wpx))
        new = crop.crop_face(photo, ang, box, width_cm, height_cm, dpi)
        if vc_file.png_size(new) != (wpx, hpx):
            raise RuntimeError("重新裁出的胶面图 %dx%d px，文档里的是 %dx%d —— 不该发生；重新 render"
                               % (vc_file.png_size(new) + (wpx, hpx)))
        out["png"] = new
        out["ops"] = [{"op": "relink", "ids": _gel_image_ids(document, gel), "png": new}]
        out["crop"] = dict(old, frame_px=[list(p) for p in frame])   # 不取整：下次读回要严格相等
        mm = lambda v: ("%+.2f" % (v if abs(v) >= 0.005 else 0.0)).replace("-", "−")   # noqa: E731（不出 −0.00）
        out["lines"] = ["裁切框：转 %s°，移 %s/%s cm，宽 ×%.3f（重新裁了胶面图）"
                        % (mm(delta["angle_deg"]), mm(delta["dx_cm"]), mm(delta["dy_cm"]), delta["scale"])]
        return out

    # ---- 旧文档：补上 Crop 画板
    if not photo or not os.path.isfile(photo):
        out["notes"].append("旧文档没有 Crop 画板（照片 %s 不在了）；要调裁切就重新 render" % photo)
        return out
    args = gel_session.default_detect_args()
    preset = {"name": meta.get("gel_preset", ""), "width_cm": round(width_cm, 2), "height_cm": round(height_cm, 2),
              "trim_top_cm": 0.0, "trim_bottom_cm": 0.0, "anchor": "bottom",
              "lane_pitch_cm": 0.0, "lane_count": 0}      # 宽高 = 胶面 cm（预设一般只到 0.01 cm）；孔距这里用不上
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _png, _ga, st = gel_session.gel_face(photo, preset, args, dpi)
        # 旧文档的胶面图是没有 +repage 的旧版裁的：把框挪到它真正裁到的地方再裁一次来比
        sx, sy = crop.legacy_page_shift(st["angle"], st["size0"], st["size1"])
        b = st["gel_box"]
        st = dict(st, gel_box=(b[0] + sx, b[1] + sy, b[2] + sx, b[3] + sy))
        png2 = crop.crop_face(photo, st["angle"], st["gel_box"], width_cm, height_cm, dpi, warn=False)
        same = _same_gel(png, png2)
    except RuntimeError:
        same = False
    if not same:
        out["notes"].append("旧文档没有 Crop 画板（当时的裁切参数没记下来）；要调裁切就重新 render")
        return out
    frame_px = crop.frame_from_stage1(st["gel_box"], st["angle"], st["size0"], st["size1"])
    jpeg, psize, _k = crop.preview(photo)
    lo = figure.crop_layout(frame_px, st["size0"], psize, width_cm)   # 与 render 同一套摆位（框是真矩形）
    out["ops"] = [{"op": "backfill", "artboard": [round(v, 3) for v in lo["artboard"]], "jpeg": jpeg,
                   "preview_size": list(psize), "image_xf": lo["image_xf"], "frame_pt": lo["frame_pt"]}]
    out["crop"] = {"photo_size": list(st["size0"]), "photo_sha1": crop.photo_sha1(photo),
                   "frame_px": [list(p) for p in frame_px],
                   "stage1": {"cue": args.cue, "rect": None, "angle": args.angle, "no_rotate": bool(args.no_rotate),
                              "longest": args.longest, "ink_thr": args.ink_thr, "gel_lanes": args.gel_lanes,
                              "trim_top_cm": 0.0, "trim_bottom_cm": 0.0}}
    out["lines"] = ["已补上 Crop 画板"]
    return out


GEL_SAME_TOL = 2.0          # 旧文档补 Crop 画板：重裁的胶面图与文档里那张平均 |Δ灰度| ≤ 这么多级 = 同一个裁切


def _same_gel(a, b, tol=GEL_SAME_TOL):
    """两张胶面图 PNG 是不是同一个裁切：像素尺寸一样、平均 |Δ灰度| ≤ tol。"""
    wa, ha, ga = profiles.gel_gray(a)
    wb, hb, gb = profiles.gel_gray(b)
    if (wa, ha) != (wb, hb):
        return False
    return sum(abs(x - y) for x, y in zip(ga, gb)) / float(len(ga) or 1) <= tol


def _printable(ops):
    """--dry-run 打印用：op 里的字节（胶面图 PNG、预览 JPEG）换成一句长度说明。"""
    return [{k: ("<%d bytes>" % len(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in op.items()}
            for op in ops]


def _summary(old, model, report, crop_lines=(), reset_lanes=(), reset_all=False, skip_bands=False):
    """这次 sync 相对上一次改了什么（给用户看的几行）；crop_lines = Crop 画板那一步的行（重新裁切 / 补画板）。

    reset_lanes / reset_all = 这一轮 --reset-peaks 清掉的道（见 _peak_reset_lanes）。
    skip_bands = 这一轮做了 marker 重检：逐条新旧 y 由 marker_redetect.format_plan 打印过了，这里不再重说。"""
    out = []
    prev = old.get("prev_centres_cm") or []
    new = workbench.centres(model)
    m = workbench.match_lanes(prev, new) if prev else []
    moved = ["%d %+.2f cm" % (j, new[j - 1] - prev[i]) for j, i in enumerate(m, 1)
             if i is not None and abs(new[j - 1] - prev[i]) >= 0.005]
    added = [j for j, i in enumerate(m, 1) if i is None]
    gone = len(prev) - len([i for i in m if i is not None])
    if moved:
        out.append("泳道移动：%s" % "，".join(moved))
    if added:
        out.append("新泳道：%s" % added)
    if gone:
        out.append("删掉了 %d 条泳道" % gone)
    trimmed = [i for i, ln in enumerate(model["lanes"], 1) if ln["trimmed"]]
    if trimmed:
        out.append("trimmed（不进 Figure）：%s" % trimmed)
    ob = {mw: y for mw, y in (old.get("bands") or [])}
    nb = {b["mw"]: b["y_cm"] for b in model["bands"]}
    bd = ["band %g %s" % (mw, "删除" if mw not in nb else "%+.2f cm" % (nb[mw] - ob[mw]))
          for mw in ob if mw not in nb or abs(nb[mw] - ob[mw]) >= 0.005]
    bd += ["band %g 新增" % mw for mw in nb if mw not in ob]
    if bd and not skip_bands:
        out.append("marker 线：%s" % "，".join(bd))
    pk = _peak_summary(old, model, reset_lanes, reset_all)
    if pk:
        out.append(pk)
    if skip_bands:
        # band 线的新旧 y 已经由 marker_redetect.format_plan 逐条打印过了；别再用「Workbench 没有改动」兜底
        return out + list(crop_lines)
    return (out or ["Workbench 没有改动"]) + list(crop_lines)


def _peak_summary(old, model, reset_lanes=(), reset_all=False):
    """样品带 override 相对上一轮的变化 → 一句（spec §7.5 的摘要）；没变 → None。

    比较 ep-meta 里上一轮的 peak_overrides 与这一轮算出来的。道数减少时说一句被丢掉的 override；
    对不上这一轮自动叫带的 suppress 也说明（它们原样保留，等 --reset-peaks）。
    --reset-peaks 清掉的道先说一句「清掉道 X / 全部 override」，这些道的「恢复 / 撤」不再逐条列；
    一条 override 都没清掉（本来就没有 / 道号不存在）→ 不写这一句。"""
    prev = gel_session.norm_overrides(old.get("peak_overrides"))
    cur = gel_session.norm_overrides(model["meta"].get("peak_overrides"))
    n = len(model["lanes"])
    skip = set(reset_lanes or ())
    cleared = sorted(lane for lane in skip if lane <= n and lane in prev)
    parts = []
    if reset_all:
        if cleared:
            parts.append("清掉全部 override（%d 条泳道）" % len(cleared))
    elif cleared:
        parts.append("清掉道 %s 的 override" % "、".join(str(lane) for lane in cleared))
    for lane in sorted(set(prev) | set(cur)):
        if lane in skip:
            continue
        p, c = prev.get(lane, {}), cur.get(lane, {})
        if lane > n:
            if p:
                parts.append("道 %d 的样品带 override 随泳道消失（现在只有 %d 条泳道）" % (lane, n))
            continue
        ps, cs = set(p.get("suppress") or []), set(c.get("suppress") or [])
        pa, ca = set(p.get("add") or []), set(c.get("add") or [])
        parts += ["道 %d 删 y=%.2f" % (lane, y) for y in sorted(cs - ps)]
        parts += ["道 %d 恢复 y=%.2f" % (lane, y) for y in sorted(ps - cs)]
        parts += ["道 %d 补 y=%.2f" % (lane, y) for y in sorted(ca - pa)]
        parts += ["道 %d 撤 y=%.2f" % (lane, y) for y in sorted(pa - ca)]
    auto = model["meta"].get("peak_auto")           # 这一轮的检测器叫带（_peak_summary 在 _meta_state 之后调用）
    if isinstance(auto, dict):                     # 没有 peak_auto（旧文档第一次 sync）就无从判断失配
        for lane in sorted(cur):
            vals = auto.get(str(lane))
            autos = [float(y) for y in vals if isinstance(y, (int, float))] if isinstance(vals, list) else []
            for y in cur[lane].get("suppress") or []:
                if not any(abs(y - a) <= gel_session.TICK_TOL_CM + 1e-9 for a in autos):
                    parts.append("道 %d 的样品带 override（y=%.2f）已经对不上这一轮的自动叫带" % (lane, y))
    return ("样品带：%s" % "；".join(parts)) if parts else None


# ---------------------------------------------------------------- probe

def cmd_probe(args):
    from . import well_grid
    preset = gel_session.preset_from_args(args)
    lane_count = int(args.gel_lanes or preset["lane_count"])
    png, ga, _stage1 = gel_session.gel_face(args.image, preset, args, 300)
    w, h, gray = profiles.gel_gray(png)
    ppc = w / float(ga["width_cm"])
    sd = well_grid.seed(gray, w, h, ppc, lane_count, float(preset["lane_pitch_cm"]))
    print("[i] 泳道种子：%s，孔距 %.3f cm，首道 %.3f cm，孔深 %.0f%s" % (
        sd["source"], sd["pitch_cm"], sd["first_cm"], sd["depth"], ("；" + sd["note"]) if sd["note"] else ""))
    gel_session.check_pitch(sd, preset)
    top = workbench.TOP_GUARD_CM
    bot = ga["height_cm"] * (1 - workbench.BOTTOM_GUARD)
    # 泳道与 render 同口径：首尾相接（相邻中心中点分界），不是种子给的 0.7·pitch 窄窗 —— 剖面一致，probe 的数字才与 render 对得上
    cells = workbench._cells([sd["first_cm"] + k * sd["pitch_cm"] for k in range(lane_count)],
                             float(ga["width_cm"]), (), sd["pitch_cm"] / 2.0, sd["pitch_cm"] / 2.0)
    profs = []
    for i, ln in enumerate(cells, 1):
        a, b = ln["left_cm"], ln["right_cm"]            # probe 的种子泳道是矩形的
        ys, gs = profiles.lane_profile(gray, w, h, ppc, a, b, top, bot)
        profs.append((ys, gs))
        _b, _ink, sm = profiles.ink_trace(gs, ppc)
        calls, noise = profiles.call_bands(ys, gs, sm, ppc)
        print("  泳道 %2d  %.3f–%.3f cm  噪声 %.2f  条带 %2d：%s" % (
            i, a, b, noise, len(calls), [round(c["y_cm"], 2) for c in calls]))
    if getattr(args, "markers", None):       # ep-9ji：marker 道不看噪声门槛，看前 N 名与断崖
        _n, _ag, mk_labels, cand, mk_note = gel_session.lanes_and_markers(args, lane_count)
        need = workbench.rows_needed(mk_labels)
        print("[i] marker 配置：%s —— 标签要 %d 条" % (mk_note, need))
        for ml in cand:
            ys, gs = profs[ml - 1]
            rows, info = profiles.pick_marker(ys, gs, ppc, need)
            ratio = "%.2f×（第 %d 名 %.2f / 第 %d 名 %.2f）" % (
                info["ratio"], need, info["nth"], need + 1, info["next"]) if info["ratio"] else "—"
            print("  marker 候选 泳道 %2d：取到 %d 条，最短的一条 / 下一条 = %s  %s" % (
                ml, len(rows), ratio, "通过" if info["ok"] else "不过（%s）" % info["reason"]))
            print("      y cm：%s" % [round(r["y_cm"], 2) for r in rows])
    return 0


# ---------------------------------------------------------------- CLI

def _reject_removed(args):
    for flag in REMOVED:
        if getattr(args, "removed_" + flag.lstrip("-").replace("-", "_"), None) not in (None, False):
            raise RuntimeError("%s 已经去掉了：%s" % (flag, REMOVED[flag]))


def _removed_export(_args):
    raise RuntimeError("export 已经去掉了：fig.png 由 VectorCraft 导出；改了文档就跑 `python3 -m epcraft figure sync fig.vectorcraft`")


def _add_removed(a):
    for flag in REMOVED:
        dest = "removed_" + flag.lstrip("-").replace("-", "_")
        if flag in _FLAG_ONLY:
            a.add_argument(flag, dest=dest, action="store_true", help=argparse.SUPPRESS)
        else:
            a.add_argument(flag, dest=dest, default=None, help=argparse.SUPPRESS)


PROBE_HELP = "只探测：胶面、按加样孔放的泳道、每道叫出的带、marker 道的前 N 名；不写文件"
RENDER_HELP = "出 fig.vectorcraft + fig.png（VectorCraft 导出）+ fig.mw.json"
SYNC_HELP = "读 VectorCraft 里改过的 Workbench → 更新文档、fig.png、fig.mw.json"
EDIT_HELP = "改进 Figure 的泳道 / 泳道名 / marker 泳道，然后 sync（不用手写控制口脚本）"


def _add_probe_parser(parent):
    p = parent.add_parser("probe", help=PROBE_HELP)
    p.add_argument("image")
    p.add_argument("--markers", help="marker 配置 JSON（给了就打印 marker 候选道的前 N 名与闸门）")
    p.add_argument("--preset", help="从 marker 预设库里选一个（与 render 同）")
    p.add_argument("--lanes", help="泳道配置 JSON（marker_lanes 决定先试哪几道）")
    gel_session.add_detect_args(p)
    p.set_defaults(func=cmd_probe)
    return p


def _add_render_parser(parent):
    r = parent.add_parser("render", help=RENDER_HELP)
    r.add_argument("image")
    r.add_argument("--out", required=True, help="fig.vectorcraft（给 .png 也行，取同名 stem）")
    r.add_argument("--markers", help="marker 配置 JSON；单个配置，或预设库（如 data/markers.json，配合 --preset）")
    r.add_argument("--preset", help="从预设库里选一个：1 起的序号，或 _preset 描述里的关键词")
    r.add_argument("--lanes", help="泳道配置 JSON（labels 左到右，\"\" = 空道；marker_lanes = 梯子在哪几道）")
    r.add_argument("--trim-lanes", help="不进 Figure 的泳道号（1 起，逗号/区间：1,2,14-15）；照样分析，"
                                        "也能在 VectorCraft 里挪回来")
    r.add_argument("--dpi", type=int, default=300, help="胶面图与 fig.png 的 dpi（默认 300）")
    r.add_argument("--profiles", choices=("stack", "strips"), default=figure.PROFILE_LAYOUT,
                   help="Profiles 画板的版式：stack=每道一行横向曲线（默认）/ strips=每道一条竖向曲线，与胶同比例")
    r.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT, help="VectorCraft 控制口（默认 7797）")
    r.add_argument("--label-angle", type=float, default=0.0,
                   help="泳道名标签的角度（度，逆时针；90 = 从下往上读；默认 0）；lanes.json 里每项可用 angle 覆盖")
    r.add_argument("--marker-label-angle", type=float, default=0.0,
                   help="marker 标签的角度（度，逆时针；默认 0）；markers.json 里每项可用 angle 覆盖")
    r.add_argument("--no-mw", action="store_true", help="不写 fig.mw.json")
    gel_session.add_detect_args(r)
    _add_removed(r)
    r.set_defaults(func=cmd_render)
    return r


def _add_sync_parser(parent):
    s = parent.add_parser("sync", help=SYNC_HELP)
    s.add_argument("doc", help="render 写出的 fig.vectorcraft")
    s.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT, help="VectorCraft 控制口（默认 7797）")
    s.add_argument("--dpi", type=int, default=300, help="ep-meta 里没记 dpi 时用它")
    s.add_argument("--dry-run", action="store_true", help="只打印要做的改动，不动文档")
    s.add_argument("--reset-peaks", nargs="*", type=int, default=None, metavar="N",
                   help="清掉这些泳道的样品带 override，这次 sync 直接按纯自动重叫带、重写刻度："
                        "--reset-peaks 3 12 只清道 3、12；--reset-peaks 不带道号清全部")
    s.add_argument("--reset-marker", action="store_true",
                   help="按当前 Crop 框 + 当前泳道几何在 marker 道重叫显著度前 N 名，重建 band <MW> 线，"
                        "然后照常 sync（标签 / Profiles / fig.png / fig.mw.json 一起更新）；一条撤销步")
    s.add_argument("--lane", type=int, default=None, metavar="N",
                   help="配 --reset-marker：用第 N 条泳道重检（默认 ep-meta.marker_lane）；重检后它就是新 marker 道")
    s.set_defaults(func=cmd_sync)
    return s


def _add_edit_parser(parent):
    ed = parent.add_parser("edit", help=EDIT_HELP)
    ed.add_argument("doc", help="render 写出的 fig.vectorcraft")
    ed.add_argument("--keep", help="只留这些泳道进 Figure（1 起，如 2-7）；其余画 trim 标记")
    ed.add_argument("--trim", help="拿掉这些泳道（如 1,8-15）；给空串 \"\" = 一条都不拿")
    ed.add_argument("--labels", help="泳道名，从泳道 1 起、逗号分隔（空的写成两个逗号之间什么都没有）")
    ed.add_argument("--label", action="append", metavar="N=TEXT", help="单改一条泳道名，可重复：--label 9=M")
    ed.add_argument("--shift-labels", type=int, default=0, metavar="K",
                    help="泳道名整体右移 K 格（负数左移）；先移，再套 --labels / --label")
    ed.add_argument("--marker-lane", type=int, help="band 线整组搬到这条泳道（y 不变）：改用这条梯子标定")
    ed.add_argument("--no-sync", action="store_true", help="只改文档，不跟着 sync")
    ed.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT, help="VectorCraft 控制口（默认 7797）")
    ed.add_argument("--dpi", type=int, default=300, help="ep-meta 里没记 dpi 时用它")
    ed.set_defaults(func=cmd_edit)
    return ed


def cmd_selftest(_args):
    from .smoke import selftest

    return selftest()


def build_parser(canvas=False):
    """→ argparse.ArgumentParser。canvas=False 保持旧的扁平命令（老用法与现有自检）；
    canvas=True 是公开 epcraft 的画板分组命令（见 canvas_cli.add_commands）。"""
    ap = argparse.ArgumentParser(
        prog="epcraft" if canvas else None,
        description=("Electrophoresis gel labeling and offline densitometry, organized by canvas." if canvas else
                     "凝胶电泳照片 → fig.vectorcraft（VectorCraft 里可改）+ fig.png + fig.mw.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    if canvas:
        groups = {}
        for name, help_text in (
                ("figure", "Create, edit and export the Figure canvas"),
                ("crop", "Probe photos and apply Crop canvas edits"),
                ("workbench", "Edit lanes, sync, and export/import lane data"),
                ("profile", "Recompute the Profiles canvas and sample peaks")):
            group = sub.add_parser(name, help=help_text)
            groups[name] = group.add_subparsers(dest="action", required=True)

        _add_render_parser(groups["figure"])
        _add_probe_parser(groups["crop"])
        _add_sync_parser(groups["workbench"])
        _add_edit_parser(groups["figure"])
        # A sync updates every dependent canvas, so the same handler is offered on each canvas group.
        _add_sync_parser(groups["figure"])
        _add_sync_parser(groups["crop"])
        _add_sync_parser(groups["profile"])
        _add_edit_parser(groups["workbench"])

        from .canvas_cli import add_commands
        add_commands(sub, groups, sync=cmd_sync)   # workbench import 导入后跟着同一个 sync
    else:
        _add_probe_parser(sub)
        _add_render_parser(sub)
        _add_sync_parser(sub)
        _add_edit_parser(sub)
        e = sub.add_parser("export", help=argparse.SUPPRESS)
        e.add_argument("rest", nargs=argparse.REMAINDER)       # 老用法的任何参数都吞下，只为报那一句
        e.set_defaults(func=_removed_export)

    t = sub.add_parser("selftest", help="Offline smoke check for the installed package")
    t.set_defaults(func=cmd_selftest)
    d = sub.add_parser("doctor", help="环境自检：python / magick / 字体 / VectorCraft / 数据文件")
    d.set_defaults(func=cmd_doctor)
    return ap


CANVAS_COMMANDS = ("launch", "figure", "crop", "workbench", "profile", "workflow", "densitometry", "dens")


def main(argv=None, canvas=None):
    """canvas=None 时按第一个子命令自动选层级：新画板命令走 canvas，老扁平命令保持原样。"""
    argv = sys.argv[1:] if argv is None else argv
    if canvas is None:
        canvas = bool(argv) and argv[0] in CANVAS_COMMANDS
    ap = build_parser(canvas=canvas)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, OSError, ValueError) as e:
        # 一行错误、非 0 退出，不吐 traceback（缺预设、Workbench 读不通、控制口连不上等都走这里）
        print("[!] %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
