"""
json_io — the chain's JSON I/O and the report v2 validation `measure` depends on.

`report_path` resolves the sibling `<stem>.mw.json` of a `.vectorcraft` document (decision §2.1),
`load_json` / `write_json` are the one reader and writer every chain file goes through (UTF-8,
`ensure_ascii=False`, indent 1, trailing newline — ruling R2), and `load_report` normalises and
validates a report v2 so `measure` never re-checks it (R6: `source.report`).
"""

import json
import os

from .contracts import DOC_SUFFIX, REPORT_PATH_KEY, REPORT_SUFFIX
from .values import _dy, _one_line

def report_path(path):
    """A `.vectorcraft` document path → its report sibling `<stem>.mw.json`; anything else is
    already the report path (decision §2.1).  Pure string work — no existence check."""
    text = str(path)
    if text.lower().endswith(DOC_SUFFIX):
        return text[:-len(DOC_SUFFIX)] + REPORT_SUFFIX
    return text


def load_json(path, kind=None):
    """Read a JSON object written by this chain (ruling R4).

    RuntimeError (single line, Chinese) when the file is missing, is not JSON, is not an object,
    or its `kind` differs from `kind`.  `load_report` is built on this.
    """
    text = str(path)
    if not os.path.exists(text):
        raise RuntimeError("找不到 %s" % text)
    try:
        with open(text, encoding="utf-8") as f:
            obj = json.load(f)
    except ValueError as e:
        raise RuntimeError("%s 不是合法的 JSON：%s" % (text, _one_line(e)))
    if not isinstance(obj, dict):
        raise RuntimeError("%s 不是 JSON 对象（顶层是 %s）" % (text, type(obj).__name__))
    if kind is not None and obj.get("kind") != kind:
        raise RuntimeError("%s 不是 %s 文件（kind=%r）" % (text, kind, obj.get("kind")))
    return obj


def _validate_report(rep, where):
    """Validate report v2: version, lanes, per-lane profile arrays (length, non-empty, uniform y)."""
    if rep.get("version") != 2:
        raise RuntimeError("%s 不是报告 v2（version=%r）" % (where, rep.get("version")))
    lanes = rep.get("lanes")
    if not isinstance(lanes, list):
        raise RuntimeError("%s 里没有 lanes 列表（不是报告 v2？）" % where)
    if not lanes:
        raise RuntimeError("%s 里没有泳道" % where)
    for ln in lanes:
        if not isinstance(ln, dict):
            raise RuntimeError("%s 的 lanes 里有不是对象的条目" % where)
        no = ln.get("lane")
        prof = ln.get("profile")
        if not isinstance(prof, dict):
            raise RuntimeError("%s 的泳道 %r 没有 profile" % (where, no))
        arrays = []
        for key in ("y_cm", "gray", "baseline"):
            vals = prof.get(key)
            if not isinstance(vals, list) or not vals:
                raise RuntimeError("%s 的泳道 %r 的 profile 缺少 %s 数组" % (where, no, key))
            arrays.append(vals)
        if len(arrays[0]) != len(arrays[1]) or len(arrays[0]) != len(arrays[2]):
            raise RuntimeError("%s 的泳道 %r 的 profile 三条数组长度不一致（%d / %d / %d）"
                               % (where, no, len(arrays[0]), len(arrays[1]), len(arrays[2])))
        try:
            _dy([float(v) for v in arrays[0]])
        except RuntimeError as e:
            raise RuntimeError("%s 的泳道 %r：%s" % (where, no, e))
    for key in ("gel", "window"):
        if not isinstance(rep.get(key), dict):
            raise RuntimeError("%s 缺少 %s（不是报告 v2？）" % (where, key))


def load_report(path):
    """Report v2 at path → dict (a `.vectorcraft` path resolves to its `<stem>.mw.json` first).

    A missing / stale report for a document raises RuntimeError naming `先 sync（它会重写这份报告）`
    (decision §2.1: sync rewrites the report, so the document is the truth).  The returned dict is
    a shallow copy carrying REPORT_PATH_KEY = the path as the user typed it (source.report, ruling
    R6) — use `report_path()` on it when the file that was actually read is needed.
    """
    typed = str(path)
    rp = report_path(typed)
    if rp != typed and not os.path.exists(typed):
        raise RuntimeError("找不到 %s" % typed)
    if not os.path.exists(rp):
        if rp != typed:
            raise RuntimeError("%s 不存在；先 sync（它会重写这份报告）" % rp)
        raise RuntimeError("找不到 %s（报告要由 render / sync 生成）" % rp)
    if rp != typed:
        try:
            stale = os.path.getmtime(rp) < os.path.getmtime(typed)
        except OSError as e:
            raise RuntimeError("读不了 %s 的时间戳：%s" % (rp, _one_line(e)))
        if stale:
            raise RuntimeError("%s 比文档 %s 旧；先 sync（它会重写这份报告）" % (rp, typed))
    rep = load_json(rp)
    _validate_report(rep, rp)
    return dict(rep, **{REPORT_PATH_KEY: typed})


def write_json(obj, path):
    """UTF-8, ensure_ascii=False, indent=1, trailing newline (§4.1, §4.4, ruling R2)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
