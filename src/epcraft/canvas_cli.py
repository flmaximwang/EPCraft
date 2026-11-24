#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""canvas_cli.py —— 公开 `epcraft` 的画板分组命令里「不属于旧扁平 CLI」的那几条。

`gel_photo.build_parser(canvas=True)` 先建好 figure / crop / workbench / profile 四个组以及共用的
probe / render / sync / edit 叶子命令，再调用这里的 `add_commands(sub, groups)` 补上：

    epcraft launch [DOC] [--port PORT]
    epcraft figure export DOC [--artboard NAME] [--out PATH] [--dpi DPI] [--background C] [--port PORT]
    epcraft workbench export DOC [--out PATH] [--port PORT]
    epcraft workbench import DOC --input PATH [--no-sync] [--dpi DPI] [--port PORT]

Workbench 数据交换的实现由 workbench_io.py 负责（延迟 import：两个文件分开演进，也避免循环依赖）；
活文档、stage 1 与 peak override 的工具函数在 gel_session.py。本模块只做参数、路径保护与错误边界；
业务错误统一抛 RuntimeError / OSError / ValueError，由 gel_photo.main 打一行 `[!] …` 并返回 2。
"""
import argparse
import os
import sys

from . import gel_session
from . import vc_control

FIGURE_ARTBOARD = "Figure"
FIGURE_OUT_SUFFIX = ".png"


# ---------------------------------------------------------------- 共用小工具

def _abs_doc(doc):
    path = os.path.abspath(doc)
    if not os.path.exists(path):
        raise RuntimeError("找不到 %s" % path)
    return path


def _ensure_open(client, path):
    """让 VectorCraft 打开并激活 path；返回文档下标（自检替换这个名字，实现见 gel_session.ensure_open）。"""
    return gel_session.ensure_open(client, path)


def _resolve_artboard(artboards, name):
    names = [a.get("name") for a in artboards]
    hits = [i for i, n in enumerate(names) if n == name]
    if not hits:
        raise RuntimeError("文档里没有叫 %r 的画板；现有画板：%s" % (name, names or "（无）"))
    if len(hits) > 1:
        raise RuntimeError("文档里有 %d 个叫 %r 的画板，无法确定导出哪一个" % (len(hits), name))
    return hits[0]


def _export_out(doc_path, out):
    """figure export 的输出路径：默认 <doc stem>.png；必须是 .png，且绝不能覆盖源文档或任意 JSON。"""
    if out is None:
        out = os.path.splitext(doc_path)[0] + FIGURE_OUT_SUFFIX
    out = os.path.abspath(out)
    if not out.lower().endswith(FIGURE_OUT_SUFFIX):
        raise ValueError("--out 必须是 .png 路径（收到 %s）" % out)
    if os.path.realpath(out) == os.path.realpath(doc_path):
        raise ValueError("--out 不能指向源文档 %s（PNG 不能覆盖 .vectorcraft）" % doc_path)
    return out


# ---------------------------------------------------------------- launch

def cmd_launch(args):
    """连上控制口；没有就带 --control 启动 VectorCraft。给了 DOC 就打开/激活它。"""
    path = os.path.abspath(args.doc) if args.doc else None
    if path and not os.path.exists(path):
        raise RuntimeError("找不到 %s" % path)
    client, launched = vc_control.connect(args.port, path=path, launch=True)
    if path:
        _ensure_open(client, path)
    print("[i] VectorCraft 控制口 %d 已就绪（%s）%s" % (
        args.port, "本次启动" if launched else "复用已有实例",
        "，已打开 %s" % path if path else ""))
    return 0


# ---------------------------------------------------------------- figure export

def cmd_figure_export(args):
    path = _abs_doc(args.doc)
    if args.dpi <= 0:
        raise ValueError("--dpi 必须是正数（收到 %s）" % args.dpi)
    out = _export_out(path, args.out)
    client, _launched = vc_control.connect(args.port, path=path, launch=True)
    _ensure_open(client, path)
    artboards = (client.inspect(0) or {}).get("artboards") or []
    # 公开命令只导出 Figure 画板；按名字解析下标，不暴露任意画板选择。
    idx = _resolve_artboard(artboards, FIGURE_ARTBOARD)
    w, h, dpi = client.export_png(out, idx, args.dpi, background=args.background)
    print("输出 %s（画板 %s，%d×%d px, %d dpi）" % (out, FIGURE_ARTBOARD, w, h, round(dpi)))
    return 0


# ---------------------------------------------------------------- Workbench 数据交换

def cmd_workbench_export(args):
    from . import workbench_io
    return workbench_io.cmd_export(args)


def cmd_workbench_import(args):
    from . import workbench_io
    return workbench_io.cmd_import(args)


# ---------------------------------------------------------------- 注册

def add_commands(sub, groups, sync=None):
    """在公开 CLI 骨架上补 workflow / launch / export / import。

    sync = 本层的 sync 处理函数（gel_photo.cmd_sync），注入给 workbench import 做「导入后跟着 sync」，
    这样 workbench_io 不必反过来 import gel_photo（CLI 胶水层的单向依赖）。
    """
    from .workflow_cli import add_commands as add_workflow
    add_workflow(sub, figure_export=cmd_figure_export)
    from .densitometry_cli import add_commands as add_densitometry
    add_densitometry(sub)
    lp = sub.add_parser("launch", help="启动或连接 VectorCraft 控制口；可选打开一份文档")
    lp.add_argument("doc", nargs="?", help="要打开的 fig.vectorcraft（省略则只启动/连接）")
    lp.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT,
                    help="VectorCraft 控制口（默认 %d）" % vc_control.DEFAULT_PORT)
    lp.set_defaults(func=cmd_launch)

    figure = groups.get("figure")
    if figure is not None:
        fe = figure.add_parser("export", help="把 Figure 画板导出为 PNG（不覆盖源文档）")
        fe.add_argument("doc", help="render 写出的 fig.vectorcraft")
        fe.add_argument("--out", help="PNG 输出路径（必须是 .png；默认与文档同 stem 的 .png）")
        fe.add_argument("--dpi", type=int, default=300, help="导出 dpi（默认 300）")
        fe.add_argument("--background", default="white", help="导出背景色（默认 white）")
        fe.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT,
                        help="VectorCraft 控制口（默认 %d）" % vc_control.DEFAULT_PORT)
        fe.set_defaults(func=cmd_figure_export)

    workbench = groups.get("workbench")
    if workbench is not None:
        we = workbench.add_parser("export", help="把 Workbench 的泳道/窗口/带数据导出为带版本的 JSON")
        we.add_argument("doc", help="render 写出的 fig.vectorcraft")
        we.add_argument("--out", help="输出 JSON 路径（省略 = 写到标准输出）")
        we.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT,
                        help="VectorCraft 控制口（默认 %d）" % vc_control.DEFAULT_PORT)
        we.set_defaults(func=cmd_workbench_export)

        wi = workbench.add_parser("import", help="从 JSON 恢复 Workbench 数据并校验后再写入")
        wi.add_argument("doc", help="render 写出的 fig.vectorcraft")
        wi.add_argument("--input", required=True, metavar="PATH", help="workbench export 写出的 JSON")
        wi.add_argument("--no-sync", action="store_true", help="只改文档，不跟着 sync")
        wi.add_argument("--dpi", type=int, default=300, help="ep-meta 里没记 dpi 时用它")
        wi.add_argument("--port", type=int, default=vc_control.DEFAULT_PORT,
                        help="VectorCraft 控制口（默认 %d）" % vc_control.DEFAULT_PORT)
        wi.set_defaults(func=cmd_workbench_import, sync=sync)   # workbench_io 用 args.sync 接 sync 处理函数
