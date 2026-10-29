# greeter: the two v1 cases, run with real agents

A four-file repo (`lib.greet`, an app, two tests) and two plans:

| plan | tasks | expected outcome |
|---|---|---|
| `case1.json` | `shout.py` and `wordcount.py`, disjoint scopes | both `merged`, zero human touch |
| `case2.json` | rename `lib.greet` to `welcome` + a new `farewell.py` that still calls `greet` | one `merged`, the other `needs_human` (caught by the merge-time full suite) |

Case 2's plan is *deliberately* faulty: the lead hands one task a contract change and
the other the stale contract. The scopes are disjoint, so the claim lets them run in
parallel and each passes its own tests; only the merged tree breaks. The point is that
SEAM catches it and escalates instead of merging a broken main.

This is a toy repo. Passing here shows the mechanism works end to end; it is **not**
the escalation-rate benchmark, which needs a real codebase.

## Run it

```sh
# 0. once: a Docker daemon and the sandbox image (python + pytest + Claude Code)
colima start
docker build -t seam-sandbox sandbox/

# 1. auth for the workers. Set exactly ONE of these, by env var only. It is handed
#    to SEAM's credential proxy; worker containers only ever see a placeholder.
claude setup-token                      # prints a long-lived token
read -s "CLAUDE_CODE_OAUTH_TOKEN?token: " && export CLAUDE_CODE_OAUTH_TOKEN
#    (or ANTHROPIC_API_KEY; the reviewer runs on the host with tools disabled,
#    using your normal login)

# 2. paths must live under $HOME: colima shares only $HOME with containers
examples/greeter/make_repo.sh ~/seam-demo/case1
.venv/bin/python cli.py run --repo ~/seam-demo/case1 --plan examples/greeter/case1.json \
    --state-dir ~/seam-demo/case1-state --budget-usd 3

examples/greeter/make_repo.sh ~/seam-demo/case2
.venv/bin/python cli.py run --repo ~/seam-demo/case2 --plan examples/greeter/case2.json \
    --state-dir ~/seam-demo/case2-state --budget-usd 3

# 3. look at what happened
.venv/bin/python cli.py status  --state-dir ~/seam-demo/case2-state
.venv/bin/python cli.py inspect --state-dir ~/seam-demo/case2-state --task 2
git -C ~/seam-demo/case1 log --oneline --graph main
```

Each goal loads once per state dir: re-running `run --plan` there is refused. To
resume an interrupted run, call `run` without `--plan`; to start over, delete the repo
and state dirs and run `make_repo.sh` again.
