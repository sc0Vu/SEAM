"""The existing-test guard's detector (seam_merge/reviewer/existing_tests.py)."""

import pytest

from seam_merge.reviewer.existing_tests import changed_tests, is_test_file

P = "tests/test_f.py"

# The boltons replay's r3 (doc §12): the original test, and SEAM's rewrite of it that
# let a behavior change (a missing file now raises) pass the full suite.
R3_BEFORE = '''
def test_rotate_file_no_rotation(tmp_path):
    file_path = tmp_path / 'test_file.txt'
    fileutils.rotate_file(file_path)
    assert not file_path.exists()
'''
R3_AFTER = '''
def test_rotate_file_no_rotation(tmp_path):
    file_path = tmp_path / 'test_file.txt'
    with pytest.raises(FileNotFoundError):
        fileutils.rotate_file(file_path)
    assert list(tmp_path.iterdir()) == []
'''


def test_the_r3_rewrite_is_caught():
    assert changed_tests(P, R3_BEFORE, R3_AFTER) == {f"{P}::test_rotate_file_no_rotation":
                                                     "changed"}


def test_adding_assertions_or_tests_is_not_a_change():
    stronger = R3_BEFORE + "    assert list(tmp_path.iterdir()) == []\n"
    new_test = R3_BEFORE + "\ndef test_more():\n    assert 1\n"
    assert changed_tests(P, R3_BEFORE, stronger) == {}
    assert changed_tests(P, R3_BEFORE, new_test) == {}


def test_formatting_and_comments_do_not_count():
    reformatted = R3_BEFORE.replace("'test_file.txt'", '"test_file.txt"').replace(
        "    fileutils.rotate_file(file_path)",
        "    # rotate it\n    fileutils.rotate_file(  file_path  )")
    assert changed_tests(P, R3_BEFORE, reformatted) == {}


def test_editing_an_assertion_is_a_change():
    weaker = R3_BEFORE.replace("assert not file_path.exists()", "assert True")
    assert changed_tests(P, R3_BEFORE, weaker) == {f"{P}::test_rotate_file_no_rotation":
                                                   "changed"}


def test_removed_renamed_or_deleted_tests():
    two = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 2\n"
    assert changed_tests(P, two, "def test_a():\n    assert 1\n") == {f"{P}::test_b": "removed"}
    renamed = two.replace("test_b", "test_b2")
    assert changed_tests(P, two, renamed) == {f"{P}::test_b": "removed"}
    assert changed_tests(P, two, None) == {f"{P}::test_a": "removed", f"{P}::test_b": "removed"}
    assert changed_tests(P, two, "def test_a(:\n") == {f"{P}::test_a": "removed",
                                                         f"{P}::test_b": "removed"}


def test_class_methods_and_decorators():
    before = ('class TestX:\n'
              '    @pytest.mark.parametrize("n", [1, 2])\n'
              '    def test_n(self, n):\n        assert n\n'
              '    def helper(self):\n        return 1\n')
    fewer_cases = before.replace("[1, 2]", "[1]")
    helper_edit = before.replace("return 1", "return 2")
    assert changed_tests(P, before, fewer_cases) == {f"{P}::TestX::test_n": "changed"}
    assert changed_tests(P, before, helper_edit) == {}  # not a test function


def test_unparseable_original_has_nothing_to_protect():
    assert changed_tests(P, "def test_a(:\n", "") == {}


@pytest.mark.parametrize("path, expected", [
    ("tests/test_x.py", True), ("x_test.py", True), ("pkg/test_x.py", True),
    ("tests/conftest.py", False), ("tests/helpers.py", False), ("test_x.txt", False),
])
def test_is_test_file(path, expected):
    assert is_test_file(path) is expected
