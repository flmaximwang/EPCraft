#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""".vectorcraft 文件的读写（VectorCraft 0.14，format version 4）。不碰 app，纯 stdlib。

实测依据见 references/vectorcraft.md §1：
- app 的加载器很严，缺任何字段整份文件打不开 → 每种对象都从 app 自己存的
  templates/kinds.vectorcraft **深拷贝**，只改我们要改的字段，绝不从零拼 JSON；
  文档级字段（色板、图形样式、网格……）也从同一份模板来。
- 未知键存一次就被 app 丢掉 → 这里不写任何自定义键（运行元数据走 ep-meta 文字对象）。
- 写：inline 编码（单个 JSON，图片 base64 放在顶层 images[key].data）。
- 读：inline / `\\n\\0VCBLOBS\\0` 二进制容器 / gzip 三种都收。

用法：
    doc = new_doc("fig")
    add_artboard(doc, "Figure", 0, 0, 300, 200)
    lay = add_layer(doc, "lanes", printable=False)
    lay_children(lay).append(line(doc, "lane", (10, 0), (10, 100), (0, .7, 1)))
    write(doc, "fig.vectorcraft")
    top, blobs = read("fig.vectorcraft")
坐标一律 pt，原点左上，y 向下；1 cm = 72/2.54 pt。
"""
import base64
import copy
import gzip
import hashlib
import json
import math
import os
import struct


TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "templates", "kinds.vectorcraft")
BLOB_MARK = b"\0VCBLOBS\0"
PT_PER_CM = 72.0 / 2.54
# 模板里每种对象的名字（templates/kinds.vectorcraft，见 references/vectorcraft.md §5）
_KIND_NAMES = ("clip", "line", "text", "polyline")

_tpl_cache = {}


# ---------------------------------------------------------------- 读

def read(path):
    """→ (top, blobs)：top 是整份 JSON；blobs = {image key: bytes}（三种编码都收）。"""
    with open(path, "rb") as f:
        raw = f.read()
    return loads(raw, path)


def loads(raw, where="<bytes>"):
    """read() 的内存版：bytes → (top, blobs)。"""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    i = raw.find(BLOB_MARK)
    head, tail = (raw[:i], raw[i + len(BLOB_MARK):]) if i >= 0 else (raw, b"")
    try:
        top = json.loads(head.decode("utf-8"))
    except ValueError as e:
        raise RuntimeError("%s 不是 .vectorcraft（JSON 解析失败：%s）" % (where, e))
    if top.get("format") != "vectorcraft" or "document" not in top:
        raise RuntimeError("%s 不是 .vectorcraft（缺 format/document）" % where)
    blobs = {}
    for key, ent in (top.get("images") or {}).items():
        if "data" in ent:
            blobs[key] = base64.b64decode(ent["data"])
        elif "blob" in ent:
            off, n = ent["blob"]
            if off + n > len(tail):
                raise RuntimeError("%s：图片 %s 的 blob [%d, %d] 超出文件尾（%d 字节）"
                                   % (where, key, off, n, len(tail)))
            blobs[key] = tail[off:off + n]
        else:
            raise RuntimeError("%s：图片 %s 既没有 data 也没有 blob" % (where, key))
    return top, blobs


def walk(document):
    """按文件顺序产出 (所在顶层图层, 对象)；进组（含剪切组），不产出图层本身。"""
    def rec(layer, nodes):
        for n in nodes:
            yield layer, n
            kids = n.get("kind", {}).get("children")
            if kids and n["kind"].get("type") != "layer":
                yield from rec(layer, kids)
    for lay in document.get("layers", []):
        yield from rec(lay, lay["kind"].get("children", []))


def layer_by_name(document, name):
    """名字唯一的顶层图层；没有 → None，重名 → 报错（名字就是本 skill 的绑定）。"""
    hits = [l for l in document.get("layers", []) if l.get("name") == name]
    if len(hits) > 1:
        raise RuntimeError("文档里有 %d 个叫 %r 的图层，分不清用哪个" % (len(hits), name))
    return hits[0] if hits else None


def layer_flags(lay):
    """图层的 (visible, locked, printable)：前两个挂在节点上（缺省 True/False），printable 在 kind 里。"""
    return lay.get("visible", True), lay.get("locked", False), lay["kind"].get("printable", True)


def line_ends(node):
    """直线/路径对象的首末锚点 ((x0, y0), (x1, y1))。"""
    anchors = node["kind"]["path"]["subpaths"][0]["anchors"]
    return tuple(anchors[0]["p"]), tuple(anchors[-1]["p"])


def text_info(node):
    """文字对象 → {text, x, y, angle, font, size, color(#rrggbb), justify}；
    多个 run 时文字拼起来、样式取第一个。angle = 逆时针度（见 angle_of_xf）。"""
    k = node["kind"]
    st = k["runs"][0]["style"]
    return {"text": "".join(r["text"] for r in k["runs"]),
            "x": k["xf"][4], "y": k["xf"][5], "angle": angle_of_xf(k["xf"]),
            "font": st.get("font_family"), "size": st.get("size"),
            "color": _paint_hex(st.get("fill")),
            "justify": k.get("para", {}).get("justify")}


def rot_xf(x, y, angle):
    """绕 (x, y) 逆时针转 angle 度的 xf 矩阵 [a,b,c,d,e,f]（x' = a u + c v + e，y' = b u + d v + f）。

    实测（references/vectorcraft.md §1）：VectorCraft 把点文字的 xf 当普通仿射用，绕锚点转；
    [0,−1,1,0] 让字从下往上读（逆时针 90°），与控制口 object.rotate{angle} 的正方向一致。
    """
    t = math.radians(float(angle))
    c, s = math.cos(t), math.sin(t)
    return [c, -s, s, c, float(x), float(y)]


def angle_of_xf(xf):
    """xf 的线性部分 → 逆时针角度（度，(-180, 180]）。忽略缩放与切变：取阅读方向 (a, b)。"""
    a, b = float(xf[0]), float(xf[1])
    deg = math.degrees(math.atan2(-b, a))
    return round(((deg + 180.0) % 360.0) - 180.0, 6)



# ---------------------------------------------------------------- 写

def new_doc(title, template=TEMPLATE):
    """空文档（无画板、无图层），文档级字段来自模板。返回可变的 builder dict。"""
    tpl = _template(template)
    d = copy.deepcopy(tpl["document"])
    d.update(title=title, artboards=[], layers=[], guides=[], images={}, next_id=1)
    d.pop("last_view", None)
    d.pop("layers_open", None)
    top = {"format": "vectorcraft", "version": tpl["version"], "generator": tpl["generator"],
           "document": d, "images": {}}
    return {"top": top, "blobs": {}, "tpl": tpl, "next": 1}


def add_artboard(doc, name, x, y, w, h):
    """追加画板（pt）。返回画板 dict；第 i 个画板就是 document.export 的 artboard=i。"""
    ab = copy.deepcopy(doc["tpl"]["artboard"])
    ab.update(id=len(doc["top"]["document"]["artboards"]) + 1, name=name,
              rect={"x0": float(x), "y0": float(y), "x1": float(x + w), "y1": float(y + h)})
    doc["top"]["document"]["artboards"].append(ab)
    return ab


def add_layer(doc, name, printable=True, visible=True, locked=False, color=0):
    """追加顶层图层（列表越靠后越在上面）。返回图层节点。"""
    lay = copy.deepcopy(doc["tpl"]["layer"])
    lay["id"] = _id(doc)
    lay["name"] = name
    lay["kind"].update(children=[], printable=bool(printable), color={"Preset": int(color)})
    lay.pop("visible", None)
    lay.pop("locked", None)
    if not visible:
        lay["visible"] = False
    if locked:
        lay["locked"] = True
    doc["top"]["document"]["layers"].append(lay)
    return lay


def lay_children(lay):
    return lay["kind"]["children"]


def line(doc, name, a, b, rgb=(0.0, 0.0, 0.0), width=0.5):
    """两点直线（无填充，实线描边）。a/b = (x, y) pt；rgb 各 0–1。"""
    n = copy.deepcopy(doc["tpl"]["line"])
    n["id"] = _id(doc)
    n["name"] = name
    stroke = next(it for it in n["appearance"]["items"] if it["kind"] == "stroke")
    stroke["paint"] = {"type": "solid", "color": _rgb(rgb)}
    stroke["width"] = float(width)
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    n["kind"]["path"]["subpaths"] = [{"anchors": [{"p": [ax, ay]}, {"p": [bx, by]}], "closed": False}]
    n["kind"]["live"].update(a={"x": ax, "y": ay}, b={"x": bx, "y": by})
    return n


def polyline(doc, name, points, rgb=(0.0, 0.0, 0.0), width=0.5):
    """多点折线（开放、无填充、实线描边；没有 live 形状）。points = [(x, y), …] pt，至少 2 点。"""
    if len(points) < 2:
        raise RuntimeError("polyline 至少要 2 个点（收到 %d）" % len(points))
    n = copy.deepcopy(doc["tpl"]["polyline"])
    n["id"] = _id(doc)
    n["name"] = name
    for it in n["appearance"]["items"]:
        if it["kind"] == "fill":
            it["paint"] = {"type": "none"}
        elif it["kind"] == "stroke":
            it["paint"] = {"type": "solid", "color": _rgb(rgb)}
            it["width"] = float(width)
    n["kind"]["path"]["subpaths"] = [{"anchors": [{"p": [float(x), float(y)]} for x, y in points],
                                      "closed": False}]
    n["kind"].pop("live", None)
    return n


def text(doc, name, x, y, s, size=8.0, font="Arial", rgb=(0.0, 0.0, 0.0), justify="Center", angle=0.0):
    """点文字。(x, y) 是锚点：y 是基线，x 按 justify（Left/Center/Right）对齐；angle = 逆时针度，
    绕锚点转（90 = 从下往上读）。字号只写 size、从不缩放。"""
    if justify not in ("Left", "Center", "Right"):
        raise RuntimeError("text justify 只收 Left/Center/Right（收到 %r）" % (justify,))
    n = copy.deepcopy(doc["tpl"]["text"])
    n["id"] = _id(doc)
    n["name"] = name
    k = n["kind"]
    k["xf"] = rot_xf(x, y, angle)
    run = copy.deepcopy(k["runs"][0])
    run["text"] = s
    run["style"].update(font_family=font, font_style="Regular", size=float(size),
                        fill={"type": "solid", "color": _rgb(rgb)})
    k["runs"] = [run]
    k["para"]["justify"] = justify
    k["para"].pop("justify_auto", None)
    return n


def add_image(doc, data, mime="image/png"):
    """把图片字节放进文档（内嵌，不链接）；同一份字节只存一次。返回 (key, 宽 px, 高 px)。"""
    if mime != "image/png":
        raise RuntimeError("add_image 目前只收 PNG（收到 %s）" % mime)
    w, h = png_size(data)
    key = "img" + hashlib.sha1(data).hexdigest()[:16]
    doc["blobs"][key] = data
    doc["top"]["images"][key] = {"mime": mime}
    return key, w, h


def image(doc, name, key, w_px, h_px, x, y, pt_per_px):
    """放置一张已 add_image 的图：左上角 (x, y) pt，每像素 pt_per_px pt（等比）。"""
    if key not in doc["blobs"]:
        raise RuntimeError("image：%s 没有先 add_image" % key)
    s = float(pt_per_px)
    return {"id": _id(doc), "name": name,
            "kind": {"type": "image", "key": key, "width": int(w_px), "height": int(h_px),
                     "xf": [s, 0.0, 0.0, s, float(x), float(y)]}}


def clip(doc, name, rect, content):
    """剪切组：矩形 rect=(x, y, w, h) pt 只露出 content（一个对象节点）落在矩形里的部分。"""
    g = copy.deepcopy(doc["tpl"]["clip"])
    g["id"] = _id(doc)
    g["name"] = name
    path = next(c for c in g["kind"]["children"] if c["kind"].get("clipping"))
    path = copy.deepcopy(path)
    path["id"] = _id(doc)
    path.pop("name", None)
    x, y, w, h = (float(v) for v in rect)
    path["kind"]["path"]["subpaths"] = [{"anchors": [{"p": [x, y]}, {"p": [x + w, y]},
                                                     {"p": [x + w, y + h]}, {"p": [x, y + h]}],
                                         "closed": True}]
    path["kind"]["live"].update(w=w, h=h, xf=[1.0, 0.0, 0.0, 1.0, x, y])
    g["kind"]["children"] = [path, content]
    return g


def dumps(doc):
    """builder → 文件字节（inline 编码）。顺带校验 id 唯一、图片 key 都有字节。"""
    top = copy.deepcopy(doc["top"])
    ids = [n["id"] for _l, n in walk(top["document"])] + [l["id"] for l in top["document"]["layers"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("vc_file：对象 id 重复（%d 个里只有 %d 个不同）" % (len(ids), len(set(ids))))
    used = {n["kind"]["key"] for _l, n in walk(top["document"]) if n["kind"].get("type") == "image"}
    for key in used:
        if key not in doc["blobs"]:
            raise RuntimeError("vc_file：图片 %s 被引用但没有字节" % key)
    top["images"] = {k: {"mime": v["mime"], "data": base64.b64encode(doc["blobs"][k]).decode("ascii")}
                     for k, v in top["images"].items() if k in used}
    top["document"]["next_id"] = max(ids + [0]) + 1
    return (json.dumps(top, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def write(doc, path):
    """原子写：先写临时文件再 rename（app 正开着同名文件时不会读到半截）。"""
    data = dumps(doc)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- 内部

def png_size(data):
    """PNG 的 (宽, 高)，读 IHDR。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RuntimeError("不是 PNG（缺签名或 IHDR）")
    return struct.unpack(">II", data[16:24])


def _template(path):
    if path not in _tpl_cache:
        top, _blobs = read(path)
        d = top["document"]
        if not d.get("layers") or not d.get("artboards"):
            raise RuntimeError("模板 %s 缺图层或画板" % path)
        lay = d["layers"][0]
        kinds = {n.get("name"): n for n in lay["kind"]["children"]}
        missing = [k for k in _KIND_NAMES if k not in kinds]
        if missing:
            raise RuntimeError("模板 %s 缺对象 %s（见 references/vectorcraft.md §5）" % (path, missing))
        lay = copy.deepcopy(lay)
        lay["kind"]["children"] = []
        _tpl_cache[path] = {"version": top["version"], "generator": top["generator"],
                            "document": d, "layer": lay, "artboard": d["artboards"][0],
                            "clip": kinds["clip"], "line": kinds["line"], "text": kinds["text"],
                            "polyline": kinds["polyline"]}
    return _tpl_cache[path]


def _id(doc):
    i = doc["next"]
    doc["next"] += 1
    return i


def _rgb(rgb):
    r, g, b = (float(v) for v in rgb)
    for v in (r, g, b):
        if not 0.0 <= v <= 1.0:
            raise RuntimeError("颜色分量要在 0–1（收到 %r）" % (rgb,))
    return {"model": "rgb", "r": r, "g": g, "b": b}


def _paint_hex(paint):
    """solid rgb/gray 填色 → #rrggbb；其它（none、cmyk、渐变）→ None。"""
    if not paint or paint.get("type") != "solid":
        return None
    c = paint["color"]
    if c.get("model") == "rgb":
        vals = (c["r"], c["g"], c["b"])
    elif c.get("model") == "gray":
        vals = (1.0 - c["k"],) * 3
    else:
        return None
    return "#%02x%02x%02x" % tuple(int(round(max(0.0, min(1.0, v)) * 255)) for v in vals)
