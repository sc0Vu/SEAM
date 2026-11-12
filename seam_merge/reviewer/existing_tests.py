"""Which pre-existing tests a change rewrote (doc §6, the existing-test guard).

A pre-existing test is the suite's record of current behavior. If a worker edits it
to match new code, the full suite stays green and a behavior change merges unseen:
in the boltons replay (doc §12, r3) a wrong contract said a missing file should
raise, and the worker rewrote `test_rotate_file_no_rotation` to expect that.

The rule is syntactic and deterministic: a test function that existed at the merge
base is *changed* if it was deleted or if any of its original top-level statements
or decorators no longer appears in it (compared as `ast.unparse` text, so
formatting and comments do not count). Adding statements is fine: a test may grow
stronger without permission, except what silences it: a skip or xfail marker on the
test, its class or its module (`pytestmark`), or an added `return`,
`pytest.skip(...)` or `pytest.xfail(...)`. Only the lead can authorize a change, by
listing the test in the task's `may_change_tests`.
"""

from __future__ import annotations

import ast
from collections import Counter


def is_test_file(path: str) -> bool:
    """pytest's default test-file names: `test_*.py` or `*_test.py`."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


_Fn = ast.FunctionDef | ast.AsyncFunctionDef
_SILENCERS = ("skip", "xfail")
_SILENCING_CALLS = ("pytest.skip(", "pytest.xfail(", "pytest.importorskip(")


def _markers(nodes: list[ast.stmt]) -> list[str]:
    """`pytestmark = ...` assignments in a module or class body, as text."""
    return [ast.unparse(n) for n in nodes if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in n.targets)]


def _tests(source: str, path: str) -> dict[str, tuple[_Fn, list[str]]] | None:
    """`path::[Class::]name -> (node, enclosing markers)` for each test function, as
    pytest collects them by default (`test*` functions, and methods of `Test*`
    classes); the markers are the module's and class's `pytestmark` and the class
    decorators. None if the source does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found: dict[str, tuple[_Fn, list[str]]] = {}

    def visit(nodes: list[ast.stmt], prefix: str, context: list[str]) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if node.name.startswith("test"):
                    found[f"{path}::{prefix}{node.name}"] = (node, context)
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                inner = [*context, *(ast.unparse(d) for d in node.decorator_list),
                         *_markers(node.body)]
                visit(node.body, f"{prefix}{node.name}::", inner)

    visit(tree.body, "", _markers(tree.body))
    return found


def _parts(fn: _Fn) -> Counter[str]:
    return Counter([ast.unparse(d) for d in fn.decorator_list]
                   + [ast.unparse(s) for s in fn.body])


def _silenced(old: tuple[_Fn, list[str]], new: tuple[_Fn, list[str]]) -> bool:
    """Whether the new version adds something that stops the test from asserting:
    a skip/xfail marker (on it, its class or module), or a `return` or skip/xfail
    call in its body."""
    added = _parts(new[0]) - _parts(old[0])
    added_context = Counter(new[1]) - Counter(old[1])
    for text in [*added, *added_context]:
        if any(word in text for word in _SILENCERS) and ("mark" in text or "pytestmark" in text):
            return True
        if any(call in text for call in _SILENCING_CALLS):
            return True
    added_stmts = [s for s in new[0].body if ast.unparse(s) in added]
    return any(_returns(s) for s in added_stmts)


def _returns(node: ast.AST) -> bool:
    """A `return` that exits the test itself (not one inside a nested def/lambda)."""
    if isinstance(node, ast.Return):
        return True
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
        return False
    return any(_returns(child) for child in ast.iter_child_nodes(node))


def changed_tests(path: str, old: str, new: str | None) -> dict[str, str]:
    """Pre-existing tests in `path` that the change rewrote: `test id -> "removed" |
    "changed" | "silenced"`. `new` None means the file was deleted. If the original
    does not parse there is nothing to protect; if the new version does not parse,
    every test counts as removed (the suite would not collect them)."""
    before = _tests(old, path)
    if not before:
        return {}
    after = _tests(new, path) if new is not None else None
    if after is None:
        return {tid: "removed" for tid in before}
    out: dict[str, str] = {}
    for tid, old_test in before.items():
        if tid not in after:
            out[tid] = "removed"
        elif _parts(old_test[0]) - _parts(after[tid][0]):
            out[tid] = "changed"
        elif _silenced(old_test, after[tid]):
            out[tid] = "silenced"
    return out
