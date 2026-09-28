# pokescanner — status

Read this first. Plan: `docs/POKESCANNER-PLAN.md`. Workflow: `docs/agent-workflow.md`.

## Wave 1 (started 2026-09-28 ~23:00, owner asleep)

| Lane | Repo / branch | Scope | State |
|---|---|---|---|
| serve | pokescan `serve` (worktree `/srv/repos/pokescan-wt/serve`) | `pokescan.serve` FastAPI: /health /bundle /identify /ref /trace, `result_to_dict`, quad capture, names subset, tests, host smoke on :8765 | running |
| ct | denils `pokescanner-host` | NixOS LXC `pokescanner`, /tank/pokescan/{src,bundle-v12} ro mounts, systemd unit, agenix token, gatus/homepage | running |
| matcher | pokecollector `lane/matcher` | `external` provider, queue integration, session_lang, trace blob, `/matcher` + `/settings/scanner/external` endpoints, tests | running |

Not started: debug panel + settings UI (§5.3), `/live` P1 page (§7), scan.nilss.dev proxy + Authentik app (owner TODO unless the ct lane could express it in code).

## Merged

(nothing yet)

## Owner TODOs

(filled in from lane reports)
