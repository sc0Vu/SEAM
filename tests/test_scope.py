import pytest

from engine.scope import globs_overlap, scopes_overlap


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
