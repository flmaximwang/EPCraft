#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VectorCraft 控制口客户端（0.14，`--control <port>`）。纯 stdlib。

协议：TCP 上一行一个 JSON 请求 `{"id","method","params"}`，回一行 `{"ok","result"|"error"}`；
每次调用开一条新连接（V0 实测走通的用法）。命令都走 `engine.execute {command, params}`。
实测依据与坑见 references/vectorcraft.md §2–§3：
- 没带 `--control` 的实例连不上 → 只能让用户存盘、退出，再由本模块带端口重启。
- 打开文件可能弹对话框（缺链接等），不关掉后续调用全卡 → open() 后先 clear_dialogs()。
- document.save 返回时文件可能还没写完 → 轮询到「比请求新且能解析」。
- document.export {artboard, ppi} 只出一个画板、app 自己写 pHYs → 这里只核对，不后处理。
- command.batch 全有或全无；undo_group() 把多步并成一个撤销步，出错 cancel 回滚。
"""
import contextlib
import itertools
import json
import os
import plistlib
import socket
import struct
import subprocess
import time

import vc_file


DEFAULT_PORT = 7797
APP = os.environ.get("VECTORCRAFT_APP", "/Applications/VectorCraft.app")
SUPPORTED = "0.14."
_ids = itertools.count(1)


class VCError(RuntimeError):
    """控制口或 app 报的错（消息里写清楚用户该做什么）。"""


# ---------------------------------------------------------------- app 与端口

def app_exe(app=APP):
    return os.path.join(app, "Contents", "MacOS", "VectorCraft")


def app_version(app=APP):
    """Info.plist 里的版本号；没装 → None。"""
    plist = os.path.join(app, "Contents", "Info.plist")
    if not os.path.isfile(plist):
        return None
    with open(plist, "rb") as f:
        return plistlib.load(f).get("CFBundleShortVersionString")


def version_ok(ver):
    return bool(ver) and ver.startswith(SUPPORTED)


def listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def app_running():
    """有没有 VectorCraft 进程在跑（不管带没带 --control）。"""
    try:
        return subprocess.run(["pgrep", "-x", "VectorCraft"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False


def connect(port=DEFAULT_PORT, path=None, launch=True, wait=30.0, app=APP,
            _running=app_running, _popen=subprocess.Popen):
    """→ (Client, 是否本次启动)。端口上有实例就复用；没有就（launch 时）带 --control 起一个。

    已有 VectorCraft 在跑但端口没人听 = 它没带 --control：不替用户关它（会丢未存改动），报错说怎么办。
    """
    c = Client(port)
    if listening(port):
        return c, False
    if _running():
        raise VCError("VectorCraft 在运行，但端口 %d 上没有控制口（它不是用 --control 启动的）。\n"
                      "请在 VectorCraft 里存盘并退出，再重跑本命令；本 skill 会用 --control %d 重新打开。"
                      % (port, port))
    if not launch:
        raise VCError("端口 %d 上没有 VectorCraft 控制口；先运行：\n  %s --control %d <文件>"
                      % (port, app_exe(app), port))
    exe = app_exe(app)
    if not os.path.isfile(exe):
        raise VCError("找不到 VectorCraft（%s）。装好 VectorCraft 0.14 后再跑；`gel_photo.py doctor` 可自检。" % app)
    cmd = [exe, "--control", str(port)] + ([os.path.abspath(path)] if path else [])
    _popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    t_end = time.time() + wait
    while time.time() < t_end:
        if listening(port):
            return c, True
        time.sleep(0.25)
    raise VCError("启动了 VectorCraft，但 %.0f 秒内端口 %d 没有开始监听" % (wait, port))


# ---------------------------------------------------------------- 客户端

class Client:
    def __init__(self, port=DEFAULT_PORT, host="127.0.0.1", timeout=120.0):
        self.port, self.host, self.timeout = port, host, timeout

    def call(self, method, params=None):
        """一次请求一行回包；ok=false → VCError。"""
        req = {"id": next(_ids), "method": method, "params": params or {}}
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(1 << 20)
                    if not chunk:
                        break
                    buf += chunk
        except socket.timeout:
            raise VCError("VectorCraft 控制口 %.0f 秒没回 %s（可能有对话框挡着：看一眼 app 窗口）"
                          % (self.timeout, method))
        except OSError as e:
            raise VCError("连不上 VectorCraft 控制口 %s:%d（%s）" % (self.host, self.port, e))
        if not buf.strip():
            raise VCError("VectorCraft 控制口对 %s 回了空包（app 可能已退出）" % method)
        try:
            resp = json.loads(buf.decode("utf-8"))
        except ValueError:
            raise VCError("VectorCraft 控制口对 %s 回了非 JSON：%r" % (method, buf[:200]))
        if not resp.get("ok"):
            raise VCError("%s 失败：%s" % (method, resp.get("error")))
        return resp.get("result")

    def execute(self, command, params=None):
        try:
            return self.call("engine.execute", {"command": command, "params": params or {}})
        except VCError as e:
            raise VCError(str(e).replace("engine.execute", command, 1))

    def document_json(self):
        return self.execute("document.json")

    def inspect(self, depth=0):
        return self.execute("document.inspect", {"depth": depth})

    def dialog(self):
        ui = self.call("ui.inspect") or {}
        return (ui.get("ui") or {}).get("dialog")

    def clear_dialogs(self, tries=6):
        """关掉挡路的对话框（最多 tries 次）。返回关掉的对话框 kind 列表。"""
        closed = []
        for _ in range(tries):
            d = self.dialog()
            if not d:
                return closed
            closed.append(d.get("kind"))
            self.call("ui.dialog.cancel")
        if self.dialog():
            raise VCError("关了 %d 次对话框还有（%s），请在 VectorCraft 里手动处理" % (tries, closed[-1]))
        return closed

    def open(self, path):
        """打开文件并清对话框。返回 file.open 的结果（含 warnings）。"""
        r = self.execute("file.open", {"path": os.path.abspath(path)}) or {}
        self.clear_dialogs()
        return r

    def save(self, path, timeout=60.0):
        """存为 .vectorcraft，轮询到文件比请求新且能被 vc_file 解析。返回解析结果 (top, blobs)。"""
        path = os.path.abspath(path)
        before = _mtime_ns(path)
        self.execute("document.save", {"path": path})
        return _poll(path, before, timeout, lambda data: vc_file.loads(data, path), "存盘")

    def export_png(self, path, artboard, dpi, background="white", timeout=60.0):
        """导出第 artboard 个画板为 PNG（ppi=dpi）。核对：比请求新、是 PNG、pHYs = dpi、
        像素尺寸 = 画板 pt × dpi/72（±1 px）。返回 (宽, 高, dpi)。"""
        path = os.path.abspath(path)
        abs_ = (self.inspect(0) or {}).get("artboards") or []
        if not 0 <= artboard < len(abs_):
            raise VCError("文档只有 %d 个画板，没有第 %d 个" % (len(abs_), artboard))
        ab = abs_[artboard]
        before = _mtime_ns(path)
        self.execute("document.export", {"path": path, "format": "png", "artboard": artboard,
                                         "ppi": dpi, "background": background})
        w, h, got = _poll(path, before, timeout, png_info, "导出")
        if got is None or abs(got - dpi) > 0.5:
            raise VCError("导出的 PNG dpi 不对：要 %s，pHYs 是 %s（%s）" % (dpi, got, path))
        ew, eh = ab["width"] * dpi / 72.0, ab["height"] * dpi / 72.0
        if abs(w - ew) > 1 or abs(h - eh) > 1:
            raise VCError("导出的 PNG 尺寸 %dx%d 与画板 %r 在 %s dpi 下的 %.0fx%.0f 不符"
                          % (w, h, ab.get("name"), dpi, ew, eh))
        return w, h, got

    def batch(self, commands, label="ep-craft"):
        """command.batch：全有或全无、一个撤销步。commands = [(command, params), …]。"""
        return self.execute("command.batch", {"label": label, "commands": [
            {"command": c, "params": p or {}} for c, p in commands]})

    @contextlib.contextmanager
    def undo_group(self, label="ep-craft sync"):
        """with 块里的编辑并成一个撤销步；块里抛错 → cancel（回滚）再把错抛出去。"""
        self.execute("edit.undoGroup.begin", {"label": label})
        try:
            yield self
        except BaseException:
            with contextlib.suppress(VCError):      # 回滚失败也要抛原来的错
                self.execute("edit.undoGroup.end", {"cancel": True})
            raise
        self.execute("edit.undoGroup.end", {})

    def quit(self):
        """退出 app（有未存文档时 app 会弹存盘询问，不在这里替用户回答）。"""
        return self.call("app.quit")


# ---------------------------------------------------------------- PNG / 轮询

def png_info(data):
    """PNG 字节 → (宽, 高, dpi 或 None)。dpi 从 pHYs（单位=米）换算。"""
    w, h = vc_file.png_size(data)
    i = 8
    while i + 8 <= len(data):
        n, tag = struct.unpack(">I4s", data[i:i + 8])
        if tag == b"pHYs" and n == 9:
            ppx, _ppy, unit = struct.unpack(">IIB", data[i + 8:i + 17])
            return w, h, (ppx * 0.0254 if unit == 1 else None)
        if tag in (b"IDAT", b"IEND"):
            break
        i += 12 + n
    return w, h, None


def _mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return -1


def _poll(path, before, timeout, parse, what):
    """等 path 比 before 新、非空、parse(bytes) 不抛错；超时报错。返回 parse 的结果。"""
    t_end = time.time() + timeout
    last = None
    while True:
        if _mtime_ns(path) > before:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                if data:
                    return parse(data)
            except (RuntimeError, ValueError, OSError) as e:
                last = e
        if time.time() >= t_end:
            raise VCError("%s %.0f 秒内没有得到完整文件：%s%s"
                          % (what, timeout, path, "（最后一次：%s）" % last if last else ""))
        time.sleep(0.05)
