"""File-scope overlap: the conflict-prevention core (invariant 2, doc §5).

Scopes are lists of path globs (`auth/**`, `api/users.*`). Two scopes overlap if some
path could match a glob in each. Deciding that exactly for arbitrary globs is hard, so
this check is *conservative*: it may report overlap where none exists (two tasks then
serialize, which only costs throughput), but it never misses a real overlap (which
would let two in-flight workers edit the same file).
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

_WILDCARDS = frozenset("*?[")


def _segments(glob: str) -> list[str]:
    return [s for s in glob.strip("/").split("/") if s not in ("", ".")]


def _has_wildcard(segment: str) -> bool:
    return any(c in _WILDCARDS for c in segment)


def globs_overlap(a: str, b: str) -> bool:
    """Return True if some path could match both globs `a` and `b` (conservatively)."""
    sa, sb = _segments(a), _segments(b)
    for x, y in zip(sa, sb, strict=False):
        if x == "**" or y == "**":
            return True
        wx, wy = _has_wildcard(x), _has_wildcard(y)
        if wx and wy:
            continue  # two wildcard segments: assume they can meet
        if wx and not fnmatchcase(y, x):
            return False
        if wy and not fnmatchcase(x, y):
            return False
        if not wx and not wy and x != y:
            return False
    if len(sa) == len(sb):
        return True
    # The longer glob continues past the shorter one. They meet only if the rest of
    # the longer one is a single `**` (which also matches the directory itself).
    rest = (sa if len(sa) > len(sb) else sb)[min(len(sa), len(sb)):]
    return rest == ["**"]


def _match(path: list[str], glob: list[str]) -> bool:
    if not glob:
        return not path
    if glob[0] == "**":
        return any(_match(path[i:], glob[1:]) for i in range(len(path) + 1))
    return bool(path) and fnmatchcase(path[0], glob[0]) and _match(path[1:], glob[1:])


def path_in_scope(path: str, scope: Iterable[str]) -> bool:
    """True if the concrete repo-relative `path` matches some glob in `scope`.

    Same glob semantics as `globs_overlap` (`*` stays within a segment, `**` spans any
    number of them, including none). The review gate uses it to reject a diff that
    edits outside its task's file_scope (invariant 3).
    """
    segs = _segments(path)
    return any(_match(segs, _segments(g)) for g in scope)


def scopes_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    """Return True if any glob in scope `a` overlaps any glob in scope `b`."""
    b = list(b)
    return any(globs_overlap(x, y) for x in a for y in b)
