#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""editor：把 scene 与胶面 PNG 填进 `templates/editor.html`，以及从生成的 HTML 抽回来。

编辑器只有两个占位符（spec §6）：

    __SCENE_JSON__    <script type="application/json" id="scene"> 的内容
    __GEL_DATA_URI__  <img id="gel" src="..."> 的 data:image/png;base64,...

本模块只做字节层面的填充/抽取，不跑 magick、不读照片；`extract()` 让 `export` 能
把用户改过的 HTML 当成 scene 输入（spec §7）。模板里的 JS/CSS 全在一个文件里，
离线可用（spec §2 决策 4）。
"""
import base64
import json
import os
import re

from scene import validate

SCENE_TOKEN = "__SCENE_JSON__"
GEL_TOKEN = "__GEL_DATA_URI__"

# scripts/ 的上一级就是 skill 根目录
DEFAULT_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "editor.html")

_SCENE_RE = re.compile(
    r'<script\b[^>]*\bid\s*=\s*["\']scene["\'][^>]*>(.*?)</script>', re.S | re.I)
_IMG_RE = re.compile(r'<img\b[^>]*\bid\s*=\s*["\']gel["\'][^>]*>', re.S | re.I)
_SRC_RE = re.compile(r'\bsrc\s*=\s*["\'](data:image/png;base64,[^"\']*)["\']', re.S | re.I)


def _read(path, binary=False):
    if binary:
        with open(path, "rb") as f:
            return f.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_html(template_path, scene, gel_png_path, out_path):
    """用模板生成一个自包含的编辑器 HTML（scene JSON + 胶面 data URI）。

    scene 先 validate：编辑器只该拿到合法版式，非法数据在 Python 这边就报错，
    而不是等浏览器打开后白屏。胶面 PNG 原样 base64 嵌入，不重编码（不丢像素）。
    """
    validate(scene)
    tpl = _read(template_path)
    for token in (SCENE_TOKEN, GEL_TOKEN):
        n = tpl.count(token)
        if n != 1:
            raise RuntimeError("编辑器模板的占位符 %s 应恰好出现 1 次（现在 %d 次）：%s"
                               % (token, n, template_path))
    gel = _read(gel_png_path, binary=True)
    if not gel.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("胶面不是 PNG：%s" % gel_png_path)

    # JSON 里的 `<` 转义成 `\u003c`：标签文字出现 `</script>` 或 `<!--` 也不会把
    # 数据块提前关掉 / 改状态。两者都是合法 JSON，json.loads 读回来完全一样。
    scene_json = json.dumps(scene, ensure_ascii=False, separators=(",", ":"))
    scene_json = scene_json.replace("<", "\\u003c")
    data_uri = "data:image/png;base64," + base64.b64encode(gel).decode("ascii")

    html = tpl.replace(SCENE_TOKEN, scene_json).replace(GEL_TOKEN, data_uri)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def extract(path):
    """从生成/保存过的编辑器 HTML 读回 (scene_dict, gel_png_bytes)。

    scene 走 json.loads 后 validate；胶面只接受 `data:image/png;base64,...`
    （模板只写这一种），用 stdlib base64 解码，不引入新依赖。
    """
    html = _read(path)
    m = _SCENE_RE.search(html)
    if not m:
        raise RuntimeError("HTML 里找不到 #scene 数据块：%s" % path)
    try:
        scene = json.loads(m.group(1))
    except ValueError as e:
        raise RuntimeError("HTML 里的 #scene 不是合法 JSON：%s（%s）" % (path, e))
    validate(scene)

    tag = _IMG_RE.search(html)
    if not tag:
        raise RuntimeError("HTML 里找不到 #gel 图片：%s" % path)
    src = _SRC_RE.search(tag.group(0))
    if not src:
        raise RuntimeError("#gel 的 src 不是内嵌 PNG data URI：%s" % path)
    payload = src.group(1).split(",", 1)[1].strip()
    try:
        gel = base64.b64decode(payload)
    except Exception as e:
        raise RuntimeError("胶面 data URI 解码失败：%s（%s）" % (path, e))
    if not gel.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("HTML 里嵌的胶面不是 PNG：%s" % path)
    return scene, gel
