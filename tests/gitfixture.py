"""Helpers for building throwaway git repos in tests."""

import subprocess
from pathlib import Path

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], env=ENV, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], msg: str = "c") -> str:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A repo with `files` committed on `main`, HEAD detached so `main` can move."""
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_files(repo, files, "init")
    git(repo, "checkout", "-q", "--detach")
    return repo


def branch_with(repo: Path, branch: str, files: dict[str, str], start: str = "main") -> str:
    """Create `branch` from `start` with `files` committed, without moving HEAD."""
    wt = repo.parent / f"wt-{branch.replace('/', '-')}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(wt), start)
    sha = commit_files(wt, files, f"work on {branch}")
    git(repo, "worktree", "remove", "--force", str(wt))
    return sha
