"""Dashboard stylesheets: a block left open swallows every rule after it into a media query.

On 2026-09-29 a merge dropped the closing brace of two phone-only @media blocks in gallery.css, so every
section appended after them (shell, calls, markets, system) silently applied to phones only. These checks
catch that before it ships: braces balance, never close more than they open, and every section marker
comment ("/* ─── name ─── */") sits at the top level, outside any block.
"""

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "albatross" / "api" / "static"
SHEETS = sorted(STATIC.glob("*.css"))


def scan(css: str):
    """Walk the sheet outside comments and strings: (final depth, [(line, depth)] of each '}' going below 0,
    [(line, depth, text)] of each section marker comment)."""
    depth, line, i, n = 0, 1, 0, len(css)
    under, markers = [], []
    while i < n:
        if css.startswith("/*", i):
            j = css.find("*/", i + 2)
            j = n if j < 0 else j + 2
            comment = css[i:j]
            if comment.startswith("/* ───"):
                markers.append((line, depth, comment.splitlines()[0][:70]))
            line += comment.count("\n")
            i = j
            continue
        ch = css[i]
        if ch in "\"'":                                  # a quoted string may hold a brace
            j = i + 1
            while j < n and css[j] != ch:
                j += 2 if css[j] == "\\" else 1
            line += css[i:j].count("\n")
            i = j + 1
            continue
        if ch == "\n":
            line += 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                under.append((line, depth))
        i += 1
    return depth, under, markers


def test_there_are_stylesheets_to_check():
    assert {p.name for p in SHEETS} >= {"style.css", "gallery.css", "micro.css"}


@pytest.mark.parametrize("sheet", SHEETS, ids=lambda p: p.name)
def test_braces_balance(sheet):
    depth, under, _ = scan(sheet.read_text())
    assert not under, f"{sheet.name}: '}}' with nothing open at line {under[0][0]}"
    assert depth == 0, f"{sheet.name}: {depth} block(s) never closed; every rule after the first one is nested inside it"


@pytest.mark.parametrize("sheet", SHEETS, ids=lambda p: p.name)
def test_every_section_starts_at_the_top_level(sheet):
    _, _, markers = scan(sheet.read_text())
    nested = [f"line {ln}: {text}" for ln, d, text in markers if d != 0]
    assert not nested, f"{sheet.name}: sections nested inside an unclosed block: {nested}"


def test_the_scanner_catches_the_2026_09_29_merge_bug():
    broken = "@media (max-width: 640px) {\n  .a { color: red; }\n/* ─── qa: shell ─── */\n.b { color: blue; }\n"
    depth, _, markers = scan(broken)
    assert depth == 1 and markers[0][1] == 1
    ok = 'a::before { content: "}"; }\n/* ─── ok ─── */\n'
    depth, under, markers = scan(ok)
    assert depth == 0 and not under and markers[0][1] == 0


def test_no_merge_markers_left_in_static_files():
    import re
    for f in STATIC.glob("*.*"):
        if f.suffix in {".css", ".js", ".html"}:
            assert not re.search(r"^(<<<<<<<|=======|>>>>>>>)( |$)", f.read_text(), flags=re.M), f.name
