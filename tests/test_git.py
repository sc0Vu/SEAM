import pytest

from engine.errors import GitError
from engine.git import GitRepo
from tests.gitfixture import branch_with, commit_files, git, make_repo


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path, {"a.py": "A = 1\n", "b.py": "B = 1\n"})


def test_clean_merge_tree_then_commit_and_export(repo, tmp_path):
    g = GitRepo(repo)
    branch_with(repo, "t1", {"a.py": "A = 2\n"})
    branch_with(repo, "t2", {"b.py": "B = 2\n"})
    base = g.rev_parse("main")
    first = g.merge_tree(base, "t1")
    assert first.conflicts == ()
    m1 = g.commit_tree(first.tree, [base, g.rev_parse("t1")], "merge t1")
    second = g.merge_tree(m1, "t2")
    assert second.conflicts == ()
    m2 = g.commit_tree(second.tree, [m1, g.rev_parse("t2")], "merge t2")

    out = tmp_path / "out"
    out.mkdir()
    g.export(m2, out)
    assert (out / "a.py").read_text() == "A = 2\n"
    assert (out / "b.py").read_text() == "B = 2\n"
    assert not (out / ".git").exists()


def test_textual_conflict_names_the_files(repo):
    g = GitRepo(repo)
    branch_with(repo, "t1", {"a.py": "A = 'branch'\n"})
    branch_with(repo, "other", {"a.py": "A = 'main'\n"})
    git(repo, "update-ref", "refs/heads/main", g.rev_parse("other"))
    assert g.merge_tree(g.rev_parse("main"), "t1").conflicts == ("a.py",)


def test_update_branch_is_compare_and_swap(repo):
    g = GitRepo(repo)
    base = g.rev_parse("main")
    new = branch_with(repo, "t1", {"a.py": "A = 2\n"})
    assert not g.update_branch("main", new, expected=new)  # wrong expectation
    assert g.rev_parse("main") == base
    assert g.update_branch("main", new, expected=base)
    assert g.rev_parse("main") == new


def test_is_ancestor(repo):
    g = GitRepo(repo)
    base = g.rev_parse("main")
    side = branch_with(repo, "t1", {"a.py": "A = 2\n"})
    assert g.is_ancestor(base, "t1")
    assert not g.is_ancestor(side, "main")


def test_checked_out_in(repo, tmp_path):
    g = GitRepo(repo)
    assert g.checked_out_in("main") == []
    wt = tmp_path / "task-wt"
    g.add_worktree(wt, "seam/task-1", "main")
    assert g.checked_out_in("seam/task-1") == [str(wt.resolve())]
    commit_files(wt, {"c.py": "C = 1\n"})
    g.remove_worktree(wt)
    assert g.checked_out_in("seam/task-1") == []


def test_unknown_ref_raises(repo):
    with pytest.raises(GitError):
        GitRepo(repo).rev_parse("nope")
