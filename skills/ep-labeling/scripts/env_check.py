#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doctor：环境自检。"""
import os
import sys

from magick_io import MAGICK, _run
from text_layout import pick_font
import vc_control


def cmd_doctor(args):
    """环境自检：能跑就一行行打出来，缺什么直接说怎么补。"""
    import platform
    import shutil
    bad = 0
    print("python : %s (%s)" % (sys.executable, platform.python_version()))
    mg = shutil.which(MAGICK)
    if not mg:
        print("magick : 缺 —— ImageMagick 7 没装（macOS: brew install imagemagick / "
              "Windows: winget install ImageMagick.ImageMagick / Linux: apt install imagemagick）")
        bad += 1
    else:
        ver = _run([MAGICK, "-version"], False).strip().splitlines()[0]
        print("magick : %s\n         %s" % (mg, ver))
        if "ImageMagick 7" not in ver:
            print("         !! 本 skill 按 ImageMagick 7 的用法写的（-annotate/-rotate 行为），6.x 请自测")
    f = pick_font()
    print("字体   : %s" % (f or "没找到 —— 用 --font 指一个 .ttf/.ttc"))
    if not f:
        bad += 1
    ver = vc_control.app_version()
    if not ver:
        print("VectorCraft: 缺 —— %s 不存在；fig.png 由 VectorCraft 导出，必须装 0.14.x" % vc_control.APP)
        bad += 1
    else:
        print("VectorCraft: %s（%s）" % (vc_control.APP, ver))
        if not vc_control.version_ok(ver):
            print("         !! 本 skill 按 VectorCraft %sx 实测（references/vectorcraft.md），"
                  "其它版本先按其 §6 复核" % vc_control.SUPPORTED)
        port = vc_control.DEFAULT_PORT
        if vc_control.listening(port):
            print("         控制口 %d：在听" % port)
        elif vc_control.app_running():
            print("         控制口 %d：没人听，但 VectorCraft 在跑（没带 --control）—— render/sync 前请存盘退出" % port)
        else:
            print("         控制口 %d：没人听（render 会自己带 --control 启动）" % port)
    skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("data/markers.json", "data/gel_size.json"):
        p = os.path.join(skill, rel)
        print("数据   : %s %s" % (rel, "存在" if os.path.isfile(p) else "缺 —— render 要用 --markers / --gel-size 显式指路径"))
    print("selftest: 跑 `python3 %s selftest` 验算法（16 项）" % "gel_photo.py")
    return 1 if bad else 0
