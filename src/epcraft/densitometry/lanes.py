"""
lanes — the lane grammar of `--lanes` (§4.1, ruling R8).

It mirrors `lane_select.parse_keep`, but every message names `--lanes`, because that is the flag
the user typed.
"""

import re

def parse_lanes(text, n):
    """'1,2,8-10' → [1, 2, 8, 9, 10] (1-based, de-duplicated, ascending).

    Grammar of `lane_select.parse_keep`, but the messages name `--lanes`: None or "" selects every
    lane.  Out of range / empty segment / reversed range / garbage raise RuntimeError, one line.
    """
    count = int(n)
    if count < 1:
        raise RuntimeError("--lanes：报告里的泳道数必须 ≥1（收到 %r）" % (n,))
    if text is None:
        return list(range(1, count + 1))
    s = str(text).strip()
    if not s:
        return list(range(1, count + 1))
    out = set()
    for raw in s.split(","):
        tok = raw.strip()
        if not tok:
            raise RuntimeError("--lanes 里有一段是空的：%r（逗号之间不能没有泳道号）" % s)
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", tok)
        if not m:
            raise RuntimeError("--lanes 里有一段写法不对：%r（只接受 3 或 8-10 这种，1 起）" % tok)
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) is not None else a
        if b < a:
            raise RuntimeError("--lanes 里区间反了：%r（左号必须 ≤ 右号）" % tok)
        for k in (a, b):
            if k < 1 or k > count:
                raise RuntimeError("--lanes 里第 %d 条越界：报告里只有 1–%d 条泳道（那段是 %r）"
                                   % (k, count, tok))
        out.update(range(a, b + 1))
    return sorted(out)
