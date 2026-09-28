# pokescanner — status

Read this first. Plan: `docs/POKESCANNER-PLAN.md`. Workflow: `docs/agent-workflow.md`.

## Wave 1 (started 2026-09-28 ~23:00, owner asleep)

| Lane | Repo / branch | Scope | State |
|---|---|---|---|
| serve | pokescan `serve` (worktree `/srv/repos/pokescan-wt/serve`, 4fa7053) | `pokescan.serve` FastAPI: /health /bundle /identify /ref /trace, `result_to_dict`, `Result.quad`, names subset, ref thumbs, `export_serve_bundle.py`, 17 tests (826 total) | **done**, running on host :8765 (PID 1585092). No downscale (1600 px cost 2 correct frames in A/B). RSS 1.15–1.3 GB, p50 ~1 s at load 40 |
| ct | denils `pokescanner-host` | NixOS LXC `pokescanner`, /tank/pokescan/{src,bundle-v12} ro mounts, systemd unit, agenix token, gatus/homepage | running (told to use `export_serve_bundle.py` from the serve worktree — v12 assets live in `pokescan-wt-r2/data`, not the main checkout) |
| matcher | pokecollector `lane/matcher` (52c5068) | `external` provider, queue integration, session_lang, trace blob, `/matcher` + `/settings/scanner/external`, ref proxy, 52 tests (1017 total) | **done**, merged |
| ui | pokecollector `lane/ui` (65c9d30) | `MatcherDebugPanel`, external-matcher settings block, en/de session toggle, live link, 18 tests (335 total) | **done**, merged |
| verify-1 | pokecollector `pokescanner` | both suites, frontend↔backend field cross-check, ref-proxy security, timings bar, Docker build arg | running |
| live | pokescan `live` (from `serve`) | `/live` P1 camera page + consensus + `/collection/add` proxy | running |

Owner TODO regardless: scan.nilss.dev proxy + Authentik app (unless the ct lane could express it in code); set `EXTERNAL_MATCHER_URL/TOKEN` on CT 100 (Komodo) and pick "external" once in Scanner Settings → Test → Save.

## Merged

- `lane/matcher` 52c5068 + `lane/ui` 65c9d30 → `pokescanner` 91e542f (pushed), verifier pending.

## Owner TODOs

(filled in from lane reports)
