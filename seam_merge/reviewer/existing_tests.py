"""Which pre-existing tests a change rewrote (doc §6, the existing-test guard).

A pre-existing test is the suite's record of current behavior. If a worker edits it
to match new code, the full suite stays green and a behavior change merges unseen:
in the boltons replay (doc §12, r3) a wrong contract said a missing file should
raise, and the worker rewrote `test_rotate_file_no_rotation` to expect that.

The rule is syntactic and deterministic: a test function that existed at the merge
base is *changed* if it was deleted or if any of its original top-level statements
or decorators no longer appears in it (compared as `ast.unparse` text, so
formatting and comments do not count). Adding statements is fine: a test may grow
stronger without permission. Only the lead can authorize a change, by listing the
test in the task's `may_change_tests`.
"""

from __future__ import annotations

import ast
from collections import Counter


def is_test_file(path: str) -> bool:
    """pytest's default test-file names: `test_*.py` or `*_test.py`."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _tests(source: str, path: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    """`path::[Class::]name -> node` for each test function, as pytest collects them
    by default (`test*` functions, and methods of `Test*` classes); None if the
    source does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def visit(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("test"):
                    found[f"{path}::{prefix}{node.name}"] = node
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                visit(node.body, f"{prefix}{node.name}::")

    visit(tree.body, "")
    return found


def _parts(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Counter[str]:
    return Counter([ast.unparse(d) for d in fn.decorator_list]
                   + [ast.unparse(s) for s in fn.body])


def changed_tests(path: str, old: str, new: str | None) -> dict[str, str]:
    """Pre-existing tests in `path` that the change rewrote: `test id -> "removed" |
    "changed"`. `new` None means the file was deleted. If the original does not parse
    there is nothing to protect; if the new version does not parse, every test counts
    as removed (the suite would not collect them)."""
    before = _tests(old, path)
    if not before:
        return {}
    after = _tests(new, path) if new is not None else None
    if after is None:
        return {tid: "removed" for tid in before}
    out: dict[str, str] = {}
    for tid, fn in before.items():
        if tid not in after:
            out[tid] = "removed"
        elif _parts(fn) - _parts(after[tid]):
            out[tid] = "changed"
    return out
