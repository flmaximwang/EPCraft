#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""label_gel.py — 给 SDS-PAGE 胶图的 VectorCraft 工程自动加标签。

三个子命令：

  label              读标记位置 → 写文字 → 另存工程 → 导出 PNG 并裁掉四周白边
  generate_template  生成 markers.json / lanes.json 模版
  selftest           不连 VectorCraft，自检 PNG 解码/裁边/编码

在 VectorCraft GUI 里标好位置 → 跑 label → 自动在每个 marker 条带旁写分子量、
在每个泳道正上方写泳道名。文字的字号 / 内容 / 颜色 / 对齐全部由两个 JSON 控制。

标记约定（默认 --marks guides，在 GUI 里拖参考线即可）：
  · 水平参考线 = 一个 marker 条带的高度位置（从上到下 = markers.json 里 labels 的顺序）
  · 垂直参考线 = 一条泳道的中心（从左到右 = lanes.json 里 labels 的顺序）
    ⚠️ marker 泳道也要拖一条，并在 lanes.json 里给它一个标签（不想写字就留空 ""）
  · 也可以用图形标记：--marks layer:标记图层名
    该图层里 竖长的小图形 = 泳道中心，横长的小图形 = marker 条带高度

用法：
  python3 label_gel.py generate_template --lanes 8       # 先出两个配置模版，改成自己的
  python3 label_gel.py label gel.vectorcraft             # 用同目录的 markers.json / lanes.json
  python3 label_gel.py label gel.vectorcraft --markers m.json --lanes l.json --out out.vectorcraft
  python3 label_gel.py label gel.vectorcraft --dry-run   # 只算位置，不写字、不导出
  python3 label_gel.py label gel.vectorcraft --no-preview  # 不导出预览 PNG
  python3 label_gel.py selftest

输出：默认另存为 <名字>_labeled.vectorcraft（原文件不动），并导出同名 .png（已裁掉四周白边）。
      文字分两个图层写：分子量标签进 “marker labels (auto)”，泳道名进 “lane labels (auto)”，
      胶图留在它原来的图层 —— 三类东西各占一层，可以单独显示/隐藏/导出。
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

DEFAULT_APP = "/Applications/VectorCraft.app/Contents/MacOS/VectorCraft"
DEFAULT_PORT = 7981
LABEL_LAYERS = ("marker labels (auto)", "lane labels (auto)")   # marker / 泳道名各占一个图层
BG_LEVEL = 250            # 每个通道都 ≥ 这个值 = 白底（裁边只看这一条）


# --------------------------------------------------------------------------- #
# 控制通道（VectorCraft 的 JSON over TCP 控制口）
# --------------------------------------------------------------------------- #
class VC:
    def __init__(self, port=DEFAULT_PORT, app=DEFAULT_APP, launch=True, verbose=False):
        self.port, self.app, self.proc, self.verbose = port, app, None, verbose
        try:
            self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            if verbose:
                print(f"[i] 复用已在监听 {port} 的 VectorCraft")
        except OSError:
            if not launch:
                sys.exit(f"连不上 127.0.0.1:{port}，请先启动：{app} --control {port}")
            if not os.path.exists(app):
                sys.exit(f"找不到 VectorCraft：{app}（用 --app 指定路径）")
            print(f"[i] 启动 VectorCraft（控制口 {port}）…")
            self.proc = subprocess.Popen(
                [app, "--control", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.time() + 90
            while True:
                try:
                    self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                    break
                except OSError:
                    if time.time() > deadline:
                        sys.exit("VectorCraft 启动超时（控制口没开起来）")
                    time.sleep(0.5)
        self.sock.settimeout(120)
        self._id = 0

    def call(self, method, params=None):
        self._id += 1
        req = json.dumps({"id": self._id, "method": method, "params": params or {}})
        self.sock.sendall((req + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise RuntimeError("控制连接被关闭")
            buf += chunk
        rep = json.loads(buf.decode("utf-8"))
        if not rep.get("ok"):
            raise RuntimeError(f"{method} 失败：{rep.get('error')}")
        return rep.get("result")

    def ex(self, command, params=None):
        """跑一个 engine 命令（菜单/工具底层命令）。"""
        return self.call("engine.execute", {"command": command, "params": params or {}})

    def close(self, quit_app=True):
        try:
            if quit_app and self.proc is not None:      # 只关我们自己起的实例
                try:
                    self.call("app.quit", {})
                except Exception:
                    pass
            self.sock.close()
        except Exception:
            pass
        if quit_app and self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.terminate()


def ui(vc):
    return vc.call("ui.inspect", {})


def doc(vc, depth=2, child_limit=500):
    return vc.call("document.inspect", {"depth": depth, "childLimit": child_limit})


def dismiss_dialogs(vc, tries=6):
    """打开文件后可能弹 missingLinks 之类对话框，取消掉，别卡住。"""
    for _ in range(tries):
        d = (ui(vc).get("ui") or {}).get("dialog")
        if not d:
            return
        print(f"[i] 关掉对话框 {d.get('kind')}")
        vc.call("ui.dialog.cancel", {})


def walk(node):
    """document.inspect 的层/组/对象树。"""
    yield node
    for child in (node.get("children") or []):
        yield from walk(child)


def is_image(node):
    return str(node.get("kind", "")).lower() in ("image", "placedimage")


def selection_bounds(vc):
    return doc(vc).get("selectionBounds")


def create_text(vc, x, y, entry, defaults):
    """写一条文字，返回它的 id。entry 可以是字符串或 {"text","size","color","font"}。"""
    if isinstance(entry, str):
        entry = {"text": entry}
    text = str(entry.get("text", ""))
    size = entry.get("size", defaults.get("size", 16))
    color = entry.get("color", defaults.get("color", "#000000"))
    font = entry.get("font", defaults.get("font"))
    params = {"x": x, "y": y, "text": text, "size": size, "color": color}
    if font:
        params["font"] = font
    return vc.ex("text.create", params)["id"]


def style_and_place(vc, oid, justify, target):
    """设置对齐，然后把文字挪到位：target = ("center_y", y) 或 ("bottom_y", y)。"""
    vc.ex("text.setStyle", {"id": oid, "justify": justify})
    b = selection_bounds(vc)
    if not b:
        raise RuntimeError("拿不到新建文字的包围盒")
    if target[0] == "center_y":
        dy = target[1] - (b["y"] + b["height"] / 2.0)
    else:  # bottom_y
        dy = target[1] - (b["y"] + b["height"])
    if abs(dy) > 1e-6:
        vc.ex("object.move", {"dx": 0.0, "dy": dy})
        b = selection_bounds(vc) or b
    return b


# --------------------------------------------------------------------------- #
# 读取标记
# --------------------------------------------------------------------------- #
def lane_pitch(lane_x):
    """泳道带宽的估计 = 相邻泳道中心距的中位数。

    不要用「胶图宽 ÷ 泳道数」：胶图对象往往比泳道区宽（照片自带白边/背景），
    那个估计会把带宽放大一倍以上，于是标签离胶图老远、gap 看起来像没生效。
    用中位数而不是平均值，是为了让一条拖歪的参考线不要把带宽带跑。
    """
    if len(lane_x) < 2:
        return 0.0
    gaps = sorted(b - a for a, b in zip(lane_x, lane_x[1:]))
    return gaps[len(gaps) // 2]


def positions_from_guides(vc):
    guides = vc.ex("guide.list", {}) or []
    lanes = sorted((g["pos"] for g in guides if g.get("vertical")), key=float)
    bands = sorted((g["pos"] for g in guides if not g.get("vertical")), key=float)
    return lanes, bands


def positions_from_layer(vc, layer_name):
    d = doc(vc, depth=3)
    marks = []
    for layer in d.get("layers", []):
        if layer.get("name") != layer_name:
            continue
        for n in walk(layer):
            if n is layer or n.get("kind") == "Layer":
                continue
            if is_image(n) or str(n.get("kind", "")).lower() == "type":
                continue
            b = n.get("bounds")
            if not b:
                continue
            marks.append(b)
    if not marks:
        sys.exit(f"图层 “{layer_name}” 里没找到标记图形")
    lanes, bands = [], []
    for b in marks:
        cx, cy = b["x"] + b["width"] / 2.0, b["y"] + b["height"] / 2.0
        (lanes if b["height"] >= b["width"] else bands).append(cx if b["height"] >= b["width"] else cy)
    return sorted(lanes), sorted(bands)


def image_rect(vc):
    d = doc(vc, depth=3)
    imgs = [n for layer in d.get("layers", []) for n in walk(layer)
            if is_image(n) and n.get("bounds") and n.get("visible", True)]
    if not imgs:
        sys.exit("文档里没找到图片对象（胶图）")
    b = max(imgs, key=lambda n: n["bounds"]["width"] * n["bounds"]["height"])["bounds"]
    return {"left": b["x"], "top": b["y"], "right": b["x"] + b["width"], "bottom": b["y"] + b["height"],
            "width": b["width"], "height": b["height"]}


# --------------------------------------------------------------------------- #
# 画布准备
# --------------------------------------------------------------------------- #
def ensure_layer(vc, name, clean=True):
    d = doc(vc, depth=1)
    target = next((l for l in d.get("layers", []) if l.get("name") == name), None)
    if target is None:
        target_id = vc.ex("layer.new", {"name": name, "top": True})["id"]
    else:
        target_id = target["id"]
    # 让新文字确实落进这个图层：currentLayer 才是新图形的去处。
    # 刚打开文档时图层行状态还没建好，setCurrent 可能不生效，所以重试 + 校验。
    vc.ex("window.panel", {"panel": "layers"})
    cur = None
    for attempt in range(10):
        vc.ex("layer.setCurrent", {"id": target_id})
        cur = doc(vc, depth=1).get("currentLayer")
        if cur == target_id:
            break
        vc.ex("layer.highlight", {"ids": [target_id], "mode": "set"})
        cur = doc(vc, depth=1).get("currentLayer")
        if cur == target_id:
            break
        time.sleep(0.3)
    if cur != target_id:
        sys.exit(f"没法把 “{name}” 设为当前图层（当前是 {cur}）")
    if clean:
        old = []
        for layer in doc(vc, depth=3).get("layers", []):
            if layer.get("name") == name:
                old += [n["id"] for n in walk(layer) if str(n.get("kind", "")).lower() == "type"]
        if old:
            vc.ex("select.set", {"ids": old})
            vc.ex("edit.clear", {})
    return target_id


def texts_in_layer(vc, layer_name):
    """这个图层里的文字对象（含子层）。"""
    out = []
    for layer in doc(vc, depth=3).get("layers", []):
        if layer.get("name") == layer_name:
            out += [n for n in walk(layer) if str(n.get("kind", "")).lower() == "type"]
    return out


def clear_text_in_layer(vc, layer_name):
    ids = []
    for layer in doc(vc, depth=3).get("layers", []):
        if layer.get("name") == layer_name:
            ids += [n["id"] for n in walk(layer) if str(n.get("kind", "")).lower() == "type"]
    if ids:
        vc.ex("select.set", {"ids": ids})
        vc.ex("edit.clear", {})
    return len(ids)


# --------------------------------------------------------------------------- #
# PNG：解码 / 裁白边 / 编码（纯 stdlib；VectorCraft 导出的是 8-bit 非隔行 PNG）
# --------------------------------------------------------------------------- #
class TrimError(Exception):
    pass


def png_decode(path):
    """→ (w, h, channels, pixels)。只吃 8-bit RGB/RGBA、非隔行；其它形状抛 TrimError。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TrimError("不是 PNG")
    pos, idat = 8, []
    w = h = ch = stride = None
    while pos + 12 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _cm, _fm, il = struct.unpack(">IIBBBBB", body)
            if bd != 8 or il != 0 or ct not in (2, 6):
                raise TrimError(f"不支持的 PNG 形状（bitdepth={bd} colortype={ct} interlace={il}）")
            ch = 3 if ct == 2 else 4
            stride = w * ch
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if w is None:
        raise TrimError("没有 IHDR")
    raw = zlib.decompress(b"".join(idat))
    if len(raw) != h * (stride + 1):
        raise TrimError("PNG 数据长度与 IHDR 不符")
    px = bytearray(w * h * ch)
    prev = bytearray(stride)
    p = 0
    # ponytail: 逐字节解滤波，2 MP 的导出图约 1 s（线性）；要更快再上 numpy
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b, c = prev[i], (prev[i - ch] if i >= ch else 0)
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif f != 0:
            raise TrimError(f"PNG 行滤波器 {f} 不认识")
        px[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, ch, px


def png_encode(path, w, h, ch, px):
    """写 8-bit RGB/RGBA PNG（每行 filter 0，够用且最小实现）。"""
    ct = 2 if ch == 3 else 6
    stride = w * ch
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                + chunk(b"IEND", b""))


def trim_png(path, level=BG_LEVEL):
    """把 PNG 四周的白底裁掉，**原地覆盖**。返回 ((w,h), (裁剪后 w,h))。"""
    w, h, ch, px = png_decode(path)
    stride = w * ch
    # 带 alpha（透明底导出）的图看 alpha>0（半透明也算内容）；不带 alpha 的看三通道有没有 < level
    alphas = (ch == 4)
    tbl = (bytes(1 if v > 0 else 0 for v in range(256)) if alphas
           else bytes(1 if v < level else 0 for v in range(256)))     # 1 = 非背景
    x0, x1, y0, y1 = w, -1, h, -1
    for y in range(h):
        row = px[y * stride:(y + 1) * stride]
        t = bytes(row[3::4]).translate(tbl) if alphas else bytes(row).translate(tbl)
        i = t.find(b"\x01")
        if i < 0:
            continue
        j = t.rfind(b"\x01")
        k = 1 if alphas else ch        # alphas 时扫描串是 1 字节/像素，行内下标即列号
        i //= k
        j //= k
        y1 = y
        if y < y0:
            y0 = y
        if i < x0:
            x0 = i
        if j > x1:
            x1 = j
    if x1 < 0:
        raise TrimError("整张图都是背景色，没有可裁的内容（工程里是不是没有可见对象？）")
    cw, chh = x1 - x0 + 1, y1 - y0 + 1
    if (cw, chh) == (w, h):
        return (w, h), (w, h)
    crop = bytearray()
    for y in range(y0, y1 + 1):
        crop += px[y * stride + x0 * ch: y * stride + (x1 + 1) * ch]
    png_encode(path, cw, chh, ch, crop)
    return (w, h), (cw, chh)


# --------------------------------------------------------------------------- #
# 两个 JSON
# --------------------------------------------------------------------------- #
MARKER_LADDER = ["250 kD", "150 kD", "100 kD", "70 kD", "50 kD", "40 kD",
                 "35 kD", "25 kD", "20 kD", "15 kD", "10 kD"]

MARKER_NOTE = ("marker 尺寸标签：labels 从上到下的顺序 = 工程里水平参考线从上到下的顺序。"
               "size/color/font 是默认值，单条可以覆盖。marker_lane = marker 泳道是左起第几条竖参考线"
               "（0 = 第 1 条左边一条泳道带处）。gap = 文字与泳道带边缘的水平间距 (pt)。"
               "lane_width 一般不用给：脚本按相邻泳道中心距自动算泳道带宽。")
LANE_NOTE = ("泳道名：labels 从左到右的顺序 = 工程里竖直参考线从左到右的顺序，每条泳道一条"
             "（含 marker 泳道）。text 留空 \"\" 就不写。gap = 文字底边离胶图顶边的距离 (pt)。")


def marker_template():
    return {"_说明": MARKER_NOTE, "font": "Source Sans 3", "size": 16, "color": "#000000",
            "gap": 4, "marker_lane": 1,
            "labels": [{"text": t} for t in MARKER_LADDER]}


def lane_template(n):
    labels = [{"text": "M"}] + [{"text": f"Sample {i}"} for i in range(1, n)]
    return {"_说明": LANE_NOTE, "font": "Source Sans 3", "size": 16, "color": "#000000",
            "gap": 0, "align": "center", "labels": labels}


def load_cfg(path, what):
    if not os.path.exists(path):
        sys.exit(f"找不到{what}：{path}\n"
                 f"（用 `label_gel.py generate_template` 生成模版，或用 --markers/--lanes 指到你自己的配置）")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    labels = cfg.get("labels") or []
    if not labels:
        sys.exit(f"{path} 里没有 labels")
    return cfg, labels


# --------------------------------------------------------------------------- #
# 子命令：generate_template
# --------------------------------------------------------------------------- #
def cmd_generate_template(args):
    folder = os.path.abspath(os.path.expanduser(args.dir))
    os.makedirs(folder, exist_ok=True)
    todo = [(os.path.join(folder, args.markers_name), marker_template()),
            (os.path.join(folder, args.lanes_name), lane_template(max(2, args.lanes)))]
    for path, _ in todo:                       # 先全查再写，别写了一个才失败
        if os.path.exists(path) and not args.force:
            sys.exit(f"已存在 {path} —— 不覆盖（要覆盖加 --force）")
    for path, cfg in todo:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        with open(path, "r", encoding="utf-8") as f:    # 写完立刻回读，确认是合法 JSON
            back = json.load(f)
        print(f"[✓] {path}（{len(back['labels'])} 条 labels）")
    print(f"[i] 改完这两个文件，再在 GUI 里拖参考线、跑 "
          f"`label_gel.py label <工程> --markers {args.markers_name} --lanes {args.lanes_name}`")


# --------------------------------------------------------------------------- #
# 子命令：selftest（不连 VectorCraft）
# --------------------------------------------------------------------------- #
def _png_wrap(w, h, raw, ct=2):
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _png_wrap_filtered(w, h, rows, ch=3):
    """按行轮流用 5 种滤波器打包 —— 解码器的对照实现（只给 selftest 用）。"""
    raw = bytearray()
    prev = bytes(w * ch)
    for y, row in enumerate(rows):
        f = y % 5
        line = bytearray(row)
        for i in range(len(line)):
            a = row[i - ch] if i >= ch else 0
            b = prev[i]
            c = prev[i - ch] if i >= ch else 0
            if f == 1:
                pr = a
            elif f == 2:
                pr = b
            elif f == 3:
                pr = (a + b) >> 1
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            else:
                pr = 0
            line[i] = (line[i] - pr) & 255
        raw.append(f)
        raw += line
        prev = row
    return bytes(raw)


def cmd_selftest(args):
    tmp = tempfile.mkdtemp(prefix="label_gel_selftest_")
    checks = []

    def ok(name, cond, extra=""):
        checks.append((name, bool(cond), extra))
        print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))

    # 1. 40×20：白底 + 位于 x=7..14 / y=6..10 的 8×5 色块；每行换一种滤波器
    W, H = 40, 20
    bx0, bx1, by0, by1 = 7, 14, 6, 10
    rows = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            row += bytes((10, 20, 30)) if (bx0 <= x <= bx1 and by0 <= y <= by1) else bytes((255, 255, 255))
        rows.append(bytes(row))
    p1 = os.path.join(tmp, "rgb.png")
    with open(p1, "wb") as f:
        f.write(_png_wrap(W, H, _png_wrap_filtered(W, H, rows)))
    w, h, ch, px = png_decode(p1)
    ok("解码尺寸/通道", (w, h, ch) == (W, H, 3), f"{w}x{h} ch={ch}")
    ok("5 种滤波器全部还原", bytes(px) == b"".join(rows))
    before, after = trim_png(p1)
    ok("裁边尺寸", after == (bx1 - bx0 + 1, by1 - by0 + 1), f"{before} → {after}")
    w2, h2, ch2, px2 = png_decode(p1)
    ok("裁完的图 = 色块本身", (w2, h2) == after
       and bytes(px2) == b"".join(rows[y][bx0 * 3:(bx1 + 1) * 3] for y in range(by0, by1 + 1)))

    # 2. 透明底（RGBA）：margin 全透明 —— 裁边只看 alpha
    rows4 = []
    for y in range(H):
        row = bytearray()
        for x in range(W):
            inside = bx0 <= x <= bx1 and by0 <= y <= by1
            row += bytes((10, 20, 30, 255)) if inside else bytes((0, 0, 0, 0))
        rows4.append(bytes(row))
    p2 = os.path.join(tmp, "rgba.png")
    with open(p2, "wb") as f:
        f.write(_png_wrap(W, H, _png_wrap_filtered(W, H, rows4, ch=4), ct=6))
    before2, after2 = trim_png(p2)
    ok("透明底按 alpha 裁边", after2 == (bx1 - bx0 + 1, by1 - by0 + 1), f"{before2} → {after2}")

    # 3. 全白图必须报错，而不是写出 0 尺寸
    p3 = os.path.join(tmp, "white.png")
    png_encode(p3, 4, 4, 3, bytearray(b"\xff\xff\xff" * 16))
    try:
        trim_png(p3)
        ok("全白图报错", False, "居然裁成功了")
    except TrimError:
        ok("全白图报错", True)

    # 4. 泳道带宽：中位数相邻间距，别被一条歪参考线带跑
    ok("lane_pitch 中位数", lane_pitch([0, 10, 20, 30]) == 10)
    ok("lane_pitch 抗歪一条", lane_pitch([0, 10, 20, 30, 200]) == 10, str(lane_pitch([0, 10, 20, 30, 200])))
    ok("lane_pitch 单泳道 = 0", lane_pitch([5]) == 0)

    # 5. 编码器自回环
    p4 = os.path.join(tmp, "roundtrip.png")
    png_encode(p4, 3, 2, 3, bytearray(b"\x01\x02\x03" * 6))
    w4, h4, ch4, px4 = png_decode(p4)
    ok("encode→decode 回环", (w4, h4, ch4) == (3, 2, 3) and bytes(px4) == b"\x01\x02\x03" * 6)

    bad = [n for n, passed, _ in checks if not passed]
    print(f"[{'✓' if not bad else '✗'}] selftest：{len(checks) - len(bad)}/{len(checks)} 通过")
    if bad:
        sys.exit(f"失败：{', '.join(bad)}")


# --------------------------------------------------------------------------- #
# 子命令：label
# --------------------------------------------------------------------------- #
def cmd_label(args):
    project = os.path.abspath(os.path.expanduser(args.project))
    if not os.path.exists(project):
        sys.exit(f"找不到工程文件：{project}")
    folder, stem = os.path.dirname(project), os.path.splitext(os.path.basename(project))[0]
    markers_path = args.markers or os.path.join(folder, "markers.json")
    lanes_path = args.lanes or os.path.join(folder, "lanes.json")
    out_path = args.out or os.path.join(folder, f"{stem}_labeled.vectorcraft")

    m_cfg, m_labels = load_cfg(markers_path, "marker 配置")
    l_cfg, l_labels = load_cfg(lanes_path, "泳道配置")

    vc = VC(port=args.port, app=args.app, verbose=True)
    try:
        print(f"[i] 打开 {project}")
        opened = vc.ex("file.open", {"path": project})
        if opened and opened.get("missingLinks"):
            print(f"[!] 有 {len(opened['missingLinks'])} 个链接文件找不到（图是内嵌的，不影响）")
        dismiss_dialogs(vc)

        if args.clear_text_in:
            n = clear_text_in_layer(vc, args.clear_text_in)
            print(f"[i] 清掉图层 “{args.clear_text_in}” 里的 {n} 条旧文字")

        # 1. 读标记
        if args.marks.startswith("layer:"):
            lane_x, band_y = positions_from_layer(vc, args.marks.split(":", 1)[1])
        else:
            lane_x, band_y = positions_from_guides(vc)

        print(f"[i] 读到 {len(lane_x)} 条泳道标记，{len(band_y)} 条 marker 标记")
        if len(lane_x) != len(l_labels):
            sys.exit(f"泳道标记 {len(lane_x)} 条 ≠ lanes.json 里 {len(l_labels)} 个标签 —— "
                     f"检查是不是每条泳道（含 marker 泳道）都拖了竖直参考线")
        if len(band_y) != len(m_labels):
            sys.exit(f"marker 标记 {len(band_y)} 条 ≠ markers.json 里 {len(m_labels)} 个标签 —— "
                     f"每个条带都要一条水平参考线")

        img = image_rect(vc)
        gap_m = float(m_cfg.get("gap", 4))
        gap_l = float(l_cfg.get("gap", 0))
        align_l = l_cfg.get("align", "center")
        # 泳道带宽：优先用配置里的 lane_width，否则按相邻泳道中心距算（见 lane_pitch 的注释）
        lane_w = float(m_cfg["lane_width"]) if m_cfg.get("lane_width") else lane_pitch(lane_x)

        # marker 泳道
        if m_cfg.get("marker_lane_x") is not None:
            mx = float(m_cfg["marker_lane_x"])
        else:
            idx = int(m_cfg.get("marker_lane", 1))
            if idx == 0:                       # 0 = 第 1 条泳道左边一条泳道带处
                mx = lane_x[0] - lane_w
            elif 1 <= idx <= len(lane_x):
                mx = lane_x[idx - 1]
            else:
                sys.exit("markers.json 的 marker_lane 超出泳道数量")
        # 左右半边按**泳道**的中线判，不要按胶图对象的中线判：
        # 胶图对象常常比泳道区宽（自带白边），按它判会把左边的泳道误判成右半边。
        mid = (min(lane_x) + max(lane_x)) / 2.0
        left_side = mx < mid
        m_justify = "right" if left_side else "left"
        edge = mx if m_cfg.get("lane_edge") == "inner" else (
            mx - lane_w / 2.0 if left_side else mx + lane_w / 2.0)
        m_x = edge - gap_m if left_side else edge + gap_m
        l_y = img["top"] - gap_l

        print(f"[i] 胶图 {img['width']:.1f}×{img['height']:.1f} pt，左边缘 x={img['left']:.1f}，"
              f"顶边 y={img['top']:.1f}")
        print(f"[i] 泳道带宽 ≈{lane_w:.1f} pt（"
              + ("来自 markers.json 的 lane_width" if m_cfg.get("lane_width")
                 else "相邻泳道中心距的中位数") + "）")
        print(f"[i] marker 泳道 x≈{mx:.1f}（泳道区中线 {mid:.1f} 的{'左' if left_side else '右'}边）"
              f"→ 泳道带边 x≈{edge:.1f} → {m_justify} 对齐，锚点 x={m_x:.1f}（离带边 {gap_m:g} pt）")
        print(f"[i] 泳道名：底边 y={l_y:.1f}，{align_l} 对齐")

        if args.dry_run:
            for y, e in zip(band_y, m_labels):
                t = e if isinstance(e, str) else e.get("text")
                print(f"    marker {t!r:12} y={y:.2f} x={m_x:.2f} ({m_justify})")
            for x, e in zip(lane_x, l_labels):
                t = e if isinstance(e, str) else e.get("text")
                print(f"    泳道   {t!r:12} x={x:.2f} y底={l_y:.2f}")
            return

        # 2. 写文字：marker 标签、泳道名各写各的图层
        ensure_layer(vc, args.marker_layer, clean=True)
        print(f"[i] 分子量标签写入图层 “{args.marker_layer}”")
        for y, entry in zip(band_y, m_labels):
            oid = create_text(vc, m_x, y, entry, m_cfg)
            style_and_place(vc, oid, m_justify, ("center_y", y))
        n_marker = len(texts_in_layer(vc, args.marker_layer))
        if n_marker != len(m_labels):
            sys.exit(f"“{args.marker_layer}” 里只有 {n_marker} 条分子量标签，应该是 {len(m_labels)} 条")

        ensure_layer(vc, args.lane_layer, clean=True)
        print(f"[i] 泳道名写入图层 “{args.lane_layer}”")
        n_lane_want = 0
        for x, entry in zip(lane_x, l_labels):
            if isinstance(entry, str):
                entry = {"text": entry}
            if not str(entry.get("text", "")).strip():
                continue                      # 留空 = 这个泳道不写字（比如 marker 泳道）
            oid = create_text(vc, x, l_y, entry, l_cfg)
            style_and_place(vc, oid, align_l, ("bottom_y", l_y))
            n_lane_want += 1
        n_lane = len(texts_in_layer(vc, args.lane_layer))
        if n_lane != n_lane_want:
            sys.exit(f"“{args.lane_layer}” 里只有 {n_lane} 条泳道名，应该是 {n_lane_want} 条")
        print(f"[i] 校验通过：{n_marker} 条分子量 + {n_lane} 条泳道名")

        # 图层布局：胶图 / marker 标签 / 泳道名 分在不同图层
        print("[i] 图层布局：")
        for layer in doc(vc, depth=3).get("layers", []):
            kids = list(walk(layer))[1:]
            kinds = {}
            for n in kids:
                k = str(n.get("kind", "?")).lower()
                kinds[k] = kinds.get(k, 0) + 1
            if kinds:
                print(f"    “{layer.get('name')}”: " +
                      ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())))
        img_layers = [l.get("name") for l in doc(vc, depth=3).get("layers", [])
                      if any(is_image(n) for n in walk(l))]
        for name in (args.marker_layer, args.lane_layer):
            if name in img_layers:
                sys.exit(f"图层 “{name}” 里还有胶图，换个图层名（胶图在：{', '.join(img_layers)}）")
        print(f"[i] 胶图在图层：{', '.join(img_layers) or '?'}")

        # 3. 另存 + 导出（导出默认开；导出后裁掉四周白边）
        print(f"[i] 另存为 {out_path}")
        vc.ex("file.saveAs", {"path": out_path})
        deadline = time.time() + 60
        while time.time() < deadline:          # 保存是后台任务，等文件落地
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                break
            time.sleep(0.3)
        else:
            sys.exit("保存超时")
        print(f"[✓] 工程：{out_path}（{os.path.getsize(out_path)} 字节）")

        if args.no_preview:
            print("[i] 按 --no-preview 跳过导出")
            return
        pv = os.path.abspath(os.path.expanduser(args.preview or os.path.splitext(out_path)[0] + ".png"))
        vc.ex("file.exportAs", {"path": pv, "format": "png", "scale": 2,
                                "background": args.preview_background})
        deadline = time.time() + 60
        while time.time() < deadline:
            if os.path.exists(pv) and os.path.getsize(pv) > 0:
                break
            time.sleep(0.3)
        else:
            sys.exit("导出预览超时")
        print(f"[✓] 预览：{pv}（{os.path.getsize(pv)} 字节）")
        try:
            before, after = trim_png(pv)
            print(f"[✓] 裁白边：{before[0]}×{before[1]} → {after[0]}×{after[1]} px")
        except TrimError as e:
            print(f"[!] 没裁白边：{e}（工程已存好，预览图是整页）")
    finally:
        vc.close(quit_app=not args.keep_open)


# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(prog="label_gel.py", description="SDS-PAGE 胶图标记工具（走 VectorCraft 控制口）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("label", help="读标记 → 写文字 → 另存工程 → 导出 PNG 并裁白边")
    lp.add_argument("project", help="标好位置的 .vectorcraft 工程文件")
    lp.add_argument("--markers", default=None, help="marker 配置 JSON（默认与工程同目录的 markers.json）")
    lp.add_argument("--lanes", default=None, help="泳道配置 JSON（默认与工程同目录的 lanes.json）")
    lp.add_argument("--out", default=None, help="输出工程文件（默认 <名字>_labeled.vectorcraft）")
    lp.add_argument("--preview", default=None, help="预览 PNG 路径（默认 <输出名>.png）")
    lp.add_argument("--no-preview", action="store_true", help="不导出预览 PNG（默认导出并裁白边）")
    lp.add_argument("--preview-background", default="white",
                    help="预览图背景：white（默认）/ transparent / black / #rrggbb")
    lp.add_argument("--marks", default="guides", help="标记来源：guides（参考线，默认）或 layer:图层名")
    lp.add_argument("--marker-layer", default=LABEL_LAYERS[0],
                    help=f"分子量标签放哪个图层（默认 {LABEL_LAYERS[0]}）")
    lp.add_argument("--lane-layer", default=LABEL_LAYERS[1],
                    help=f"泳道名放哪个图层（默认 {LABEL_LAYERS[1]}）")
    lp.add_argument("--clear-text-in", default=None, help="先删掉这个图层里已有的文字（清掉老的手打标签）")
    lp.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"控制口（默认 {DEFAULT_PORT}）")
    lp.add_argument("--app", default=DEFAULT_APP, help="VectorCraft 可执行文件路径")
    lp.add_argument("--keep-open", action="store_true", help="跑完不关掉脚本自己启动的 VectorCraft")
    lp.add_argument("--dry-run", action="store_true", help="只打印将要写的内容和位置，不改文件、不导出")
    lp.set_defaults(func=cmd_label)

    gp = sub.add_parser("generate_template", help="生成 markers.json / lanes.json 模版")
    gp.add_argument("--dir", default=".", help="写到哪个目录（默认当前目录）")
    gp.add_argument("--lanes", type=int, default=8, help="泳道条数（默认 8：M + Sample 1…N-1）")
    gp.add_argument("--markers-name", default="markers.json", help="marker 模版文件名")
    gp.add_argument("--lanes-name", default="lanes.json", help="泳道模版文件名")
    gp.add_argument("--force", action="store_true", help="已存在也覆盖（默认不覆盖）")
    gp.set_defaults(func=cmd_generate_template)

    sp = sub.add_parser("selftest", help="自检 PNG 解码/裁边/编码与泳道带宽（不连 VectorCraft）")
    sp.set_defaults(func=cmd_selftest)
    return ap


def main():
    if len(sys.argv) == 1:
        build_parser().print_help()
        sys.exit(2)
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
