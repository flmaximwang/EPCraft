#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""label_gel.py — 给 SDS-PAGE 胶图的 VectorCraft 工程自动加标签。

你在 VectorCraft GUI 里标好位置 → 跑这个脚本 → 自动在每个 marker 条带旁写分子量、
在每个泳道正上方写泳道名。文字的字号 / 内容 / 颜色 / 对齐全部由两个 JSON 控制。

标记约定（默认 --marks guides，在 GUI 里拖参考线即可）：
  · 水平参考线 = 一个 marker 条带的高度位置（从上到下 = markers.json 里 labels 的顺序）
  · 垂直参考线 = 一条泳道的中心（从左到右 = lanes.json 里 labels 的顺序）
    ⚠️ marker 泳道也要拖一条，并在 lanes.json 里给它一个标签（不想写字就留空 ""）
  · 也可以用图形标记：--marks layer:标记图层名
    该图层里 竖长的小图形 = 泳道中心，横长的小图形 = marker 条带高度

用法：
  python3 label_gel.py gel.vectorcraft                       # 用同目录的 markers.json / lanes.json
  python3 label_gel.py gel.vectorcraft --markers m.json --lanes l.json --preview out.png
  python3 label_gel.py gel.vectorcraft --dry-run              # 只算位置，不写字
  python3 label_gel.py gel.vectorcraft --marks layer:marks    # 用图层里的短线标记

输出：默认另存为 <名字>_labeled.vectorcraft，原文件不动。
      文字分两个图层写：分子量标签进 “marker labels (auto)”，泳道名进 “lane labels (auto)”，
      胶图留在它原来的图层 —— 三类东西各占一层，可以单独显示/隐藏/导出。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time

DEFAULT_APP = "/Applications/VectorCraft.app/Contents/MacOS/VectorCraft"
DEFAULT_PORT = 7981
LABEL_LAYERS = ("marker labels (auto)", "lane labels (auto)")   # marker / 泳道名各占一个图层


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
    """写一条文字，返回它的 bounds。entry 可以是字符串或 {"text","size","color","font"}。"""
    if isinstance(entry, str):
        entry = {"text": entry}
    text = str(entry.get("text", ""))
    size = entry.get("size", defaults.get("size", 16))
    color = entry.get("color", defaults.get("color", "#000000"))
    font = entry.get("font", defaults.get("font"))
    params = {"x": x, "y": y, "text": text, "size": size, "color": color}
    if font:
        params["font"] = font
    oid = vc.ex("text.create", params)["id"]
    return oid


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
# 主流程
# --------------------------------------------------------------------------- #
def load_cfg(path, what):
    if not os.path.exists(path):
        sys.exit(f"找不到{what}：{path}")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    labels = cfg.get("labels") or []
    if not labels:
        sys.exit(f"{path} 里没有 labels")
    return cfg, labels


def main():
    ap = argparse.ArgumentParser(description="给 VectorCraft 里的 SDS-PAGE 胶图加标签")
    ap.add_argument("project", help="标好位置的 .vectorcraft 工程文件")
    ap.add_argument("--markers", default=None, help="marker 配置 JSON（默认与工程同目录的 markers.json）")
    ap.add_argument("--lanes", default=None, help="泳道配置 JSON（默认与工程同目录的 lanes.json）")
    ap.add_argument("--out", default=None, help="输出工程文件（默认 <名字>_labeled.vectorcraft）")
    ap.add_argument("--preview", default=None, help="顺手导一张 PNG 预览到这个路径")
    ap.add_argument("--preview-background", default="white",
                    help="预览图背景：white（默认）/ transparent / black / #rrggbb")
    ap.add_argument("--marks", default="guides", help="标记来源：guides（参考线，默认）或 layer:图层名")
    ap.add_argument("--marker-layer", default=LABEL_LAYERS[0],
                    help=f"分子量标签放哪个图层（默认 {LABEL_LAYERS[0]}）")
    ap.add_argument("--lane-layer", default=LABEL_LAYERS[1],
                    help=f"泳道名放哪个图层（默认 {LABEL_LAYERS[1]}）")
    ap.add_argument("--clear-text-in", default=None, help="先删掉这个图层里已有的文字（清掉老的手打标签）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"控制口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--app", default=DEFAULT_APP, help="VectorCraft 可执行文件路径")
    ap.add_argument("--keep-open", action="store_true", help="跑完不关掉脚本自己启动的 VectorCraft")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写的内容和位置，不改文件")
    args = ap.parse_args()

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
        lane_w = float(m_cfg.get("lane_width") or (img["width"] / max(1, len(lane_x))))
        gap_m = float(m_cfg.get("gap", 4))
        gap_l = float(l_cfg.get("gap", 0))
        align_l = l_cfg.get("align", "center")

        # marker 泳道
        if m_cfg.get("marker_lane_x") is not None:
            mx = float(m_cfg["marker_lane_x"])
        else:
            idx = int(m_cfg.get("marker_lane", 1))
            if idx == 0:                       # 0 = 第 1 条泳道左边一条泳道宽处
                mx = lane_x[0] - lane_w
            elif 1 <= idx <= len(lane_x):
                mx = lane_x[idx - 1]
            else:
                sys.exit("markers.json 的 marker_lane 超出泳道数量")
        left_side = mx < (img["left"] + img["right"]) / 2.0
        m_justify = "right" if left_side else "left"
        edge = mx if m_cfg.get("lane_edge") == "inner" else (
            mx - lane_w / 2.0 if left_side else mx + lane_w / 2.0)
        m_x = edge - gap_m if left_side else edge + gap_m
        l_y = img["top"] - gap_l

        print(f"[i] 胶图 {img['width']:.1f}×{img['height']:.1f} pt，左边缘 x={img['left']:.1f}，"
              f"顶边 y={img['top']:.1f}")
        print(f"[i] marker 泳道 x≈{mx:.1f}（图片{'左' if left_side else '右'}半边）→ {m_justify} 对齐，锚点 x={m_x:.1f}")
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

        # 3. 另存 + 预览
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

        if args.preview:
            pv = os.path.abspath(os.path.expanduser(args.preview))
            vc.ex("file.exportAs", {"path": pv, "format": "png", "scale": 2,
                                    "background": args.preview_background})
            for _ in range(60):
                if os.path.exists(pv):
                    break
                time.sleep(0.3)
            print(f"[✓] 预览：{pv}")
    finally:
        vc.close(quit_app=not args.keep_open)


if __name__ == "__main__":
    main()
