# pokescanner — status

Read this first. Plan: `docs/POKESCANNER-PLAN.md`. Workflow: `docs/agent-workflow.md`.

## State on 2026-09-29 ~01:00 (end of the overnight run)

**P1 is built and the scanner service is live on CT 140.** What is NOT done is the one
step the permission system blocked: deploying this branch to the live pokecollector on
CT 100 (recipe below, owner runs it), plus the proxy/Authentik GUI work.

### Working now

- **pokescanner service**: CT 140 `pokescanner` @ `http://10.0.1.40:8000`, NixOS via denils
  (`main`, merged from `pokescanner-host`), source = pokescan branch `live` 0b6b3f3
  (bind mount `/tank/pokescan/src`, ro), bundle v12 + names + 60,600 ref thumbs =
  1.6 GB at `/tank/pokescan/bundle-v12` (ro), token in agenix `secrets/pokescanner-env.age`
  → `/run/agenix/pokescanner-env` in the CT. `/health` ok, `/identify` verified with real
  wild photos through the container (Dark Haunter IDENTIFIED 0.873; Ampharos and Squirtle
  correctly AMBIGUOUS with the right family in the top-3), `/trace/<id>/plane.webp` served,
  `/live` page 200, `/ref` thumbnails 200. Server time 1.3–2.1 s per frame while the
  Proxmox host runs the cardphotos re-scan at load ~40; 1.1 GB RSS.
- **pokescan repo**: branches `serve` (28e3ac0) and `live` (0b6b3f3, contains serve) pushed
  to origin, NOT merged into plan-a/master (another session works there). 830 tests + 35
  serve/live tests + 12 node consensus tests. The inference path is torch-free now
  (`models/letterbox.py`).
- **pokecollector branch `pokescanner`** (this repo, `fork/pokescanner` d9600cf + this
  commit): backend `external` provider + frontend debug panel + settings block + en/de
  session toggle + live link; verified (1018 backend / 337 frontend tests, security pass,
  Docker build arg). Not deployed.

### Owner TODOs, in order

1. **Deploy the branch to CT 100** — the classifier refused to let an agent do a production
   deploy. Recipe (from the Proxmox host; `TS=$(date +%Y%m%d-%H%M)`):
   0. Docker on CT 100 has been STOPPED since 2026-09-27 13:51 UTC (containers survive via
      live-restore; komodo-mongo is probably wedged on stdout): `pct exec 100 -- rc-service
      docker start`, then `docker ps`. If komodo-mongo stays unresponsive, restart just it.
   1. Backups: `docker exec pokemon-postgres pg_dump -U pokemon -Fc pokemon_tcg >
      /root/pokecollector-backups/pre-pokescanner-$TS.dump`; `tar czf
      /root/pokecollector-backups/stack-src-pre-pokescanner-$TS.tgz --exclude=pokecollector/data
      --exclude=pokecollector/backups pokecollector` (from /etc/komodo/stacks); `docker tag
      pokecollector-backend:latest pokecollector-backend:pre-pokescanner` (+ frontend).
   2. `git -C /srv/repos/pokecollector archive --format=tar.gz -o /tmp/pokescanner.tgz
      fork/pokescanner && pct push 100 /tmp/pokescanner.tgz /tmp/pokescanner.tgz`.
   3. In `/etc/komodo/stacks/pokecollector`: `rm -rf backend frontend && tar xzf
      /tmp/pokescanner.tgz` (keep `.env`, `compose.tz.yml`, `data/`, `backups/`, `.git`).
   4. Append to `.env` (back it up first): `EXTERNAL_MATCHER_URL=http://10.0.1.40:8000`,
      `EXTERNAL_MATCHER_TOKEN=<ssh root@10.0.1.40 cat /run/agenix/pokescanner-env>`,
      `EXTERNAL_MATCHER_LABEL=pokescan`, `EXTERNAL_MATCHER_TIMEOUT=20`,
      `LIVE_SCANNER_URL=https://scan.nilss.dev/live`.
   5. `docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f
      compose.tz.yml build && … up -d` — always all three files (the plain compose file now
      points at upstream ghcr images). Version jump 1.42.2 → 1.51.0, migrations are additive,
      the backend writes its pre-upgrade backup to `./backups`.
   6. Check `https://poke.nilss.dev/api/health`, then Scanner Settings → provider
      "external" (pokescan) → Test → Save.
   7. Don't let Komodo redeploy/pull this stack (its `file_paths` lack the build file); fix
      `config.file_paths` in Mongo to include `docker-compose.build.yml` per `host.md`.
   Rollback: compose down (no `-v`), untar the stack-src backup, retag `pre-pokescanner` →
   `latest`, `up -d --no-build`; DB restore from the dump only if the old code fails.
2. **OPNsense os-caddy** (GUI): `scan.nilss.dev` → `http://10.0.1.40:8000`; forward-auth to
   the Authentik embedded outpost on `/live*` and `/collection/*` only.
3. **Authentik** (GUI): Proxy provider (forward-auth, single app) for
   `https://scan.nilss.dev` + application + embedded outpost.
4. **Live-page adds**: set `POKESCANNER_COLLECTION_URL=http://10.0.1.10:8000`,
   `POKESCANNER_COLLECTION_USERNAME/PASSWORD` (a pokecollector user; JWT is 7 d so the
   service re-logs in) and `POKESCANNER_COLLECTION_UI_URL=https://poke.nilss.dev/scans` in
   the agenix env secret + `pokescan-serve.nix`, then `update-pokescanner`.
5. **Try it on a real phone** (iOS Safari / Android Chrome) — the page was only run in
   headless Chromium with a faked camera. Then scan ~20 of your PRE/full-art cards and note
   latency + wrong results here.
6. pokescan: decide whether `live` merges into plan-a (it's a pure addition + the torch-free
   refactor; 830 tests green).

### Lane log (wave 1)

| Lane | Branch | Result |
|---|---|---|
| serve | pokescan `serve` 28e3ac0 | service, `Result.quad`, names subset, ref thumbs, `export_serve_bundle.py`, torch-free inference; no downscale (1600 px cost 2/137 frames) |
| live | pokescan `live` 0b6b3f3 | `/live` page + consensus + `/collection/add` (login/JWT proxy); headless run fired on frame 3 |
| ct | denils `main` | CT 140, unit, path watcher, agenix, gatus + homepage entries, firewall |
| matcher | pokecollector | `external` provider, queue, `session_lang`, trace blob, `/matcher`, ref proxy |
| ui | pokecollector | `MatcherDebugPanel`, settings block, toggle, live link |
| verify-1 | pokecollector | 5/5 pass; fixed trace-id `..`, token redaction in traces, `endpoint: null`, timings remainder, Docker build arg |
| deploy-recon | — | CT 100 deploy recipe above; found Docker stopped |

## P2 / P3

Not started. Plan §7. P2 = weight-only int8 CLIP export (`export_int8_weightonly.py`) +
gate re-run + onnxruntime-web port of S1/rectify/CLIP preprocessing, hybrid retrieval via
`POST /identify` JSON mode (still to add on the server).
