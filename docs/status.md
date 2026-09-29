# pokescanner — status

Read this first. Plan: `docs/POKESCANNER-PLAN.md`. Ops/runbook: `docs/POKESCANNER-OPS.md`. Workflow: `docs/agent-workflow.md`.

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

1. ~~Embedded outpost host~~ done 09-29 09:40: `authentik_host` + `authentik_host_browser` =
   `https://auth.nilss.dev` (owner-approved), server restarted; `/live` now redirects to
   `https://auth.nilss.dev/application/o/authorize/…`.
2. ~~Live-page adds~~ done 09-29 09:55: pokecollector user `pokescanner` (id 3, role trainer,
   test account so the owner's collection stays clean), creds in the agenix env secret,
   `POKESCANNER_COLLECTION_URL/UI_URL` in the unit; `/collection/status` configured=true; a
   real `POST /collection/add` created item 505 (`hgss4-6_en`). Note: a switch that changes
   both the secret and the unit can restart the service on the OLD unit file — `systemctl
   restart pokescanner` afterwards.
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

## Known problems & pitfalls (2026-09-29)

**Open problems**
1. **Token in the browser.** `/identify` is bearer-only, so the live page needs the API token
   (`/live?token=…` once). The token was pasted into a chat → rotate it (`POKESCANNER-OPS.md`).
   Planned fix (needs owner approval, it changes auth): trusted-proxy mode — Caddy puts
   `/identify` behind Authentik too, the service accepts `X-Authentik-Username` *instead of* the
   bearer only from OPNsense's IP. Handing the bearer to SSO browsers was tried and rejected.
2. **Never tested on a real phone.** Only headless Chromium with a faked camera. Unknowns: iOS
   Safari camera permission + `toBlob` speed, haptics, swipe drawer, PWA/HTTPS quirks.
3. **Engine, not plumbing:** one wild Lugia-ex photo got a wrong confident-looking top-1
   (Sadaija VMAX) with a loose outline; ~1.3–2 s/frame under host load; consensus needs 3
   agreeing frames ≈ 4–6 s per card at that rate. A quiet host should roughly halve it.
4. **Traces grow forever** on CT 140's 8 GB rootfs (~300 KB each, every queue scan is `debug=1`).
   No retention job yet.
5. **Komodo can clobber the pokecollector deploy** (stack `file_paths` lack the build file) and
   **Docker on CT 100 stops on its own** (was down 09-27 → 09-29; cause not found — check
   `/var/log/messages` next time).
6. **pHash scanner is gone** (branch abandoned by decision); `/recognize/local` no longer exists.
   Anything that bookmarked it 404s.
7. **ja/ko/zh-cn hits**: the live instance syncs `en,de,ja,ko,zh-cn` (more than the plan
   assumed), but pokescan has no ko/zh-cn gallery and zh-tw isn't synced → those prints show
   pokescanner's `/ref` thumbnail and go through a live TCGdex fetch on add.
8. **Live-page adds go to the `pokescanner` test user**, not the owner's collection (by choice;
   swap the creds in the secret when done testing).

**Pitfalls hit (so nobody hits them twice)**
- pokescan main checkout `/srv/repos/pokescan` is stale; v12 assets live only in
  `pokescan-wt-r2/data`. The serve/live worktrees symlink into it.
- `models/segdata.py` imported torch on the inference path → the CT (no torch) crash-looped;
  fixed by `models/letterbox.py`. Test with `sys.modules['torch']=None`.
- A NixOS switch that changes both the agenix secret and the unit can restart the service on
  the OLD unit file → `systemctl restart pokescanner` afterwards.
- Authentik: assigning attributes on `outpost.config` doesn't persist (reassign the whole
  object); the embedded outpost caches config → restart `authentik-server-1`;
  `authentik_host_browser` alone doesn't change the redirect, set `authentik_host`.
- OPNsense root shell is csh: no inline python/heredocs; scp a script and run it. Caddy
  forward-auth needs an extra `/outpost.goauthentik.io/*` handle → 10.0.1.10:9000.
- pokecollector `complete_claim` only persists `recognized` + `matches` → matcher flags had to
  be copied into `recognized`; the matcher blob got its own column.
- pokecollector's `frontend/package.json` has no lint; the vitest suite + `check:translations`
  is the gate. Scanner i18n keys under `settings.scanner*` must exist in all 20 locales.
- `provision-<host>` SSH step fails on a fresh raw template (known) → manual bootstrap
  (bring eth0 up, temp authorized_keys, `nixos-rebuild boot`, reboot).
- `check-drift` reports "mounts 0 vs 2" for pokescanner: intentional (bind mounts via `pct set`).

## Next steps (suggested order)

1. Rotate the API token (5 min, `POKESCANNER-OPS.md`).
2. Real-phone session: scan 20 PRE / full-art cards on `scan.nilss.dev/live` and on the queue
   page; note fps, misfires, wrong cards, UX papercuts here. This decides everything below.
3. Trusted-proxy auth (removes the token box) — owner approval needed.
4. Trace retention (e.g. 90 days on CT 140; pokecollector traces stay) + a Gatus disk check.
5. Latency: re-measure on a quiet host; if still > 1 s, try `POKESCANNER_THREADS=8` on a 8-vCPU
   CT, or the int8 tower on the server too (same export as P2).
6. Merge pokescan `live` into `plan-a` (pure addition + torch-free refactor) so the next gallery
   build ships with the service; then bundle v13.
7. P2 (on-device int8 CLIP, hybrid retrieval): plan §7. Start with the int8 export + gate
   re-run (≈1 h) — that alone tells whether P2 is viable.
8. Later: multi-card spread mode, video-mode temporal model inside pokescan (today it's only in
   the page), German-vintage refs, fr/es/it/pt galleries, upstream PR of the external-matcher
   contract to Git-Romer.

## P2 / P3

Not started. Plan §7. P2 = weight-only int8 CLIP export (`export_int8_weightonly.py`) +
gate re-run + onnxruntime-web port of S1/rectify/CLIP preprocessing, hybrid retrieval via
`POST /identify` JSON mode (still to add on the server).
