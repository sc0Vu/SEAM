import pytest

from engine.scope import globs_overlap, path_in_scope, scopes_overlap


@pytest.mark.parametrize(
    "a, b",
    [
        ("auth/**", "auth/login.py"),
        ("auth/**", "auth"),
        ("api/users.*", "api/users.py"),
        ("api/*.py", "api/users.py"),
        ("**", "anything/at/all.py"),
        ("src/a.py", "./src/a.py"),
        ("api/*.py", "api/*.js"),  # conservative: two wildcard segments assumed to meet
    ],
)
def test_overlapping(a, b):
    assert globs_overlap(a, b)
    assert globs_overlap(b, a)


@pytest.mark.parametrize(
    "a, b",
    [
        ("auth/**", "billing/**"),
        ("api/users.py", "api/orders.py"),
        ("api/users.*", "api/orders.py"),
        ("api/*", "api/v2/users.py"),  # `*` does not cross a directory
        ("src/a.py", "src/a.py/b"),
    ],
)
def test_disjoint(a, b):
    assert not globs_overlap(a, b)
    assert not globs_overlap(b, a)


def test_scope_lists():
    assert scopes_overlap(["a/**", "b/x.py"], ["c/**", "b/*.py"])
    assert not scopes_overlap(["a/**"], ["b/**", "c/**"])
    assert not scopes_overlap([], ["a/**"])


@pytest.mark.parametrize(
    "path, scope, inside",
    [
        ("auth/login.py", ["auth/**"], True),
        ("auth/deep/x/y.py", ["auth/**"], True),
        ("auth.py", ["auth/**"], False),
        ("api/users.py", ["api/users.*"], True),
        ("api/v2/users.py", ["api/*.py"], False),
        ("a/b/c/test_x.py", ["a/**/test_*.py"], True),
        ("a/test_x.py", ["a/**/test_*.py"], True),  # ** may span zero segments
        ("lib.py", ["lib.py", "app.py"], True),
        ("other.py", ["lib.py", "app.py"], False),
        ("x.py", [], False),
    ],
)
def test_path_in_scope(path, scope, inside):
    assert path_in_scope(path, scope) is inside


def test_path_in_scope_never_contradicts_overlap():
    # A path inside a scope must make that scope overlap the path's own literal scope.
    for path, scope in [("auth/x.py", ["auth/**"]), ("api/u.py", ["api/*.py"])]:
        assert path_in_scope(path, scope) and scopes_overlap(scope, [path])
