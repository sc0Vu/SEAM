"""Replay merged boltons fixes as SEAM goals, judged by the maintainers' own tests.

Each goal in goals.json names a merged boltons commit (`fix`) and the contributor
pull requests it took in. The goal text is those PRs' titles and change summaries,
taken mechanically (see `change_section`); SEAM starts from the fix's parent; the
fix commit's own test files are the hidden check, run only after SEAM has merged.

    replay.py fetch                       # PR texts -> prs/<n>.json (cached)
    replay.py goal <id>                   # print the goal text
    replay.py prepare <id> <dest> [--source URL_OR_PATH]
    replay.py check <id> <repo> [--target master] [--scratch DIR]

`check` runs every test in the fix's test files on three trees, all in the sandbox
with no network: the fix's parent, the fix, and SEAM's result, each with the fix's
test files laid over it. Target tests are those that fail on the parent and pass on
the fix. A target test that fails on SEAM's result is a silent defect if SEAM
merged that work; a test that passes on both parent and fix but fails on SEAM's
result is a regression. A failure can also mean SEAM chose a different but valid
API, so failures are listed for a human to classify, never counted blindly.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from seam_merge.engine.git import GitRepo
from seam_merge.sandbox.docker import DEFAULT_IMAGE, DockerSandbox, host_user

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "goals.json").read_text())
PR_DIR = HERE / "prs"
JUNIT = ".replay-junit.xml"
_DROP_HEADINGS = ("the backstory", "ai assistance")


# -- goal text --------------------------------------------------------------------

def goal(goal_id: str) -> dict:
    for g in MANIFEST["goals"]:
        if g["id"] == goal_id:
            return g
    raise SystemExit(f"no goal {goal_id!r}; have {[g['id'] for g in MANIFEST['goals']]}")


def change_section(body: str) -> str:
    """The part of a PR description that describes the change, by a fixed rule:
    the `## The change` section if there is one; otherwise every section except
    `## The backstory` and `## AI assistance` (how the PR was made, not what)."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            sections.append((line[3:].strip().lower(), []))
        else:
            sections[-1][1].append(line)
    named = {title: lines for title, lines in sections if title}
    if "the change" in named:
        keep = [named["the change"]]
    else:
        keep = [lines for title, lines in sections if title not in _DROP_HEADINGS]
    return "\n".join("\n".join(lines).strip() for lines in keep if any(lines)).strip()


def goal_text(g: dict) -> str:
    """The prose goal SEAM's planner receives. Same wrapper for every goal."""
    parts = ["Fix the following problems in boltons, as described by contributors. "
             "Add or update tests for each fix.\n"]
    for n in g["prs"]:
        pr = json.loads((PR_DIR / f"{n}.json").read_text())
        parts.append(f"### {pr['title']} (boltons#{n})\n\n{change_section(pr['body'])}\n")
    parts.append(f"The full test suite ({MANIFEST['suite_cmd']}) must stay green.")
    return "\n".join(parts)


def fetch() -> None:
    """Download each PR's title and body (public data) once. GITHUB_TOKEN, if set,
    only raises the API rate limit; it is sent as a header and never stored."""
    PR_DIR.mkdir(exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN")
    for n in sorted({n for g in MANIFEST["goals"] for n in g["prs"]}):
        out = PR_DIR / f"{n}.json"
        if out.exists():
            continue
        req = urllib.request.Request(f"https://api.github.com/repos/mahmoud/boltons/issues/{n}",
                                     headers={"Accept": "application/vnd.github+json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        d = json.load(urllib.request.urlopen(req, timeout=30))
        out.write_text(json.dumps({"number": n, "title": d["title"], "body": d.get("body") or "",
                                   "url": d["html_url"], "is_pr": "pull_request" in d},
                                  indent=2, ensure_ascii=False) + "\n")
        print(f"fetched #{n}: {d['title']}")


# -- repos --------------------------------------------------------------------------

def git(repo: Path | str, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def prepare(g: dict, dest: Path, source: str) -> None:
    """Clone `source` to `dest` with `master` at the fix's parent, HEAD detached."""
    if dest.exists():
        raise SystemExit(f"refusing: {dest} exists")
    subprocess.run(["git", "clone", "-q", source, str(dest)], check=True)
    base = git(dest, "rev-parse", f"{g['fix']}^")
    git(dest, "checkout", "-q", "-B", "master", base)
    git(dest, "checkout", "-q", "--detach")
    print(f"{g['id']}: {dest} at {base[:10]} (parent of {g['fix']}); master is free for SEAM")


# -- the hidden check -----------------------------------------------------------------

def outcomes(junit_xml: str) -> dict[str, str]:
    """`test id -> passed | failed | skipped` from a pytest JUnit report. A
    collection error has no testcase per test, so it appears as one failed entry."""
    out = {}
    for case in ET.fromstring(junit_xml).iter("testcase"):
        tid = f"{case.get('classname')}::{case.get('name')}"
        kinds = {child.tag for child in case}
        out[tid] = ("failed" if kinds & {"failure", "error"} else
                    "skipped" if "skipped" in kinds else "passed")
    return out


def compare(parent: dict[str, str], fix: dict[str, str], seam: dict[str, str]) -> dict:
    """Target tests fail on the parent and pass on the fix; kept tests pass on both.
    A test missing from SEAM's run (e.g. its module failed to import) counts as failed."""
    target = sorted(t for t, o in fix.items() if o == "passed" and parent.get(t) != "passed")
    kept = sorted(t for t, o in fix.items() if o == "passed" and parent.get(t) == "passed")
    return {
        "target": len(target),
        "target_passed": sum(seam.get(t) == "passed" for t in target),
        "target_failed": [t for t in target if seam.get(t) != "passed"],
        "regressions": [t for t in kept if seam.get(t) != "passed"],
    }


def test_files(repo: Path, fix: str) -> list[str]:
    changed = git(repo, "diff", "--name-only", f"{fix}^", fix).splitlines()
    return [f for f in changed if f.startswith("tests/") and f.endswith(".py")
            and git(repo, "cat-file", "-t", f"{fix}:{f}") == "blob"]


def run_overlaid(repo: Path, ref: str, fix: str, files: list[str], scratch: Path,
                 sandbox: DockerSandbox) -> dict[str, str]:
    """Run the fix's test files on `ref`'s tree with those files laid over it."""
    with tempfile.TemporaryDirectory(prefix="replay-", dir=scratch) as d:
        GitRepo(repo).export(git(repo, "rev-parse", ref), Path(d))
        for f in files:
            (Path(d) / f).parent.mkdir(parents=True, exist_ok=True)
            (Path(d) / f).write_bytes(subprocess.run(
                ["git", "-C", str(repo), "show", f"{fix}:{f}"], check=True,
                capture_output=True).stdout)
        sandbox.run(f"python -m pytest -q -p no:cacheprovider --junitxml={JUNIT} "
                    + " ".join(files), Path(d), timeout=600)
        report = Path(d) / JUNIT
        if not report.exists():
            raise SystemExit(f"no test report for {ref}: the sandbox run failed to start")
        return outcomes(report.read_text())


def check(g: dict, repo: Path, target: str, scratch: Path, image: str) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    sandbox = DockerSandbox(image, user=host_user())
    fix, files = g["fix"], test_files(repo, g["fix"])
    cache = scratch / f"{g['id']}-baseline.json"
    if cache.exists():
        base = json.loads(cache.read_text())
    else:
        base = {"parent": run_overlaid(repo, f"{fix}^", fix, files, scratch, sandbox),
                "fix": run_overlaid(repo, fix, fix, files, scratch, sandbox)}
        cache.write_text(json.dumps(base))
    seam = run_overlaid(repo, target, fix, files, scratch, sandbox)
    result = {"goal": g["id"], "fix": fix, "files": files,
              **compare(base["parent"], base["fix"], seam)}
    print(json.dumps(result, indent=2))
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("goal").add_argument("id")
    pr = sub.add_parser("prepare")
    pr.add_argument("id")
    pr.add_argument("dest", type=Path)
    pr.add_argument("--source", default=MANIFEST["repo"])
    ch = sub.add_parser("check")
    ch.add_argument("id")
    ch.add_argument("repo", type=Path)
    ch.add_argument("--target", default="master")
    ch.add_argument("--image", default=DEFAULT_IMAGE)
    ch.add_argument("--scratch", type=Path, default=Path.home() / "seam-demo" / "replay-scratch",
                    help="must be visible to containers (colima shares only $HOME)")
    a = p.parse_args(argv)
    if a.cmd == "fetch":
        fetch()
    elif a.cmd == "goal":
        print(goal_text(goal(a.id)))
    elif a.cmd == "prepare":
        prepare(goal(a.id), a.dest.expanduser().resolve(), a.source)
    else:
        check(goal(a.id), a.repo.expanduser().resolve(), a.target,
              a.scratch.expanduser().resolve(), a.image)
    return 0


if __name__ == "__main__":
    sys.exit(main())
