# boltons replay: the LLM lead on goals SEAM's author did not write

Seven merged boltons commits from the October 2026 release sweep (`goals.json`),
each replayed as a SEAM goal:

- **Goal text:** the contributor pull requests each commit took in, title plus the
  change summary, extracted by a fixed rule (`replay.py:change_section`) and
  wrapped the same way every time. The exact texts are in `goal-texts/`.
- **Start:** the fix commit's parent, `master` free for SEAM (`replay.py prepare`).
- **Hidden check:** the fix commit's own test files, written or merged by the boltons
  maintainers, run on SEAM's result only after it merged (`replay.py check`).

| id | commit | PRs | shape | hidden target tests |
|---|---|---|---|---|
| r1-excutils | `2ecf547` | 1 | single fix | 1 |
| r2-omd-extend | `8a49b74` | 1 | single fix; a vendored copy lives in urlutils | 2 |
| r3-rotate | `1c2db46` | 1 | single fix | 3 |
| r4-statsutils | `2873144` | 3 | several fixes, one module, two touch the same cache | 3 |
| r5-setutils | `40fffc5` | 6 | several fixes, one module | 8 |
| r6-file-path-named | `922e7b2` | 7 | three modules (plus a packaging item) | 7 |
| r7-gc-type-debug | `f898d8f` | 4 | three modules | 4 |

Controls, run before any agent: the suite is green at every starting commit; the
parent scores 0 on every goal's target tests and the fix scores all of them, with
no regressions.

**What this measures, and what it doesn't.**
- Independent of SEAM's author: the goals, the code base and the judging tests.
- The PRs were themselves made by AI agents (Codex and others, as their
  descriptions say) and describe the fix, not only the symptom. So this measures
  planning, implementation, review and integration given a described change, not
  finding a bug from a report.
- A failing target test can mean a silent defect, or that SEAM chose a different
  valid API. Failures are listed for a human to classify, never counted blindly.
- Seven goals is a small sample: a data point, not a rate.

## Run one

```sh
git clone https://github.com/mahmoud/boltons.git ~/seam-demo/replay-src  # full history
python examples/boltons-replay/replay.py prepare r4-statsutils ~/seam-demo/replay/r4 \
    --source ~/seam-demo/replay-src
seam plan --repo ~/seam-demo/replay/r4 --target master \
    --goal "$(cat examples/boltons-replay/goal-texts/r4-statsutils.txt)" \
    --out ~/seam-demo/replay/r4-plan.json --state-dir ~/seam-demo/replay/r4-state
seam run --repo ~/seam-demo/replay/r4 --plan ~/seam-demo/replay/r4-plan.json \
    --state-dir ~/seam-demo/replay/r4-state --target master --workers 2 \
    --suite-cmd "python -m pytest -q --doctest-modules boltons tests"
python examples/boltons-replay/replay.py check r4-statsutils ~/seam-demo/replay/r4
```

`replay.py fetch` re-downloads the PR texts into `prs/` (set `GITHUB_TOKEN` to avoid
the API's anonymous rate limit). Results are recorded in `doc/architecture.md` §12.
