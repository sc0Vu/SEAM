"""What the reviewer sees of a changed file's ORIGINAL text, so it judges a diff against
the real surrounding code (invariant 6) without paying to read whole large files.

Small files are sent whole. A large Python file becomes a focused view: an outline of
every class, function and module/class-level assignment, plus the full source of the
definitions the diff touches, the header and `__init__` of their classes, the
definitions of names the diff uses (one hop further through their decorators), and
assignments that bind or alias those names (`qp = query_params`). The source is parsed with `ast`,
never executed: it is untrusted data. Anything that does not parse falls back to
line windows around each hunk.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

WHOLE_FILE_CHARS = 12_000
WINDOW_LINES = 40
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class FileChange:
    """One file's part of a unified diff: old-file line ranges and identifiers used."""

    hunks: tuple[tuple[int, int], ...]  # (first, last) line in the ORIGINAL, 1-based
    names: frozenset[str]  # identifiers on added/removed lines


def split_diff(diff: str) -> dict[str, FileChange]:
    """Per-file hunks and identifiers from a `git diff` (keyed by the old path)."""
    out: dict[str, tuple[list, set]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = None
        elif line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
            if path is not None:
                out.setdefault(path, ([], set()))
        elif path is None:
            continue
        elif m := _HUNK.match(line):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out[path][0].append((start, start + max(count, 1) - 1))
        elif line[:1] in "+-" and not line.startswith(("+++", "---")):
            out[path][1].update(_IDENT.findall(line[1:]))
    return {p: FileChange(tuple(h), frozenset(n)) for p, (h, n) in out.items()}


def view(text: str, change: FileChange | None, focus: bool = True) -> str:
    """The original `text` as the reviewer should see it, given what the diff did.
    With `focus=False` it is always the whole file."""
    if not focus or len(text) <= WHOLE_FILE_CHARS or change is None:
        return text
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _windows(lines, change.hunks, "not parseable as Python")
    return _focused(lines, tree, change)


def _numbered(lines: list[str], first: int, last: int) -> str:
    return "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, min(last, len(lines)) + 1))


def _windows(lines: list[str], hunks, why: str) -> str:
    spans = _merge([(max(1, a - WINDOW_LINES), b + WINDOW_LINES) for a, b in hunks])
    body = "\n   ...\n".join(_numbered(lines, a, b) for a, b in spans)
    head = f"[focused view ({why}): {len(lines)} lines, ±{WINDOW_LINES} around each change]"
    return f"{head}\n{body}"


def _merge(spans):
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(s) for s in out]


@dataclass
class _Def:
    name: str
    first: int  # first decorator line, or the def/class line
    last: int
    node: ast.AST
    parent: _Def | None


@dataclass(frozen=True)
class _Assign:
    first: int
    last: int
    names: frozenset[str]  # targets and names on the right-hand side
    depth: int


def _assigns(tree: ast.AST) -> list[_Assign]:
    """Module- and class-level assignments (aliases, constants, descriptors)."""
    found: list[_Assign] = []

    def walk(body, depth):
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                found.append(_Assign(node.lineno, node.end_lineno or node.lineno,
                                     frozenset(names), depth))
            elif isinstance(node, ast.ClassDef):
                walk(node.body, depth + 1)

    walk(getattr(tree, "body", []), 0)
    return found


def _defs(tree: ast.AST) -> list[_Def]:
    found: list[_Def] = []

    def walk(node, parent):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([d.lineno for d in child.decorator_list] + [child.lineno])
                d = _Def(child.name, first, child.end_lineno or child.lineno, child, parent)
                found.append(d)
                walk(child, d)

    walk(tree, None)
    return found


def _focused(lines: list[str], tree: ast.AST, change: FileChange) -> str:
    defs = _defs(tree)
    leaf = [d for d in defs if not isinstance(d.node, ast.ClassDef)]
    by_name: dict[str, list[_Def]] = {}
    for d in defs:
        by_name.setdefault(d.name, []).append(d)

    picked: set[int] = set()  # ids into defs
    spans: list[tuple[int, int]] = []

    def take(d: _Def) -> None:
        if id(d) in picked:
            return
        picked.add(id(d))
        if isinstance(d.node, ast.ClassDef):  # a class: its header and __init__ only
            body = d.node.body
            spans.append((d.first, (body[0].lineno - 1) if body else d.last))
            for m in by_name.get("__init__", []):
                if m.parent is d:
                    spans.append((m.first, m.last))
        else:
            spans.append((d.first, d.last))
        if d.parent is not None:
            take(d.parent)

    for a, b in change.hunks:
        hit = [d for d in leaf if d.first <= b and a <= d.last]
        if hit:
            for d in hit:
                take(d)
        else:  # an insertion between definitions: show where it lands
            spans.append((max(1, a - 5), b + 5))
            for d in defs:
                if isinstance(d.node, ast.ClassDef) and d.first <= a <= d.last:
                    take(d)
    for name in change.names:
        for d in by_name.get(name, []):
            take(d)
            for dec in getattr(d.node, "decorator_list", []):
                for dn in _IDENT.findall(ast.unparse(dec)):
                    for target in by_name.get(dn, []):
                        take(target)

    assigns = _assigns(tree)
    for a in assigns:
        if a.names & change.names:
            spans.append((a.first, a.last))

    entries = [(d.node.lineno, _depth(d), _signature(lines, d)) for d in defs]
    entries += [(a.first, a.depth, lines[a.first - 1].strip()) for a in assigns]
    outline = "\n".join(f"{n:>5}  {'    ' * depth}{text}" for n, depth, text in sorted(entries))
    body = "\n   ...\n".join(_numbered(lines, a, b) for a, b in _merge(spans))
    return (f"[focused view: {len(lines)} lines. Outline of every definition, then the full "
            "source of those the diff touches or uses; elided parts are marked ...]\n"
            f"-- outline --\n{outline}\n-- source --\n{body}")


def _depth(d: _Def) -> int:
    n = 0
    while d.parent is not None:
        n, d = n + 1, d.parent
    return n


def _signature(lines: list[str], d: _Def) -> str:
    return lines[d.node.lineno - 1].strip()
