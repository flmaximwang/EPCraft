#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gel_photo.py —— 凝胶电泳照片 → 可在 VectorCraft 里改的 .vectorcraft + 它导出的 .png + MW 报告

子命令
    render  <图> --out fig.vectorcraft [配置...]   认胶面 → 正畸裁切 → 按加样孔放泳道 → 叫带/标定 →
                                                  写 fig.vectorcraft，用 VectorCraft 导出 fig.png，写 fig.mw.json
    sync    fig.vectorcraft                       读用户在 VectorCraft 里改过的 Workbench（活文档）→ 重算 →
                                                  原地更新标签/曲线/Figure → 存盘 → 重新导出 fig.png / fig.mw.json
    probe   <图> [--gel-size ...]                 只探测，打印胶面、泳道种子、每道叫出的带；不写文件
    doctor                                        环境自检（python / magick / 字体 / VectorCraft）
    selftest                                      不读用户文件、不开 app 的自检

依赖：系统 python3（纯 stdlib）+ ImageMagick 7（`magick`，读照片、正畸、裁切）+ VectorCraft 0.14
（`--control` 控制口；fig.png 由它导出，所以 png 永远就是用户在 app 里看到的那张）。
本文件只放 CLI 与编排；实现分在同目录模块：
    magick_io    magick 外呼 / PPM        gel_detect   认胶面          gel_area     stage 1 胶面区域
    geometry     旋转与位置口径           config       配置读取        text_layout  字体与文字量宽
    lane_select  泳道号语法、保留段       mw_report    MW 数学与报告    env_check    doctor
    vc_file      .vectorcraft 读写         vc_control   VectorCraft 控制口
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crop                                                                            # noqa: E402
import figure                                                                          # noqa: E402
import profiles                                                                        # noqa: E402
import vc_control                                                                      # noqa: E402
import vc_file                                                                         # noqa: E402
import workbench                                                                       # noqa: E402
from config import as_entry, load_cfg, load_gel_size, load_markers, parse_gel_cm     # noqa: E402
from env_check import cmd_doctor                                                       # noqa: E402
from gel_area import compute                                                           # noqa: E402
from gel_detect import detect_gel                                                      # noqa: E402
from lane_select import parse_keep                                                     # noqa: E402
from mw_report import format_report_v2, write_v2                                       # noqa: E402
from text_layout import window_deg                                                     # noqa: E402
from selftest import selftest                                                          # noqa: E402

SKILL_VERSION = "vectorcraft-1"

# 已经去掉的参数 → 用什么代替（V6；给了就报这一句，不静默忽略）
REMOVED = {
    "--keep-lanes": "--trim-lanes（反过来写：要从 Figure 拿掉的泳道号），或在 VectorCraft 的 trimmed 图层里给那条泳道画一条短线（trim 标记）",
    "--svg": "fig.vectorcraft 本身就是矢量文档；要 SVG 就在 VectorCraft 里 File → Export",
    "--no-editor": "不再写 HTML 编辑器；编辑在 VectorCraft 里做",
    "--debug": "检测结果都在 fig.vectorcraft 的 Workbench 画板上（lanes / window / marker bands / analysis 图层）",
    "--record": "探测记录就是 Workbench 上的线（加 ep-meta）；数值在 fig.mw.json 里",
    "--pair-force": "带数对不上时 render 自动出草稿标定；在 Workbench 上改 band 线（拖、删、改名 band <MW>），然后 sync",
    "--marker-policy": "marker 泳道被 trim 时只用来标定、不画标签（固定行为）",
    "--margin-frac": "页边由 Figure 的页面规则自动推导",
    "--outline": "文字样式在 VectorCraft 里改",
    "--no-outline": "文字样式在 VectorCraft 里改",
    "--font": "字体在 VectorCraft 里改（默认 Arial）",
    "--mw-method": "MW 一律在相邻两条 marker 带之间做两点插值，没有可选方法（禁止全局拟合）",
    "--splice-gap-cm": "拼缝固定 0.15 cm",
}
_FLAG_ONLY = ("--svg", "--no-editor", "--pair-force", "--no-outline")


def _rect_arg(s):
    if not s:
        return None
    v = [float(t) for t in s.replace(",", " ").split()]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("--rect 要 4 个数：x0,y0,x1,y1（原图像素）")
    return tuple(v)


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


def _preset(args):
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


def gel_face(image, preset, args, dpi):
    """stage 1：认胶面 → 按预设定形 → 正畸裁切成「胶面图」PNG 字节（dpi 像素/英寸，宽 = width_cm）。

    返回 (png 字节, ga, stage1)。这是老管线里唯一还留着的一级；泳道与条带都从这张图上来（V8）。
    stage1 = {"angle", "gel_box", "size0", "size1"}：Crop 画板要的量（crop.frame_from_stage1 的参数），
    size0 = 自动定向后的原图 (W0, H0)，size1 = 旋转画布 (W1, H1)。裁切本身是 crop.crop_face。
    """
    info = detect_gel(image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
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


def _check_pitch(sd, preset, tol=0.10):
    """定标核对：加样孔量出的孔距 vs 预设 lane_pitch_cm。孔距是在已定标的胶面上量的，差得多 =
    宽度定标或预设有一个不对。只警告，不改定标。"""
    if sd["source"] != "wells":
        return
    dev = abs(sd["pitch_cm"] - float(preset["lane_pitch_cm"])) / float(preset["lane_pitch_cm"])
    if dev > tol:
        print("[!] scale: 加样孔孔距 %.3f cm，预设 lane_pitch_cm %.3f cm（差 %.0f%%）—— 核对预设的 width_cm / "
              "lane_pitch_cm，或照片是不是裁过（--gel-lanes）" % (sd["pitch_cm"], float(preset["lane_pitch_cm"]), dev * 100))


def _lanes_and_markers(args, lane_count):
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


def _doc_state(client, path):
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


# ---------------------------------------------------------------- render

def cmd_render(args):
    _reject_removed(args)
    preset = _preset(args)
    dpi = args.dpi
    out = os.path.abspath(args.out if args.out.endswith(".vectorcraft")
                          else os.path.splitext(args.out)[0] + ".vectorcraft")
    stem = os.path.splitext(out)[0]
    lane_count = int(args.gel_lanes or preset["lane_count"])
    names, angles, mk_labels, cand, mk_note = _lanes_and_markers(args, lane_count)
    print("[i] 胶体预设 %s：%d 条泳道，孔距先验 %.3f cm；输出 %d dpi；marker 配置：%s" % (
        preset.get("name", "—"), lane_count, float(preset["lane_pitch_cm"]), dpi, mk_note))
    trim = parse_keep(args.trim_lanes, lane_count) if args.trim_lanes else []

    png, ga, stage1 = gel_face(args.image, preset, args, dpi)
    crop_spec, crop_meta = _crop_artboard(args, preset, stage1)
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
    counts = []                          # [(泳道, MarkerCountError)]：只是带数对不上的那些
    build = lambda ml, draft=False: workbench.from_image(   # noqa: E731
        png, ga["width_cm"], ga["height_cm"], lane_count, float(preset["lane_pitch_cm"]), names, mk_labels, ml,
        trim=trim, meta=meta, draft=draft)
    for ml in cand:                      # 写了几条 marker 泳道就挨个试：第一条叫不全（如胶边损坏）就换下一条
        try:
            model, sd = build(ml)
            break
        except workbench.WorkbenchError as e:
            err = e
            if isinstance(e, workbench.MarkerCountError):
                counts.append((ml, e))
            if len(cand) > 1:
                print("[!] marker 泳道 %d 配不上：%s" % (ml, str(e).splitlines()[0]))
    if model is None:
        if args.no_draft or not counts:
            raise err
        # 草稿：带数最接近的那条（并列取先写的）凑够条数，band 线让用户拖到位 —— 不当真
        ml, e = min(counts, key=lambda c: abs(len(c[1].rows) - c[1].need))
        model, sd = build(ml, draft=True)
        print("[!] 草稿标定：没有一条 marker 泳道叫出的带数与标签对得上（泳道 %d 叫出 %d 条，要 %d 条）。"
              "band 线是按顺序猜的位置，MW 不可信 —— 在 Workbench 上把每条 band <MW> 线拖到梯子上对应的带"
              "（梯子上没有的就删掉），然后 sync；band 线动过之后草稿标记自动去掉。"
              % (ml, len(e.rows), e.need))
    print("[i] 泳道种子：%s，%d 道，孔距 %.3f cm，首道中心 %.3f cm（孔深 %.0f 灰度级）%s" % (
        {"wells": "按加样孔定相", "even": "看不清孔，预设间距居中"}[sd["source"]], lane_count, sd["pitch_cm"],
        sd["first_cm"], sd["depth"], ("；" + sd["note"]) if sd["note"] else ""))
    print("[i] marker 泳道 %d：%d 条 band 线（%s）" % (model["marker_lane"], len(model["bands"]),
                                                "草稿：位置是猜的" if model["meta"].get("draft") else "来自剖面上叫出的峰"))
    _check_pitch(sd, preset)

    report = _mark_draft(model, profiles.analyze(model, png, names))
    model["meta"].update(_meta_state(model, report))
    model["meta"]["crop"] = crop_meta                   # ep-meta 与 plan_sync 写回的同一份（第一次 sync = 0 个操作）
    doc = figure.build_document(model, report, png, os.path.basename(stem), layout=args.profiles, crop=crop_spec)

    client, _launched = vc_control.connect(args.port, launch=True)
    idx, dirty = _doc_state(client, out)
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
          "然后跑 `gel_photo.py sync %s`" % (args.port, out))
    _ask_names(model)
    return 0


def _crop_artboard(args, preset, stage1):
    """Crop 画板要的东西（crop-artboard spec §3）→ (build_document 的 crop 规格, ep-meta 的 crop 键)。
    frame_px = crop_face 裁到的那块（原图 px）。"""
    frame_px = crop.frame_from_stage1(stage1["gel_box"], stage1["angle"], stage1["size0"], stage1["size1"])
    jpeg, psize, _k = crop.preview(args.image)
    rect = _rect_arg(args.rect)
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


DRAFT_NOTE = "标定是草稿（band 线位置是猜的，MW 不可信）—— 在 Workbench 上把 band 线拖到梯子的带上，再 sync"


def _mark_draft(model, report):
    """ep-meta 带着 draft → 报告也标上（calibration.draft = true，注里放第一条）。返回 report。"""
    if model["meta"].get("draft"):
        report["calibration"]["draft"] = True
        report["notes"] = [DRAFT_NOTE] + [n for n in report["notes"] if n != DRAFT_NOTE]
    return report


def _meta_state(model, report):
    """写进 ep-meta 的「上一次」状态：sync 拿它说清楚这次改了什么（R² 不再有：标定不做全局拟合）。"""
    return {"bands": [[b["mw"], b["y_cm"]] for b in model["bands"]]}


def _ask_names(model):
    missing = [i for i, n in enumerate(model["meta"].get("lane_names") or [], 1)
               if n is None and i <= len(model["lanes"]) and not model["lanes"][i - 1]["trimmed"]]
    if missing:
        print("[?] 泳道 %s 还没有名字（标签是 \"?\"）—— 问用户，再改那几个标签的文字" % missing)


# ---------------------------------------------------------------- sync

def cmd_sync(args):
    import sync_exec
    path = os.path.abspath(args.doc)
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s（先 render）" % path)
    stem = os.path.splitext(path)[0]
    client, _launched = vc_control.connect(args.port, path=path, launch=True)
    idx, _dirty = _doc_state(client, path)
    if idx is None:
        client.open(path)
        idx, _dirty = _doc_state(client, path)
    client.execute("document.activate", {"index": idx})
    dj = client.document_json()
    _top, blobs = vc_file.read(path)                     # 胶面图从磁盘上那份读（gel 层锁定，图不会变）
    gel = _gel_node(dj)
    if gel is not None and gel["kind"]["key"] not in blobs:
        _top, blobs = client.save(path)                  # 活文档的胶面图不在磁盘那份里（如撤销了上次的重新裁切）
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
    if model["meta"].get("draft") and not _same_bands(old.get("bands"), model["bands"]):
        model["meta"].pop("draft")                       # 用户动过 band 线：标定以他的线为准
        print("[i] band 线改过了：去掉草稿标记")
    report = _mark_draft(model, profiles.analyze(model, png, [None] * len(model["lanes"])))
    model["meta"].update(_meta_state(model, report))
    ops, meta = figure.plan_sync(dj, model, report, layout=layout)
    ops = [op for op in cs["ops"] if op["op"] == "relink"] + ops + \
          [op for op in cs["ops"] if op["op"] != "relink"]          # relink 最先（sync_exec 也会保证）
    names = meta["lane_names"]
    report = dict(report, lanes=[dict(ln, label=names[ln["lane"] - 1]) for ln in report["lanes"]],
                  notes=[n for n in report["notes"] if not n.endswith("还没有名字")]
                  + ["泳道 %d 还没有名字" % ln["lane"] for ln in report["lanes"]
                     if names[ln["lane"] - 1] is None and not ln["trimmed"]])
    for line in _summary(old, model, report, cs["lines"]):
        print("[i] %s" % line)
    if args.dry_run:
        print(json.dumps(_printable(ops), ensure_ascii=False, indent=1))
        return 0
    if ops:
        done = sync_exec.apply_ops_live(client, ops, dj)
        print("[i] 已更新：%s（一个撤销步「ep-craft sync」，⌘Z 可整体撤回）" % sync_exec.describe(done))
    else:
        print("[i] 文档已经是最新的（0 个改动）")
    client.save(path)
    dpi = int(meta.get("dpi") or args.dpi)
    w, h, d = client.export_png(stem + ".png", 0, dpi)
    write_v2(report, stem + ".mw.json")
    print(format_report_v2(report))
    print("输出 %s（已存盘）、%s（%d×%d px, %d dpi）、%s.mw.json" % (path, stem + ".png", w, h, round(d), stem))
    model["meta"] = meta
    _ask_names(model)
    return 0


def _same_bands(prev, bands, tol=0.005):
    """ep-meta 里上一次的 band 线 [[mw, y], …] 与现在的一样（MW 相同、y 差 < tol cm）？"""
    a = sorted((float(mw), float(y)) for mw, y in (prev or []))
    b = sorted((float(x["mw"]), float(x["y_cm"])) for x in bands)
    return len(a) == len(b) and all(p[0] == q[0] and abs(p[1] - q[1]) < tol for p, q in zip(a, b))


# ---------------------------------------------------------------- edit

def cmd_edit(args):
    """改哪些泳道进 Figure / 泳道名 / 用哪条泳道当 marker：一个撤销步「ep-craft edit」，然后照常 sync。"""
    import doc_edit
    import sync_exec
    if args.keep and args.trim is not None:
        raise RuntimeError("--keep 与 --trim 只给一个")
    path = os.path.abspath(args.doc)
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s（先 render）" % path)
    client, _launched = vc_control.connect(args.port, path=path, launch=True)
    idx, _dirty = _doc_state(client, path)
    if idx is None:
        client.open(path)
        idx, _dirty = _doc_state(client, path)
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
        done = sync_exec.apply_ops_live(client, ops, dj, label="ep-craft edit")
        print("[i] 已改：%s（一个撤销步「ep-craft edit」）" % sync_exec.describe(done))
    else:
        print("[i] 文档里已经是这样了（0 个改动）")
    if args.no_sync:
        return 0
    return cmd_sync(argparse.Namespace(doc=args.doc, port=args.port, dpi=args.dpi, dry_run=False))


def _gel_node(document):
    """Workbench 的胶面图（gel 图层里叫 gel 的图片）；没有 / 不唯一 → None。"""
    lay = vc_file.layer_by_name(document, "gel")
    imgs = [n for n in (vc_file.lay_children(lay) if lay else [])
            if n["kind"].get("type") == "image" and n.get("name") == "gel"]
    return imgs[0] if len(imgs) == 1 else None


def _doc_meta(document):
    """ep-meta 文字对象的 JSON（读不出 → {}；from_document 会为此另记提示）。"""
    try:
        mt = [n for n in vc_file.lay_children(vc_file.layer_by_name(document, "ep-meta") or {"kind": {}})
              if n.get("name") == "ep-meta"]
        return json.loads(vc_file.text_info(mt[0])["text"]) if mt else {}
    except (ValueError, RuntimeError, KeyError):
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
    gel = _gel_node(dj)
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
        from geometry import load_dims_after
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
    args = _default_detect_args()
    preset = {"name": meta.get("gel_preset", ""), "width_cm": round(width_cm, 2), "height_cm": round(height_cm, 2),
              "trim_top_cm": 0.0, "trim_bottom_cm": 0.0, "anchor": "bottom",
              "lane_pitch_cm": 0.0, "lane_count": 0}      # 宽高 = 胶面 cm（预设一般只到 0.01 cm）；孔距这里用不上
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _png, _ga, st = gel_face(photo, preset, args, dpi)
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


def _default_detect_args():
    """stage 1 的默认参数（与 CLI 的 _add_detect 同一份默认值）：旧文档的 ep-meta 里没记 render 参数。"""
    ap = argparse.ArgumentParser(add_help=False)
    _add_detect(ap)
    return ap.parse_args([])


def _printable(ops):
    """--dry-run 打印用：op 里的字节（胶面图 PNG、预览 JPEG）换成一句长度说明。"""
    return [{k: ("<%d bytes>" % len(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in op.items()}
            for op in ops]


def _summary(old, model, report, crop_lines=()):
    """这次 sync 相对上一次改了什么（给用户看的几行）；crop_lines = Crop 画板那一步的行（重新裁切 / 补画板）。"""
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
    if bd:
        out.append("marker 线：%s" % "，".join(bd))
    return (out or ["Workbench 没有改动"]) + list(crop_lines)


# ---------------------------------------------------------------- probe

def cmd_probe(args):
    import well_grid
    preset = _preset(args)
    lane_count = int(args.gel_lanes or preset["lane_count"])
    png, ga, _stage1 = gel_face(args.image, preset, args, 300)
    w, h, gray = profiles.gel_gray(png)
    ppc = w / float(ga["width_cm"])
    sd = well_grid.seed(gray, w, h, ppc, lane_count, float(preset["lane_pitch_cm"]))
    print("[i] 泳道种子：%s，孔距 %.3f cm，首道 %.3f cm，孔深 %.0f%s" % (
        sd["source"], sd["pitch_cm"], sd["first_cm"], sd["depth"], ("；" + sd["note"]) if sd["note"] else ""))
    _check_pitch(sd, preset)
    top = workbench.TOP_GUARD_CM
    bot = ga["height_cm"] * (1 - workbench.BOTTOM_GUARD)
    for i, (a, b) in enumerate(sd["lanes"], 1):
        ys, gs = profiles.lane_profile(gray, w, h, ppc, a, b, top, bot)
        _b, _ink, sm = profiles.ink_trace(gs, ppc)
        calls, noise = profiles.call_bands(ys, gs, sm, ppc)
        print("  泳道 %2d  %.3f–%.3f cm  噪声 %.2f  条带 %2d：%s" % (
            i, a, b, noise, len(calls), [round(c["y_cm"], 2) for c in calls]))
    return 0


# ---------------------------------------------------------------- CLI

def _reject_removed(args):
    for flag in REMOVED:
        if getattr(args, "removed_" + flag.lstrip("-").replace("-", "_"), None) not in (None, False):
            raise RuntimeError("%s 已经去掉了：%s" % (flag, REMOVED[flag]))


def _removed_export(_args):
    raise RuntimeError("export 已经去掉了：fig.png 由 VectorCraft 导出；改了文档就跑 `gel_photo.py sync fig.vectorcraft`")


def _add_detect(a):
    """probe / render 共用的 stage 1 参数（sync 补 Crop 画板时也拿它的默认值）。"""
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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="凝胶电泳照片 → fig.vectorcraft（VectorCraft 里可改）+ fig.png + fig.mw.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add_detect = _add_detect

    def add_removed(a):
        for flag in REMOVED:
            dest = "removed_" + flag.lstrip("-").replace("-", "_")
            if flag in _FLAG_ONLY:
                a.add_argument(flag, dest=dest, action="store_true", help=argparse.SUPPRESS)
            else:
                a.add_argument(flag, dest=dest, default=None, help=argparse.SUPPRESS)

    p = sub.add_parser("probe", help="只探测：胶面、按加样孔放的泳道、每道叫出的带；不写文件")
    p.add_argument("image")
    add_detect(p)
    p.set_defaults(func=cmd_probe)

    r = sub.add_parser("render", help="出 fig.vectorcraft + fig.png（VectorCraft 导出）+ fig.mw.json")
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
    r.add_argument("--no-draft", action="store_true",
                   help="marker 泳道的带数与标签都对不上时拒绝（默认：出草稿标定，band 线让用户拖到位）")
    add_detect(r)
    add_removed(r)
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("sync", help="读 VectorCraft 里改过的 Workbench → 更新文档、fig.png、fig.mw.json")
    s.add_argument("doc", help="render 写出的 fig.vectorcraft")
    s.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT, help="VectorCraft 控制口（默认 7797）")
    s.add_argument("--dpi", type=int, default=300, help="ep-meta 里没记 dpi 时用它")
    s.add_argument("--dry-run", action="store_true", help="只打印要做的改动，不动文档")
    s.set_defaults(func=cmd_sync)

    ed = sub.add_parser("edit", help="改进 Figure 的泳道 / 泳道名 / marker 泳道，然后 sync（不用手写控制口脚本）")
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

    t = sub.add_parser("selftest")
    t.set_defaults(func=lambda a: selftest())

    e = sub.add_parser("export", help=argparse.SUPPRESS)
    e.add_argument("rest", nargs=argparse.REMAINDER)       # 老用法的任何参数都吞下，只为报那一句
    e.set_defaults(func=_removed_export)

    d = sub.add_parser("doctor", help="环境自检：python / magick / 字体 / VectorCraft / 数据文件")
    d.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        # 一行错误、非 0 退出，不吐 traceback（缺预设、Workbench 读不通等都走这里）
        print("[!] %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
