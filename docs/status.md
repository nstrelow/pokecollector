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

### Deployed 2026-09-29 09:15–09:30 (owner said "deploy it yourself")

- CT 100: Docker daemon restarted (was stopped since 09-27); backups
  `/root/pokecollector-backups/{pre-pokescanner-20260929-0917.dump, stack-src-pre-pokescanner-20260929-0917.tgz}`,
  images tagged `pokecollector-{backend,frontend}:pre-pokescanner`; branch extracted into
  `/etc/komodo/stacks/pokecollector`, `.env` (+`.env.bak.20260929-0917`) got the
  `EXTERNAL_MATCHER_*` + `LIVE_SCANNER_URL` lines; built + `up -d` with all three compose
  files; `/api/health` ok, pre-upgrade backup 1.42.2→1.51.0 written by the backend;
  scanner provider `external`/pokescan tested + saved (single-user mode).
- Authentik: ProxyProvider 32 "pokescan live" (forward_single, https://scan.nilss.dev),
  application `pokescan-live`, attached to the embedded outpost.
- OPNsense Caddy (config.xml, backup `/conf/config.xml.bak-pokescan-20260929`): subdomain
  `scan.nilss.dev` (158ae50d…) + 4 handles: `/outpost.goauthentik.io/*` → 10.0.1.10:9000,
  `/live*` and `/collection/*` → 10.0.1.40:8000 with ForwardAuth, catch-all → 10.0.1.40:8000
  (bearer). Verified: `/health` 200, `/live` 302 → Authentik, `/outpost.goauthentik.io/ping` 204.

### Owner TODOs

1. **Authentik → Outposts → "authentik Embedded Outpost" → edit → set
   `authentik_host_browser: https://auth.nilss.dev`** (the classifier refused this global
   change). Until then the SSO redirect from scan.nilss.dev goes to
   `http://10.0.1.10:9000/…`, which only works on the LAN.
2. Live-page adds: `POKESCANNER_COLLECTION_URL=http://10.0.1.10:8000`,
   `POKESCANNER_COLLECTION_USERNAME/PASSWORD` (a pokecollector user; the service re-logs in
   on 401) and `POKESCANNER_COLLECTION_UI_URL=https://poke.nilss.dev/scans` in the agenix
   env secret + `pokescan-serve.nix`, then `update-pokescanner`.
3. Komodo: don't redeploy/pull the `pokecollector` stack from Komodo — its `file_paths`
   lack `docker-compose.build.yml` (fix in Mongo per `host.md`, or leave it and deploy by hand).
4. Try `/live` on a real phone, then scan ~20 PRE/full-art cards and note results here.
5. pokescan: decide whether `live` merges into plan-a.

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
