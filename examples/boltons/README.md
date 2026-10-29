# boltons-bench-1: SEAM on a real codebase

[boltons](https://github.com/mahmoud/boltons) pinned at `d31d821`: ~30 utility modules,
775 tests (doctests included) that pass offline in the sandbox in ~8 s. `plan.json` has
8 feature tasks, one per module, each scoped to the module and its test file.

**What this measures, and what it doesn't.**
- The tasks and the plan were written by the same AI assistant (Claude) that built
  SEAM, as a maintainer would write feature requests. They were not shaped to avoid or
  provoke conflicts, but that is a claim, not a control: the result is **not an
  independent benchmark**.
- The scopes are disjoint, so the claim lets every task run in parallel; any
  escalation comes from the review loop (3 attempts) or the merge-time full suite.
- 8 tasks is a small sample. Read the escalation rate as one data point.

## Run it

```sh
examples/boltons/make_repo.sh ~/seam-demo/bench1
.venv/bin/python cli.py run --repo ~/seam-demo/bench1 --plan examples/boltons/plan.json \
    --state-dir ~/seam-demo/bench1-state --target master --budget-usd 3 --workers 2 \
    --suite-cmd "python -m pytest -q --doctest-modules boltons tests"
.venv/bin/python cli.py status --state-dir ~/seam-demo/bench1-state
```

Results are recorded in `doc/architecture.md` §8.
