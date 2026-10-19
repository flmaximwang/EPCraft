#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""marker / 泳道 / 胶体尺寸配置的读取与校验。"""
import argparse
import json
import os


def load_cfg(path, what):
    """读配置并校验 labels 非空。"""
    if not os.path.exists(path):
        raise RuntimeError("找不到%s：%s（用 --markers / --lanes 指到你的配置）" % (what, path))
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    labels = cfg.get("labels") or []
    if not labels:
        raise RuntimeError("%s 里没有 labels" % path)
    return cfg, labels


def as_entry(e, defaults):
    """labels 项可以是字符串，也可以是 {text,size,color,font}。"""
    if isinstance(e, str):
        return {"text": e}
    return dict(e)


def parse_gel_cm(s):
    v = [float(t) for t in s.lower().replace("x", " x ").replace("*", " x ").split() if t != "x"]
    if len(v) != 2:
        raise argparse.ArgumentTypeError("--gel-cm 要写成 宽x高，如 8.6x7.2（cm）")
    return {"width_cm": v[0], "height_cm": v[1], "_source": "--gel-cm 命令行给定"}


def load_markers(path, preset=None):
    """--markers 的文件：单个配置（dict）直接用；预设库（list）要 --preset 选一个。

    preset 可以是 1 起的序号，或 `_preset` 描述里出现过的子串（须唯一命中）。
    返回 (cfg, labels, 说明)。
    """
    if not os.path.exists(path):
        raise RuntimeError("找不到 marker 配置：%s" % path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        cfg, note = data, path
    else:
        names = [str(p.get("_preset", ""))[:60] for p in data]
        listing = "\n".join("  %d. %s" % (i, n) for i, n in enumerate(names, 1))
        if preset is None:
            raise RuntimeError("%s 是预设库（%d 个），要 --preset <序号或描述里的关键词>：\n%s"
                               % (path, len(data), listing))
        if str(preset).isdigit() and 1 <= int(preset) <= len(data):
            cfg = data[int(preset) - 1]
        else:
            hit = [p for p in data if str(preset).lower() in str(p.get("_preset", "")).lower()]
            if len(hit) != 1:
                raise RuntimeError("--preset %r 命中 %d 个（要恰好 1 个）：\n%s" % (preset, len(hit), listing))
            cfg = hit[0]
        note = "%s 的预设 %s" % (path, str(cfg.get("_preset", ""))[:40])
    labels = cfg.get("labels") or []
    if not labels:
        raise RuntimeError("%s 里没有 labels" % path)
    return cfg, labels, note


def load_gel_size(path):
    """胶体物理尺寸 JSON（width_cm 定标，height_cm 只核对；可选 trim_*_cm / lane_pitch_cm）。"""
    if not os.path.isfile(path):
        raise RuntimeError("找不到胶体尺寸文件：%s（用 --gel-size 指到 data/gel_size.json，或用 --gel-cm 宽x高）" % path)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("width_cm") or not cfg.get("height_cm"):
        raise RuntimeError("%s 里没有 width_cm / height_cm" % path)
    return cfg
