"""
layout — display width and the number formatters the two terminal tables share (§5).

`_dwidth` measures a string in terminal columns (East Asian wide / fullwidth characters count as
two), and the `_fmt_*` helpers decide how one number is printed: equation parameters, r2, a
measured mass or concentration, a three-significant-digit area in a note.  They are display only —
every JSON value keeps ruling R2's fixed precision.
"""

import math
import unicodedata

def _dwidth(text):
    """Display width of a string: East Asian wide / fullwidth characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad_right(text, width):
    return text + " " * max(0, width - _dwidth(text))


def _pad_left(text, width):
    return " " * max(0, width - _dwidth(text)) + text


def _fmt_trim(value, nd):
    """2.0 → '2', 6.02 → '6.02': round to nd dp, drop trailing zeros, never print -0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    text = "%.*f" % (nd, number)
    if float(text) == 0.0:
        text = "%.*f" % (nd, 0.0)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _fmt_eq(value):
    """An equation parameter as the §5 sample prints it: 2 dp, trailing zeros dropped (`0.4`,
    `6.02`, `1.05`).  A nonzero parameter too small for 2 dp keeps two significant digits instead,
    so the printed equation can never claim a zero coefficient that the fit did not find."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number == 0.0 or not math.isfinite(number):
        return "0"
    if abs(number) < 0.005:
        digits = 1 - int(math.floor(math.log10(abs(number))))
        return _fmt_trim(number, min(8, digits))
    return _fmt_trim(number, 2)


def _fmt_fixed(value, nd):
    """r² always shows its 4 dp (a perfect fit reads 1.0000, not 1) — ruling R2's precision."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if round(number, nd) == 0.0:
        number = 0.0
    return "%.*f" % (nd, number)


def _fmt_value(value):
    """A measured mass / concentration as the terminal table prints it: up to 3 dp, trailing zeros
    dropped, at least one decimal kept (`0.522`, `0.052`, `5.0`).

    §5's sample (`3.1 ug`, `2.5 ug/uL`, `100%`) is unchanged by this rule, while real lanes whose
    concentrations differ in the third decimal no longer collapse to the same `0.1 ug/uL` (lead
    ruling on ep-bqv.5).  The JSON keeps ruling R2's fixed 3 dp — this is display only.  A value
    that rounds to zero keeps no sign, the convention `_fmt_signed` already documents.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number) or round(number, 3) == 0.0:
        number = 0.0
    text = "%.3f" % number
    text = text.rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def _fmt_sig(value):
    """Three significant digits, no scientific notation: 4.495 → '4.5', 12.0 → '12', 0.522 → '0.522'.

    The out-of-range note and the lane line share this formatter, so a measurement can never print
    the contradictory `面积 4.5 > 4.5` that separate precisions produced (area 4.495 against a
    maximum of 4.484, both rounded to 1 dp).  It matches `%.3g` wherever the note used to be
    readable; above 1000 `%.3g` switches to `1.2e+03`, which no note or table should show, so the
    fixed form is kept with three significant digits instead.  A whole number keeps no trailing
    decimal point (`12`).
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number == 0.0 or not math.isfinite(number):
        return "0"
    digits = 2 - int(math.floor(math.log10(abs(number))))
    if digits < 0:                       # ≥ 1000: round to three significant digits, no exponent
        number, digits = round(number, digits), 0
    # the cap keeps a tiny area from collapsing to a bare "0" in a note; 9 dp still shows three
    # significant digits down to 1e-7, and no realistic area ever needs more
    return _fmt_trim(number, min(9, digits))


def _fmt_signed(value, nd):
    """+0.8 / -1.5: a signed percentage (a value that rounds to zero keeps no sign)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if round(number, nd) == 0.0:
        number = 0.0
    return ("%+.*f" % (nd, number)) if number else ("%.*f" % (nd, 0.0))
