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
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import figure                                                                          # noqa: E402
import profiles                                                                        # noqa: E402
import vc_control                                                                      # noqa: E402
import vc_file                                                                         # noqa: E402
import workbench                                                                       # noqa: E402
from config import load_cfg, load_gel_size, load_markers, parse_gel_cm                 # noqa: E402
from env_check import cmd_doctor                                                       # noqa: E402
from gel_area import compute                                                           # noqa: E402
from gel_detect import detect_gel                                                      # noqa: E402
from geometry import load_dims_after                                                   # noqa: E402
from lane_select import parse_keep                                                     # noqa: E402
from magick_io import MAGICK, _run                                                     # noqa: E402
from mw_report import format_report_v2, write_v2                                       # noqa: E402
from selftest import selftest                                                          # noqa: E402

SKILL_VERSION = "vectorcraft-1"

# 已经去掉的参数 → 用什么代替（V6；给了就报这一句，不静默忽略）
REMOVED = {
    "--keep-lanes": "--trim-lanes（反过来写：要从 Figure 拿掉的泳道号），或在 VectorCraft 里把那条泳道的两条线挪进 trimmed 图层",
    "--svg": "fig.vectorcraft 本身就是矢量文档；要 SVG 就在 VectorCraft 里 File → Export",
    "--no-editor": "不再写 HTML 编辑器；编辑在 VectorCraft 里做",
    "--debug": "检测结果都在 fig.vectorcraft 的 Workbench 画板上（lanes / window / marker bands / analysis 图层）",
    "--record": "探测记录就是 Workbench 上的线（加 ep-meta）；数值在 fig.mw.json 里",
    "--pair-force": "不硬配：在 Workbench 上改 band 线（拖、删、改名 band <MW>），然后 sync",
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

    返回 (png 字节, ga)。这是老管线里唯一还留着的一级；泳道与条带都从这张图上来（V8）。
    """
    info = detect_gel(image, args.longest, args.cue, _rect_arg(args.rect), args.ink_thr)
    ang = args.angle if args.angle is not None else round(info["angle_vertical"], 3)
    if args.no_rotate:
        ang = 0.0
    ga = compute(info, preset, ang, gel_lanes=args.gel_lanes, pitch_px=False)   # scale 核对改由孔距做
    print("角度: 竖边拟合 %.3f°；本次用 %.3f°" % (info["angle_vertical"], ang))
    _print_stage1(ga, preset, args.gel_lanes)
    (_W0, _H0), (W1, H1) = load_dims_after(image, ang)
    bb = ga["gel_box"]
    x0, y0 = max(0, int(round(bb[0]))), max(0, int(round(bb[1])))
    x1, y1 = min(W1, int(round(bb[2]))), min(H1, int(round(bb[3])))
    gw = int(round(ga["width_cm"] / 2.54 * dpi))
    gh = int(round(ga["height_cm"] / 2.54 * dpi))
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cmd = [MAGICK, image, "-auto-orient"]
        if abs(ang) > 1e-6:
            cmd += ["-rotate", "%.4f" % ang]
        # 别用 -strip：IM7 里它会把后面设的 dpi 一起丢掉（实测）；+profile 去色彩配置
        cmd += ["-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0), "+repage",
                "-resize", "%dx%d!" % (gw, gh), "+profile", "*", "-depth", "8",
                "-units", "PixelsPerInch", "-density", str(dpi), tmp]
        _run(cmd)
        with open(tmp, "rb") as f:
            return f.read(), ga
    finally:
        os.unlink(tmp)


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
    """两个配置 → (泳道名[lane_count], marker 标签, marker 泳道候选[], 说明)。"""
    if not args.markers:
        raise RuntimeError("要 --markers <json>（单个配置，或 data/markers.json 这种预设库 + --preset）")
    mk, mk_labels, mk_note = load_markers(args.markers, args.preset)
    names = [""] * lane_count
    cand = []
    if args.lanes:
        cfg, labels = load_cfg(args.lanes, "泳道配置")
        if len(labels) > lane_count:
            raise RuntimeError("泳道配置有 %d 个名字，胶上只有 %d 条泳道" % (len(labels), lane_count))
        names[:len(labels)] = [str(x) for x in labels]
        cand = [int(v) for v in (cfg.get("marker_lanes") or [])]
    if mk.get("marker_lane") is not None and int(mk["marker_lane"]) not in cand:
        cand.append(int(mk["marker_lane"]))
    cand = [c for c in cand if 1 <= c <= lane_count] or [1]
    return names, mk_labels, cand, mk_note


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
    names, mk_labels, cand, mk_note = _lanes_and_markers(args, lane_count)
    print("[i] 胶体预设 %s：%d 条泳道，孔距先验 %.3f cm；输出 %d dpi；marker 配置：%s" % (
        preset.get("name", "—"), lane_count, float(preset["lane_pitch_cm"]), dpi, mk_note))
    trim = parse_keep(args.trim_lanes, lane_count) if args.trim_lanes else []

    png, ga = gel_face(args.image, preset, args, dpi)
    meta = {"skill": "ep-labeling", "skill_version": SKILL_VERSION, "photo": os.path.abspath(args.image),
            "gel_preset": preset.get("name", ""), "marker_preset": mk_note, "dpi": dpi,
            "profiles_layout": args.profiles}
    model, err, sd = None, None, None
    for ml in cand:                      # 写了几条 marker 泳道就挨个试：第一条叫不全（如胶边损坏）就换下一条
        try:
            model, sd = workbench.from_image(png, ga["width_cm"], ga["height_cm"], lane_count,
                                             float(preset["lane_pitch_cm"]), names, mk_labels, ml,
                                             trim=trim, meta=meta)
            break
        except workbench.WorkbenchError as e:
            err = e
            if len(cand) > 1:
                print("[!] marker 泳道 %d 配不上：%s" % (ml, str(e).splitlines()[0]))
    if model is None:
        raise err
    print("[i] 泳道种子：%s，%d 道，孔距 %.3f cm，首道中心 %.3f cm（孔深 %.0f 灰度级）%s" % (
        {"wells": "按加样孔定相", "even": "看不清孔，预设间距居中"}[sd["source"]], lane_count, sd["pitch_cm"],
        sd["first_cm"], sd["depth"], ("；" + sd["note"]) if sd["note"] else ""))
    print("[i] marker 泳道 %d：%d 条 band 线（来自剖面上叫出的峰）" % (model["marker_lane"], len(model["bands"])))
    _check_pitch(sd, preset)

    report = profiles.analyze(model, png, names)
    model["meta"].update(_meta_state(model, report))
    doc = figure.build_document(model, report, png, os.path.basename(stem), layout=args.profiles)

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
    model, notes = workbench.from_document(dj)          # 读不通 → WorkbenchError，什么都不动
    for n in notes:
        print("[i] %s" % n)
    old = dict(model["meta"])
    for legacy in ("r2",):                      # 旧文档的 ep-meta 里有全局拟合的 R²：顺手清掉
        model["meta"].pop(legacy, None)
    _top, blobs = vc_file.read(path)                     # 胶面图从磁盘上那份读（gel 层锁定，图不会变）
    key = next(n for n in vc_file.lay_children(vc_file.layer_by_name(dj, "gel"))
               if n.get("name") == "gel")["kind"]["key"]
    png = blobs[key]
    layout = old.get("profiles_layout") or figure.PROFILE_LAYOUT
    report = profiles.analyze(model, png, [None] * len(model["lanes"]))
    model["meta"].update(_meta_state(model, report))
    ops, meta = figure.plan_sync(dj, model, report, layout=layout)
    names = meta["lane_names"]
    report = dict(report, lanes=[dict(ln, label=names[ln["lane"] - 1]) for ln in report["lanes"]],
                  notes=[n for n in report["notes"] if not n.endswith("还没有名字")]
                  + ["泳道 %d 还没有名字" % ln["lane"] for ln in report["lanes"]
                     if names[ln["lane"] - 1] is None and not ln["trimmed"]])
    for line in _summary(old, model, report):
        print("[i] %s" % line)
    if args.dry_run:
        print(json.dumps(ops, ensure_ascii=False, indent=1))
        return 0
    if ops:
        done = sync_exec.apply_ops_live(client, ops, dj)
        print("[i] 已更新：%s（一个撤销步「ep-labeling sync」，⌘Z 可整体撤回）" % sync_exec.describe(done))
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


def _summary(old, model, report):
    """这次 sync 相对上一次改了什么（给用户看的几行）。"""
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
    return out or ["Workbench 没有改动"]


# ---------------------------------------------------------------- probe

def cmd_probe(args):
    import well_grid
    preset = _preset(args)
    lane_count = int(args.gel_lanes or preset["lane_count"])
    png, ga = gel_face(args.image, preset, args, 300)
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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="凝胶电泳照片 → fig.vectorcraft（VectorCraft 里可改）+ fig.png + fig.mw.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_detect(a):
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
    r.add_argument("--no-mw", action="store_true", help="不写 fig.mw.json")
    add_detect(r)
    add_removed(r)
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("sync", help="读 VectorCraft 里改过的 Workbench → 更新文档、fig.png、fig.mw.json")
    s.add_argument("doc", help="render 写出的 fig.vectorcraft")
    s.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT, help="VectorCraft 控制口（默认 7797）")
    s.add_argument("--dpi", type=int, default=300, help="ep-meta 里没记 dpi 时用它")
    s.add_argument("--dry-run", action="store_true", help="只打印要做的改动，不动文档")
    s.set_defaults(func=cmd_sync)

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
