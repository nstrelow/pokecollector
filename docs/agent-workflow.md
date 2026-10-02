# How agents work through the pokescanner plan

Same shape as the wedding-site workflow, without GitHub issues: the backlog is
**`docs/POKESCANNER-PLAN.md` §8** and the running state is **`docs/status.md`** (the
orchestrator updates it after every merge). One orchestrator (interactive session)
dispatches workers; each worker takes one lane, works in an isolated git worktree on its
own branch, and reports with evidence. A verifier (a different agent) checks the branch
before the orchestrator merges.

```
lane in status.md ─► worker (worktree, branch lane/<name>) ─► report ─► verifier ─► merge
                                                                   │ FAIL (≤2 rounds) ─► fixer
```

## Rules

- **No two lanes in one wave touch the same files.** Lanes span three repos:
  `pokescan` (the service), `denils` (the container), `pokecollector` (this repo).
- A worker never asks the owner questions. If a decision is missing it takes the
  plan's recommendation, writes the assumption into its report, and continues.
- A worker never touches OPNsense, Authentik, or production containers by hand; anything
  that is not expressible as code in the repo goes into the report as an owner TODO.
- Every behaviour change gets a test. Reports contain commands + outputs, not "looks fine".
- Worker git: commit on the lane branch, never push to `main`/`master`/`plan-a`. The
  orchestrator merges (fast-forward or merge, never force) and pushes.
- Models: pipeline/judgement work runs on Opus; don't downgrade to save tokens.

## Lane environment

- `pokescan`: worktree under `/srv/repos/PicaLens/scanner/worktrees/<lane>` from `origin/plan-a`
  (8f0ff3b pinned for the PoC); `data/` is symlinked from the main checkout; run with
  `PYTHONPATH=$PWD/src` and the venv at `/srv/repos/PicaLens/scanner/pokescan/.venv`.
- `denils`: work on a branch in `/root/denils` (always `git pull` first, push after);
  new host = `provision-<host>`, existing host = `update-<host>` (see memory
  `denils-terraform-drift-trap`).
- `pokecollector`: worktree under `.claude/worktrees/`, branch from `pokescanner`.
  Backend tests: `cd backend && python -m pytest`. Frontend: `cd frontend && npm test`.

## Report format

What was built (files), how it was verified (commands, outputs, measured numbers such as
latency and RSS), what is left open, and assumptions taken.
