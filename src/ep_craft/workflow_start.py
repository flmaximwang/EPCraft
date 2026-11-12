#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workflow_start —— 分阶段画布流程的前两步（ep-gq1.1）。

    init-canvas  <doc>                       建新文档：Figure / Workbench / Profiles / Crop 四块空画板
    img-to-crop  <image> --doc <doc> ...      认胶面 → 照片预览 + 可编辑裁切框放上 Crop 画板

约定
- init-canvas **不覆盖**已存在的文件（安全第一）；四块画板按 Figure、Workbench、Profiles、Crop
  的顺序建（0..3，与 figure.build_document 一致）。导出按**名字**找 Figure，所以画板顺序改了也照样导出。
- init-canvas 同时按 workbench.LAYERS 建好全部图层（含 photo / crop；内容为空），下游阶段不必自己补层。
- img-to-crop 只改 Crop 画板与 photo / crop 两层，外加 ep-meta；Figure / Workbench / Profiles 与其它图层
  原样保留。它**不**生成下游内容、不导出 PNG。
- 写盘前先处理活文档：VectorCraft 里开着这份文件且有未存改动 → 先 document.save（把用户手工改动落到
  磁盘），再写盘、重开。所有编辑都是"读磁盘 → 改 JSON → 原子写 → 重开"，所以不依赖控制口的创建命令。

ep-meta（与 render 同一个对象：隐藏且锁定的 `ep-meta` 图层里那个叫 `ep-meta` 的文字对象）
    {"workflow": true, "stage": "init-canvas"|"img-to-crop", "dpi": 300,
     "photo": <绝对路径>, "gel_preset": <预设名>,
     "gel": {"width_cm", "height_cm", "lane_count", "lane_pitch_cm"},
     "preset": {...预设原样...},                       # trim / anchor 等
     "crop": {photo_size, photo_sha1, frame_px, stage1}}  # 与 render 的 ep-meta.crop 同形

给下游（workflow_stages）的稳定接口
    BOARDS                          ("Figure", "Workbench", "Profiles", "Crop")
    board_names(document)           → [画板名]
    require_init(document)          → 检查四块画板 + ep-meta 图层；不满足就抛可执行的错
    read_document(path)             → (top, blobs)；离线读盘
    load_builder(path)              → 可写的 builder（保留 top/blobs，用 vc_file 的节点工厂）
    live_document(args, path=None)  → (client, document, blobs)；必要时先存盘（保住手工改动）
    save_live(client, path)         → (top, blobs)
    replace_layer_objects(doc, name, nodes)   → 清空并写入某图层（在 builder 上建节点）
    set_board_rect(doc, name, rect)           → 改某画板矩形，不动别的画板
    load_meta(document) / set_meta(document, meta) / merge_meta(document, patch)
"""
import json
import os

from . import crop as crop_geom
from . import figure
from . import gel_photo
from . import vc_control
from . import vc_file
from . import workbench


BOARDS = ("Figure", "Workbench", "Profiles", "Crop")
BOARD_W, BOARD_H = 300.0, 240.0          # init 的占位尺寸（pt）；下游阶段按胶面尺寸改
BOARD_GAP = figure.BOARD_GAP
META_LAYER = "ep-meta"
META_NAME = "ep-meta"


# ---------------------------------------------------------------- 文档小工具

def read_document(path):
    """离线读 .vectorcraft → (top, blobs)。"""
    return vc_file.read(os.path.abspath(path))


def load_builder(path):
    """读盘 → 可写 builder（top/blobs 原样保留，节点工厂用 vc_file 的模板）。"""
    top, blobs = read_document(path)
    document = top["document"]
    ids = [n["id"] for _lay, n in vc_file.walk(document)] + [lay["id"] for lay in document["layers"]]
    return {"top": top, "blobs": blobs, "tpl": vc_file.new_doc("x")["tpl"], "next": max(ids + [0]) + 1}


def _document(doc):
    return doc["top"]["document"] if "top" in doc else doc


def board_names(document):
    return [ab.get("name") for ab in document.get("artboards") or []]


def _layer(document, name):
    try:
        return vc_file.layer_by_name(document, name)
    except RuntimeError as e:
        raise RuntimeError("文档里%s：%s" % (name, e)) from e


def board_rect(document, name):
    """画板 rect → (x, y, w, h)。"""
    for ab in document.get("artboards") or []:
        if ab.get("name") == name:
            r = ab["rect"]
            return (r["x0"], r["y0"], r["x1"] - r["x0"], r["y1"] - r["y0"])
    raise RuntimeError("文档里没有 %s 画板" % name)


def set_board_rect(doc, name, rect):
    """改画板矩形（pt），返回画板 dict；只动这一块。"""
    document = _document(doc)
    for ab in document.get("artboards") or []:
        if ab.get("name") == name:
            x, y, w, h = (float(v) for v in rect)
            ab["rect"] = {"x0": x, "y0": y, "x1": x + w, "y1": y + h}
            return ab
    raise RuntimeError("文档里没有 %s 画板" % name)


def require_init(doc):
    """img-to-crop 的前提：四块画板都在、ep-meta 图层在、名字不重。收 document 或 builder 都行。"""
    document = _document(doc)
    names = board_names(document)
    missing = [n for n in BOARDS if n not in names]
    if missing:
        raise RuntimeError("文档缺 %s 画板 —— 先跑 `ep-craft workflow init-canvas <doc>`"
                           % " / ".join(missing))
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise RuntimeError("画板名字重复：%s —— 重新 init-canvas" % ", ".join(dup))
    layers = [lay.get("name") for lay in document.get("layers") or []]
    for name in ("ep-meta", "photo", "crop"):
        if name not in layers:
            raise RuntimeError("文档缺 %s 图层 —— 先跑 `ep-craft workflow init-canvas <doc>`" % name)


def replace_layer_objects(doc, layer_name, nodes):
    """清空某顶层图层并写入 nodes（nodes 必须是用同一个 builder 建的对象）。返回图层 dict。"""
    document = _document(doc)
    lay = _layer(document, layer_name)
    vc_file.lay_children(lay).clear()
    vc_file.lay_children(lay).extend(nodes)
    return lay


# ---------------------------------------------------------------- ep-meta

def _meta_node(document):
    lay = _layer(document, META_LAYER)
    found = [n for _l, n in vc_file.walk({"layers": [lay]}) if n.get("name") == META_NAME]
    if len(found) != 1:
        raise RuntimeError("%s 图层里要恰好有一个叫 %s 的文字对象（找到 %d 个）—— 先跑 init-canvas"
                           % (META_LAYER, META_NAME, len(found)))
    return found[0]


def load_meta(document):
    """ep-meta 文字 → dict；没有 / 解析失败都抛可执行的错。"""
    node = _meta_node(_document(document))
    try:
        meta = json.loads(vc_file.text_info(node)["text"])
    except ValueError as e:
        raise RuntimeError("ep-meta 不是合法 JSON（%s）—— 手工改坏了吗？" % e) from e
    if not isinstance(meta, dict):
        raise RuntimeError("ep-meta 不是 JSON 对象")
    return meta


def set_meta(doc, meta):
    """把 dict 写回 ep-meta 文字对象（保留它的字体 / 字号 / 位置）。"""
    node = _meta_node(_document(doc))
    runs = node["kind"]["runs"]
    runs[0]["text"] = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    del runs[1:]
    return meta


def merge_meta(doc, patch):
    meta = load_meta(_document(doc))
    meta.update(patch)
    return set_meta(doc, meta)


# ---------------------------------------------------------------- 活文档

def _ensure_open(client, path):
    """VectorCraft 里打开这份文件（同名文件按完整路径核对）。→ (是否本次打开, 下标)。"""
    path = os.path.abspath(path)
    idx, _dirty = gel_photo._doc_state(client, path)
    if idx is not None:
        client.execute("document.activate", {"index": idx})
        return False, idx
    r = client.open(path)
    if r.get("warnings"):
        print("[!] VectorCraft 打开时提示：%s" % r["warnings"])
    return True, None


def live_document(args, path=None, save_dirty=True):
    """连上控制口并**打开**这份文档 → (client, document, blobs)。

    开着且有未存改动时先 document.save（保住用户手工改动）；blobs 从磁盘读（控制口不给图片字节）。
    """
    path = os.path.abspath(path or getattr(args, "doc", None) or "")
    if not path:
        raise RuntimeError("live_document：缺文档路径")
    port = int(getattr(args, "port", vc_control.DEFAULT_PORT))
    client, _launched = vc_control.connect(port, launch=True)
    idx, dirty = gel_photo._doc_state(client, path)
    if idx is None:
        _ensure_open(client, path)
    elif dirty and save_dirty:
        client.save(path)
    blobs = vc_file.read(path)[1] if os.path.exists(path) else {}
    return client, client.document_json(), blobs


def save_live(client, path):
    """控制口存盘 → (top, blobs)。"""
    return client.save(os.path.abspath(path))


def _prepare_write(args, path):
    """把活文档里的手工改动先存盘、再关掉（若开着），返回 client。之后可以安全地改磁盘文件。"""
    port = int(getattr(args, "port", vc_control.DEFAULT_PORT))
    client, _launched = vc_control.connect(port, launch=True)
    idx, dirty = gel_photo._doc_state(client, path)
    if idx is not None:
        if dirty:
            client.save(path)
            print("[i] 先把 VectorCraft 里的未存改动存盘，再改 Crop")
        client.execute("document.activate", {"index": idx})
        client.execute("file.close", {})
        client.clear_dialogs()
    return client


# ---------------------------------------------------------------- init-canvas

def cmd_init_canvas(args):
    _reject_removed(args)
    path = os.path.abspath(args.doc)
    if os.path.exists(path):
        raise RuntimeError("文档已存在：%s —— init-canvas 不覆盖（请换个文件名，或删掉它再建）" % path)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise RuntimeError("目录不存在：%s" % parent)
    dpi = int(getattr(args, "dpi", 300))
    if dpi <= 0:
        raise RuntimeError("--dpi 要正整数（收到 %r）" % dpi)

    builder = vc_file.new_doc(os.path.splitext(os.path.basename(path))[0])
    document = builder["top"]["document"]
    workbench.make_layers(builder)

    # 画板顺序固定：0 Figure（导出）、1 Workbench、2 Profiles（右）、3 Crop（左）
    w, h, gap = BOARD_W, BOARD_H, BOARD_GAP
    vc_file.add_artboard(builder, "Figure", 0.0, h + gap, w, h)
    vc_file.add_artboard(builder, "Workbench", 0.0, 0.0, w, h)
    vc_file.add_artboard(builder, "Profiles", w + gap, 0.0, w, h)
    vc_file.add_artboard(builder, "Crop", -(w + gap), 0.0, w, h)

    meta = {"workflow": True, "stage": "init-canvas", "dpi": dpi}
    vc_file.lay_children(_layer(document, META_LAYER)).append(vc_file.text(
        builder, META_NAME, 0.0, h + 12.0, json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
        size=4, justify="Left"))

    client = None
    port = int(getattr(args, "port", vc_control.DEFAULT_PORT))
    try:
        client, _launched = vc_control.connect(port, launch=True)
    except vc_control.VCError as e:
        print("[!] VectorCraft 没连上（文档照样会写）：%s" % e)
    vc_file.write(builder, path)
    if client is not None:
        r = client.open(path)
        if r.get("warnings"):
            print("[!] VectorCraft 打开时提示：%s" % r["warnings"])
    print("建好四块空画板：%s（%s）" % (" / ".join(BOARDS), path))
    print("下一步：ep-craft workflow img-to-crop <照片> --doc %s --gel-size <预设>" % path)
    return 0


# ---------------------------------------------------------------- img-to-crop

def cmd_img_to_crop(args):
    _reject_removed(args)
    path = os.path.abspath(args.doc)
    image = os.path.abspath(args.image)
    if not os.path.isfile(path):
        raise RuntimeError("文档不存在：%s —— 先跑 `ep-craft workflow init-canvas %s`" % (path, path))
    if not os.path.isfile(image):
        raise RuntimeError("照片找不到：%s" % image)
    dpi = int(getattr(args, "dpi", 300))
    if dpi <= 0:
        raise RuntimeError("--dpi 要正整数（收到 %r）" % dpi)

    # 先离线核对文档结构（不碰活文档），再认胶面（最贵的一步），最后才动磁盘
    require_init(load_builder(path))

    preset = gel_photo._preset(args)                    # --gel-size 必须给（T1）
    _png, ga, stage1 = gel_photo.gel_face(image, preset, args, dpi)
    spec, crop_meta = gel_photo._crop_artboard(args, preset, stage1)
    lo = figure.crop_layout(spec["frame_px"], spec["photo_size"], spec["preview_size"],
                            float(ga["width_cm"]))

    client = _prepare_write(args, path)                 # 先存活文档里的手工改动并关掉，再读最新内容
    builder = load_builder(path)
    require_init(builder["top"]["document"])            # 存盘后的最新内容再核对一次

    key, w, h = vc_file.add_image(builder, spec["jpeg"], "image/jpeg")
    if (w, h) != (int(spec["preview_size"][0]), int(spec["preview_size"][1])):
        raise RuntimeError("预览 JPEG 是 %dx%d px，crop 规格说 %r" % (w, h, spec["preview_size"]))
    sx, _b, _c, sy, X, Y = lo["image_xf"]
    img = vc_file.image(builder, "photo", key, w, h, X, Y, sx)
    img["kind"]["xf"][3] = sy
    replace_layer_objects(builder, "photo", [img])
    replace_layer_objects(builder, "crop", [vc_file.rect(
        builder, "crop", lo["frame_pt"], figure.CROP_RGB, figure.CROP_W)])
    set_board_rect(builder, "Crop", lo["artboard"])

    gel = {"width_cm": float(ga["width_cm"]), "height_cm": float(ga["height_cm"]),
           "lane_count": int(args.gel_lanes or preset["lane_count"]),
           "lane_pitch_cm": float(preset["lane_pitch_cm"])}
    presets = {k: preset[k] for k in ("width_cm", "height_cm", "lane_count", "lane_pitch_cm",
                                      "trim_top_cm", "trim_bottom_cm", "anchor") if k in preset}
    merge_meta(builder, {"stage": "img-to-crop", "dpi": dpi, "photo": image,
                         "gel_preset": preset.get("name", ""), "gel": gel, "preset": presets,
                         "crop": crop_meta})

    vc_file.write(builder, path)
    r = client.open(path)
    if r.get("warnings"):
        print("[!] VectorCraft 打开时提示：%s" % r["warnings"])
    print("照片 %s 已放到 Crop 画板（%d dpi）：框可拖动 / 旋转 / 缩放；Workbench 等其它画板没动" % (
        os.path.basename(image), dpi))
    print("下一步：ep-craft workflow crop-to-workbench %s --markers <配置>" % path)
    return 0


def _reject_removed(args):
    """CLI 若带了已移除的参数（老脚本），明确拒绝而不是悄悄忽略。"""
    for name in [k for k in vars(args) if k.startswith("removed_") and getattr(args, k)]:
        raise RuntimeError("参数 --%s 已移除" % name[len("removed_"):].replace("_", "-"))
