# pokescanner — ops runbook

How the deployed pieces fit, and how to change each one. State lives in `docs/status.md`,
design in `docs/POKESCANNER-PLAN.md`.

## Components

| Piece | Where | How it's managed | Repo |
|---|---|---|---|
| pokescanner service (`pokescan.serve`) | CT 140 `pokescanner`, 10.0.1.40:8000, NixOS, 8 cores / 3 GB (cores 4 -> 8 on 2026-09-29, ORT threads stay 4) | denils (`modules/aspects/features/pokescan-serve.nix`, `hosts/pokescanner.nix`), `nix run .#update-pokescanner` | `/root/denils` |
| pokescan source the service runs | `/tank/pokescan/src` (git checkout of the trunk `master`; **2adb928 since 2026-10-04**), bind-mounted ro at `/var/lib/pokescanner/src` (mp1) | `git -C /tank/pokescan/src fetch && checkout <rev>`; `pokescanner.path` restarts the unit when files change; bump `commit = "…"` in the nix file (label only) | `nstrelow/pokescan` |
| model bundle v12 | `/tank/pokescan/bundle-v12` (1.6 GB incl. `ref_thumbs_v12/`), ro at `/var/lib/pokescanner/bundle` | rebuilt with `scripts/export_serve_bundle.py --out <newdir>` from a pokescan worktree; new versions get a NEW dir, then change `bundleDir` in the nix file | pokescan |
| model bundle v12p3 (historical) | `/tank/pokescan/bundle-v12p3` (1.98 GB: v12 + `client/` towers fp16 172.9 MB / fp32 345.1 MB / int8wo 87.8 MB + unet 0.6 MB; bundle sha 0fac0cf6b1b1) | CT 140 mp0 2026-09-29 evening → 2026-09-30, then the v14 rollback bundle (historical; current rollback: d43e4cc + bundle-v22); the page picks the tower per device (WebGPU+shader-f16 → fp16, WebGPU → fp32, else int8wo). Its rollback then was mp0 → bundle-v12p2 (+ `pct reboot 140`) | pokescan |
| model bundle v14 (historical) | `/tank/pokescan/bundle-v14` (2.33 GB: gallery v14 = 79,421 prints incl. English Classic + fr/it/pt/es SV/ME refs, `names_v14.json`, `ref_thumbs_v14/` 79,397, `client/` towers byte-identical to v12p3; bundle sha b7b8e8532325) | CT 140 mp0 2026-09-30 → 2026-10-02 ~09:50, code pokescan `live` 15d4869 (denils 6ad3ab7); replaced by v18/v20/v22/v23 (see the deploy blocks below) | pokescan |
| **model bundle v23 (current)** | `/tank/pokescan/bundle-v23` (gallery v22 + bank v22g + twins v24, sha 84df050d5e36; client towers byte-identical to v22) | CT 140 **mp0 since 2026-10-02**, code pokescan 9d26328 (denils b65ced3). Health = 9d26328 / v22 / 84df050d5e36 | pokescan |
| model bundle v22 (rollback) | `/tank/pokescan/bundle-v22` (sha 3d0eb299dc48) | kept for rollback: `checkout d43e4cc`, mp0 → bundle-v22, denils `commit = "d43e4cc"` (see "Current rollback" below) | pokescan |
| service secrets | denils `secrets/pokescanner-env.age` → `/run/agenix/pokescanner-env` (root 0400, read by systemd `EnvironmentFile`) | keys: `POKESCANNER_TOKEN` (API bearer), `POKESCANNER_COLLECTION_USERNAME/PASSWORD` (pokecollector user `pokescanner`) | denils |
| traces | CT 140 `/var/lib/pokescanner/traces/` (local disk, NOT backed up); one dir per `debug=1` identify: `trace.json` (full top-k, geometry), `source.jpg`, `plane.webp`, `overlay.webp` (~120–760 KB); no-card states keep only json + small overlay | swept by the service at startup + hourly: `POKESCANNER_TRACE_KEEP_DAYS` (90) then `POKESCANNER_TRACE_MAX_GB` (10), oldest first — knobs in `pokescan-serve.nix` (`traceKeepDays`, `traceMaxGb`) | pokescan `serve/app.py` `sweep_traces` |
| CT 140 rootfs | `pool/subvol-140-disk-0`, **20 GB** since 2026-09-29 (was 8) | grow with `pct resize 140 rootfs <N>G` (online), then set `proxmox.disk` in `hosts/pokescanner.nix` and run a tofu `apply -refresh-only` in `~/.local/share/den-lxc/pokescanner` so `check-drift` is clean (never `provision-*`) | denils |
| pokecollector (fork branch `pokescanner`, live `1dbcaca` since 2026-10-04) | CT 100 `alpine-komodo`, docker stack `/etc/komodo/stacks/pokecollector`, https://poke.nilss.dev | by hand (see below) — NOT via Komodo | `nstrelow/pokecollector` |
| public entry | OPNsense Caddy: `scan.nilss.dev` → 10.0.1.40:8000; `poke.nilss.dev` → 10.0.1.10:3000 | `/conf/config.xml` `<reverseproxy>` (subdomain `158ae50d…` + 4 handles, **all three scanner handles with ForwardAuth since 2026-09-30**), `configctl template reload OPNsense/Caddy && configctl caddy reload` | — |
| SSO | Authentik on CT 100: ProxyProvider "pokescan live" (forward-auth single app), app `pokescan-live`, embedded outpost (`authentik_host` = https://auth.nilss.dev); **owner-only** (see "Owner-only lockdown") | UI, or `docker exec authentik-server-1 ak shell -c '…'` | — |
| monitoring | Gatus "Pokescanner" (vigil) → `/health` every 5 min; homepage tile (arr) | emitted by the denils feature aspect; `update-vigil` / `update-arr` after changes | denils |

## P2 on-device assets (caching)

* `/live/models/manifest.json` is `no-cache`; the model files carry their sha in the name and
  are `immutable`. The worker checks the sha256 and keeps them in Cache Storage
  (`pokescan-models-v<bundle_version>`).
* `/live/p2/*.js` (worker, device, geometry, pipeline) are `no-cache` like `live.js`, so a
  deploy never pairs a new page with a stale worker; only `/live/p2/ort/**` (vendored
  onnxruntime-web, changes only with `VENDORED.md`) is `max-age=86400`.
* These routes are GET-only: `curl -I` gets 405, use `curl -s -o /dev/null -D -`.
* `POKESCANNER_LIVE_ISOLATED=1` (COOP/COEP → threaded Wasm) has been ON since 2026-09-29 (pokescan e263e43, denils 9d76c64; status.md, "Misc TODOs live 2026-09-29").

## Auth model (read this before touching it)

**scan.nilss.dev is owner-only since 2026-09-30** (owner's decision). Two layers:

1. **Caddy + Authentik:** every path on `scan.nilss.dev` (the `/live*`, `/collection/*` and
   catch-all handles) has forward-auth; only `/outpost.goauthentik.io/*` is open (the
   outpost callback). The Authentik app `pokescan-live` is bound to the owner's user only.
2. **The service (introduced in pokescan ddb93b8; live: 9d26328):**
   - `/identify`, `/price/*`, `/bundle`, `/trace/*`, `/collection/add`,
     `/public/stats`, `/live/loadlog` need `Authorization: Bearer $POKESCANNER_TOKEN`.
   - `/ref/*` and the full `/health` need a *trusted caller*. That is any of: the bearer; a
     direct LAN/loopback peer that is not Caddy (Gatus on vigil, pokecollector on CT 100,
     curl on the host); or Caddy (10.0.0.1, `POKESCANNER_TRUSTED_PROXIES`) with
     `X-Authentik-Username` in `POKESCANNER_SSO_USERS` (denils: `nils`). The page's
     `<img src=/ref/…>` gets through on that SSO header.
   - Anyone else gets `{"ok":true}` from `/health` and 401 from `/ref`.
   - `CDN-Cache-Control: no-store` is on every response, so Cloudflare never serves a copy
     of something fetched with the owner's session.
   - `/live*` and `/collection/status` stay open on the service; Caddy guards them.
- pokecollector's backend calls `http://10.0.1.40:8000` over the LAN with the token
  (`EXTERNAL_MATCHER_TOKEN` in its `.env`), so Caddy doesn't affect it. Gatus uses
  `http://10.0.1.40:8000/health` over the LAN and still gets the full body, `disk` included.
- Since v24.1 (2026-10-04) the live page needs no bearer: the SSO session covers `/identify` and `/price`
  too (see the v24.1 block below); a token kept in localStorage is removed. Before that the page needed
  the bearer (`?token=` once, stored in localStorage).

## Owner-only lockdown (2026-09-30)

- **Authentik** (CT 100):
  - App `pokescan-live` has two bindings, mode `any`:
    - order 0: user binding → `nils` (pk 5, djnilse@gmail.com);
    - order 1: expression policy **"pokescan-live: owner only (logged)"** (`request.user.pk
      == 5`, `execution_logging` on). It grants nothing extra; it exists so that every access
      decision becomes a `policy_execution` event.
  - Checked with `PolicyEngine`: ulla, lucas, antje, julien, gigi and a throwaway user
    (deleted) are DENIED; nils passes.
  - Until 2026-09-30, only `nils` had ever authorized the app.
  - No other app's bindings and no flows were changed.
- **Login alerts → ntfy** (same topic as PBS/PVE/ZED, see ops memory):
  - Notification rule **"pokescan-live: logins + denials"** (severity notice).
  - Filter: expression policy **"pokescan-live: notify filter"**.
  - Transport: **"ntfy (pokescan-live)"**, webhook to `https://ntfy.sh/` with `send_once`. The
    JSON publish body comes from the webhook mapping **"ntfy: pokescan-live alert"**, which
    carries the topic.
  - Destination: group `authentik Admins`. The webhook creates no stored notification.
  - Alerts:
    - **login** (priority 3): `authorize_application` for `pokescan-live`, i.e. each new
      scan.nilss.dev session;
    - **access DENIED** (priority 4): the logged guard fails on `/application/o/authorize…`.
      App-library listings are ignored;
    - **failed login** (priority 4): `login_failed` whose flow `next` names the pokescan
      client_id or scan.nilss.dev.
  - Denials and failed logins are deduplicated for 10 minutes per user or IP (Django cache).
    Logins are not deduplicated.
  - Tested 2026-09-30 07:19 UTC: a throwaway user's denial reached ntfy ("access DENIED for
    pokescan-denytest").
- **Caddy:** the catch-all handle `a8c1116b…` got ForwardAuth=1 (description "everything else
  (SSO, owner-only)"). Backup: `/conf/config.xml.bak-pokescan-lockdown-20260930`.
  `/root/pokescan_forwardauth.py on|off|status` now switches `/live*` and the catch-all
  together. `on` is the owner-only state.
- **Verified from outside** without a cookie, 2026-09-30: every path answers 302 to
  auth.nilss.dev, including `/`, `/live`, versioned assets, model files, `/health`, `/ref`,
  `/price`, `/bundle`, `/trace`, `/public/stats`, `/docs`, `/openapi.json`, `POST /identify`
  and `POST /collection/add`. A forged `X-Authentik-Username` header changes nothing. Seen
  from Caddy's IP without SSO: `/health` = `{"ok":true}`, `/ref` 401; with the header: full
  and 200.

**Re-enable sharing:**
- *Another signed-in person:* add a user or group binding on `pokescan-live`, and extend the
  guard's expression if their denials shouldn't alert. Also add the username to
  (`POKESCANNER_SSO_USERS` in `pokescan-serve.nix`) or clear it. Without that their `/ref`
  thumbnails return 401. They also need the bearer in their browser.
- *Anonymous public demo:*
  1. Run `ssh root@10.0.0.1 /usr/local/bin/python3 /root/pokescan_forwardauth.py off`; this
     opens `/live*` and the catch-all.
  2. Set `publicUntil = "<ISO>"` in denils, then run `update-pokescanner` and
     `systemctl restart pokescanner`.
  3. Undo with `public_demo_off.sh`. The Cloudflare side is managed separately.

## Public demo (ENDED 2026-09-29 23:29; inert since the 2026-09-30 lockdown)

Owner-approved 2026-09-29. `POKESCANNER_PUBLIC_UNTIL` (denils `publicUntil`) makes the service
answer visitors **without** the bearer on `POST /identify` and `GET /price/*` while
`now < until`; checked per request, so it switches itself off at the timestamp (no restart).
`/health.public = {active, until}`; the page (`demo.js`) runs as the demo when it has no token
and `public.active`. Caddy: `/live*` forward-auth off (handle `2b59288c…`), `/collection/*`
and `/outpost.goauthentik.io/*` unchanged.

| | anonymous visitor | owner (bearer in localStorage) |
|---|---|---|
| `/live`, assets, device models, `/ref`, `/health` | yes (per-IP limits) | yes |
| `/identify` | yes: `debug` ignored, **no trace**, 6 MB multipart / 1.5 MB JSON cap, 411 without Content-Length | yes, unchanged (debug/traces) |
| `/price/*` | yes (6 burst, 30/min) | yes |
| `/collection/*` | no: Caddy SSO 302 + bearer | adds need the SSO cookie: sheet shows "sign in" → `/collection/login` → SSO → back to `/live` |
| `/trace/*`, `/bundle`, `/public/stats`, `/live/loadlog` | 401 | yes |

Limits (`src/pokescan/serve/public.py`): per client IP token buckets — identify 3/s burst +
60/min, price 6 burst + 30/min, `/ref` 80 burst + 10/s, page/assets 60 + 5/s, model files 8/h,
vendored ORT 40/h; 429 + `Retry-After`. The IP is `CF-Connecting-IP` only when the request came
from Caddy (`POKESCANNER_TRUSTED_PROXIES`, default 10.0.0.1) **and** Caddy's last
`X-Forwarded-For` hop is a Cloudflare range; otherwise that last hop. IPs exist only in the
in-memory limiter. Concurrency: 2 anonymous identifies in the service, others wait ≤ 2 s,
then 503 `Retry-After: 2`; the recogniser lock is a `PriorityLock` (owner frames first). A wrong
bearer is a 401, never "anonymous". `CDN-Cache-Control: no-store` on `/identify`, `/price`,
`/health`, `/public/*` (Cloudflare shows `cf-cache-status: DYNAMIC`).

Stats (aggregate only, in memory, reset on restart):
`curl -s http://10.0.1.40:8000/public/stats -H "Authorization: Bearer $TOKEN"` → requests,
states per mode (server/device), rejections (`rate:<kind>`, `busy`, `size`), latency p50/p90.

**Revert (one command, Proxmox host):**
```
bash /srv/repos/PicaLens/scanner/pokescan/scripts/public_demo/public_demo_off.sh
# (same file in any pokescan checkout of `live`: scripts/public_demo/public_demo_off.sh)
```
It turns forward-auth back on for `/live*` (`opnsense_forwardauth.py on`, timestamped
`config.xml` backup + both configctl commands), sets `publicUntil = ""` in denils, commits,
pushes, `update-pokescanner`, restarts the unit and prints `/health.public` + the `/live`
status (expect 302). Caddy only: `ssh root@10.0.0.1 /usr/local/bin/python3
/root/pokescan_forwardauth.py on|off|status`. Extending the demo = new `publicUntil` +
`update-pokescanner` + `systemctl restart pokescanner`.

Verified 2026-09-29 via https://scan.nilss.dev without cookie/token: `/live` 200 (demo tag),
versioned `live.js`/`demo.js` 200, `/identify?debug=1` 200 with `trace_id: null` and no new
trace dir, `/price` 200 (`DYNAMIC`), manifest 200; `/collection/{status,login,add}` 302 →
auth.nilss.dev, `POST /collection/add` 302; `/trace`, `/bundle`, `/public/stats` 401; outpost
ping 204; 7 parallel identifies → 4× 429; the same burst via Caddy on the LAN path was
not limited (per-client keys work); owner bearer via the public host 200, wrong bearer 401.
Unit tests: `tests/test_serve_public.py` (expiry with a past timestamp, debug ignored, limits,
gate 503, priority lock, client-IP trust), `tests/live/demo.test.mjs`.

## Caching

Cloudflare fronts scan.nilss.dev and ignored `no-cache` on the live page's `.js`/`.css` (stale UI after deploys). Since pokescan 9dfab0c `/live` is `no-store` and points at `/live/v/<content hash>/live.{js,css}` (immutable); relative module imports and the p2 worker inherit the prefix, the vendored ORT redirects to its stable `/live/p2/ort/` (1 day). A deploy therefore changes every asset URL automatically.

## Common operations

**Current rollback (2026-10-04, from v24.2 `78dc0f0` + bundle-v24, back to v24.1 `2adb928` + bundle-v23):**
`pct set 140 -mp0 /tank/pokescan/bundle-v23,mp=/var/lib/pokescanner/bundle,ro=1`, `git -C /tank/pokescan/src checkout 2adb928`,
`pct reboot 140` (the mount change needs the restart); no denils change. Check: `curl -s http://10.0.1.40:8000/health | jq
'{bundle_sha256,similar_hints}'` → `84df050d5e36` / 90; `/identify` has no `print_source` in `language`. One step further
back (v24.1 → v24): `checkout 9d26328` + `systemctl restart pokescanner`, bundle-v23 as is (health has no `similar_hints`;
the phone needs the bearer again).
Note: `/tank/pokescan/src`'s `origin` remote still names the pre-move path `/srv/repos/pokescan`; fetch with
`git -C /tank/pokescan/src fetch /srv/repos/PicaLens/scanner/pokescan master` (or fix the remote URL).

**Before any deploy:** the release has a row + section in pokescan `docs/CHANGELOG.md` (fill its "deployed" column with the commit and date after the deploy) and `docs/ASSETS.md` is regenerated (`scripts/assets_report.py`); pokescan `tests/test_changelog.py` enforces both.

**Update the service code** (pokescan trunk `master` = `live` = `plan-a`):
```
git -C /tank/pokescan/src fetch origin && git -C /tank/pokescan/src checkout <rev>
# path unit restarts it; confirm:
curl -s http://10.0.1.40:8000/health | jq .commit
# optional: bump `commit = "<rev>"` in pokescan-serve.nix and update-pokescanner
```

**Rotate the API token** (do this — it was pasted into a chat once):
```
cd /root/denils
ssh root@10.0.1.40 cat /run/agenix/pokescanner-env > /tmp/env.plain     # keep the collection lines
sed -i "s/^POKESCANNER_TOKEN=.*/POKESCANNER_TOKEN=$(openssl rand -hex 32)/" /tmp/env.plain
grep -E '^  (builder|pokescanner) = "' secrets.nix | sed -E 's/^  [a-z]+ = "([^"]*)";.*/\1/' > /tmp/recip
nix run nixpkgs#age -- -R /tmp/recip -o secrets/pokescanner-env.age /tmp/env.plain
git commit -am "pokescanner: rotate token" && git push && nix run .#update-pokescanner
ssh root@10.0.1.40 systemctl restart pokescanner        # a secret+unit switch can restart on the old unit
# then on CT 100: edit EXTERNAL_MATCHER_TOKEN in /etc/komodo/stacks/pokecollector/.env and
pct exec 100 -- sh -c 'cd /etc/komodo/stacks/pokecollector && docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f compose.tz.yml up -d backend'
rm /tmp/env.plain /tmp/recip
```
Then clear `pokescan.token` from the phone's localStorage (or open `/live?token=<new>`).

**Deploy a new pokecollector build** (branch `pokescanner`):
```
git -C /srv/repos/PicaLens/collection/pokecollector archive --format=tar.gz -o /tmp/pc.tgz fork/pokescanner
pct push 100 /tmp/pc.tgz /tmp/pc.tgz
pct exec 100 -- sh -c 'cd /etc/komodo/stacks/pokecollector && docker tag pokecollector-backend:local pokecollector-backend:prev && docker tag pokecollector-frontend:local pokecollector-frontend:prev && rm -rf backend frontend && tar xzf /tmp/pc.tgz && docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f compose.tz.yml build && docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f compose.tz.yml up -d'
curl -s https://poke.nilss.dev/api/health
```
Never redeploy/pull this stack from Komodo: its `file_paths` lack `docker-compose.build.yml`,
so it would pull upstream's ghcr 1.51.0 images (no external matcher). Fix in Mongo per
`memory/host.md` if you want Komodo to match. If `docker` commands fail on CT 100, the daemon
is stopped again: `pct exec 100 -- rc-service docker start` (containers survive via live-restore).

A frontend-only change (no `backend/` diff) can build and recreate just the frontend:
`... build frontend` then `... up -d --no-deps --no-build frontend`; the backend container keeps running.

**Collector UX deploy 2026-10-04 ~10:10 Berlin** (owner yes 2026-10-04; PicaLens #30). Fork `pokescanner`
a277a28 → `1dbcaca` (fast-forward to `ux`), frontend only, the recipe above with `build frontend` +
`up -d --no-deps --no-build frontend`. Before it: `pokecollector-{frontend,backend}:local` tagged `:pre-ux` and
`:prev` (frontend `f155908aacdb`, backend `8fbd5ae44c21`, both the 2026-09-29 build of `6fd78e1`; no code change
between that and a277a28), stack source (incl. `.env`, without `data/`, `backups/`) in
`/root/pokecollector-backups/stack-src-pre-ux-20261004-1009.tgz`. New frontend image `07b42e6648d0`, bundle
`assets/index-COX-vhmw.js`. Checks: `npm test` 343 vitest + translations green; Playwright `overlay-back.spec.js`
+ `card-system.spec.js` 27 passed / 1 skipped (desktop skip of the phone-only test). This host's Playwright
browser cache has revision 1243 only, so run with
`PLAYWRIGHT_CHROMIUM_PATH=/root/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell`.
Live smoke (390×844, `http://10.0.1.10:3000/collection`, non-GET API calls blocked): card dialog is a bottom sheet
(top at y=56 of 844), ✕ 44 px stays in view after scrolling, backdrop tap / ✕ / Escape / browser Back each close it,
Back stays on the page, no history entry left, no page errors; `/api/settings/scanner/external` reaches pokescan
(`ok: true`). **Rollback (frontend):**
```
pct exec 100 -- sh -c 'cd /etc/komodo/stacks/pokecollector && docker tag pokecollector-frontend:pre-ux pokecollector-frontend:local && docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f compose.tz.yml up -d --no-deps --no-build frontend'
```
(optionally restore the source dirs from the tgz so a later rebuild doesn't bring `ux` back).

**Roll back pokecollector**: `compose down --remove-orphans` (no `-v`), restore
`/root/pokecollector-backups/stack-src-pre-pokescanner-20260929-0917.tgz` into
`/etc/komodo/stacks`, retag `pokecollector-{backend,frontend}:pre-pokescanner` → `:latest`,
`docker compose -p pokecollector -f docker-compose.yml -f compose.tz.yml up -d --no-build`.
DB: `pre-pokescanner-20260929-0917.dump` (`pg_restore -U pokemon -d pokemon_tcg --clean`), only
if 1.42.2 fails on the 1.51.0 schema (migrations were additive).

**Roll back the v14 deploy** (historical; superseded by "Current rollback (2026-10-03)") (Proxmox host; back to live e03d629 + bundle-v12p3):
```
git -C /tank/pokescan/src checkout e03d629
pct set 140 -mp0 /tank/pokescan/bundle-v12p3,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140
# denils: commit = "e03d629" in modules/aspects/features/pokescan-serve.nix (+ the bundle comments
# in hosts/pokescanner.nix), commit, push, nix run .#update-pokescanner, then:
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256}'   # e03d629 / v12 / 0fac0cf6b1b1
```
The page's model cache key follows `bundle_version` (`pokescan-models-v14` → `-v12`), so a
rollback (like the upgrade) makes device-mode users re-fetch the tower once; the file names
are the same, so Cloudflare's immutable copies serve it.

**Rebuild the bundle for a new gallery version** (from a pokescan worktree with `data/`):
```
PYTHONPATH=src .venv/bin/python scripts/build_names_subset.py      # names_vN.json
PYTHONPATH=src .venv/bin/python scripts/build_ref_thumbs.py        # ~1 GB, hours
PYTHONPATH=src .venv/bin/python scripts/export_serve_bundle.py --out /tank/pokescan/bundle-vN \
  --client-tower data/exp/onnx/clip_b16_224.int8wo.onnx --client-localizer data/exp/onnx/unet-m3.single.onnx \
  --client-tower-fp16 data/exp/onnx/clip_b16_224.fp16.onnx --client-tower-fp32 data/exp/onnx/clip_b16_224.onnx
```
v14 (2026-09-30) shortcuts: `build_names_subset.py --catalog data/catalog_v13.sqlite` (v14 added
no catalog row, so there is no catalog_v14); unchanged thumbnails hard-linked from
`ref_thumbs_v12` (same `ref` path in both names files), so `build_ref_thumbs.py --version 14`
only wrote the 18,797 new ones (~10 min). Device-mode gate: a temp bundle = the new
`bundle.json` with `clip_tower` → int8wo (+ card_back's tower sha, recomputed `bundle_sha256`),
then `gate_cascade.py --bundle <it> --tta 4 --rotation-mode live1`, compared frame by frame with
`gate_cascade_p2_live1_tta4.json`.
Then repoint CT 140's mp0 (`pct set 140 -mp0 <dir>,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140`), bump `commit` in `pokescan-serve.nix` → `update-pokescanner` → `systemctl restart pokescanner`. `Bundle.load` refuses any
file whose sha256 differs from `bundle.json`, so a half-copied dir fails loudly.

**Deployed 2026-10-02: v18 stack** (`live` 7b9a53c, `/tank/pokescan/bundle-v18`, sha af05805bdf15;
superseded the v15+v16 candidate, see `status.md`). **Roll back the v18 deploy** (historical; superseded by "Current rollback (2026-10-03)") (Proxmox host):
```
git -C /tank/pokescan/src checkout cdce949
pct set 140 -mp0 /tank/pokescan/bundle-v14,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140
# denils: commit = "cdce949" in modules/aspects/features/pokescan-serve.nix (+ bundle-v14 comments), push, nix run .#update-pokescanner
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256}'   # cdce949 / v14 / b7b8e8532325
```
The next gallery gets its own `bundle-vN` beside these; the same steps with its sha deploy it.

**v20 DEPLOYED 2026-10-02 ~12:45** (owner OK; live 7045960, denils bdac1bd, health 7045960 / v20 / 60f0cc5fc797). pokescan branch `live-v20` 7045960 = live 33fa456
(v18 + opt-in side2 + ORT spin off) + `dp-era` (gallery v19 promos + v20 DP-era Japanese, 1,199 rows,
picker-only, tap-only) + `classic-strip` (Classic outlined number fallback, bank v18f). Bundle
`/tank/pokescan/bundle-v20`, sha 60f0cc5fc797, 82,926 prints; client towers byte-identical to v18.
Server gate 132/132 identical to v18, negatives 0/134, device gate identical to v18's baseline; tests green.
Deploy (Proxmox host):
```
cd /root/denils && git pull
git -C /tank/pokescan/src fetch origin && git -C /tank/pokescan/src checkout 7045960   # or `live` after ff
git -C /srv/repos/PicaLens/scanner/worktrees/live20 push origin live-v20:live                       # ff only, never force (historical: worktree removed 2026-10-03)
pct set 140 -mp0 /tank/pokescan/bundle-v20,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140
# denils: commit = "7045960" in modules/aspects/features/pokescan-serve.nix (+ bundle-v20 comment in
# hosts/pokescanner.nix), commit, push, nix run .#update-pokescanner, then:
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256}'   # 7045960 / v20 / 60f0cc5fc797
```
Check /live, the model manifest (cache key `pokescan-models-v20`; tower file names unchanged, so no
Cloudflare trap) and a real /identify. If `live` moved past 33fa456 first, merge it into live-v20 and
re-run the tests before deploying. **Rollback:** `checkout 33fa456`, mp0 → `/tank/pokescan/bundle-v18`,
denils `commit = "33fa456"`, update + restart; health = 33fa456 (tree of d6f9330) / v18 / af05805bdf15.

**v22 DEPLOYED 2026-10-02 ~14:00** (owner OK; live b07fabc, denils e1247cb, health b07fabc / v22 / 3d0eb299dc48). pokescan branch `live-v22` b07fabc = live 7045960 (v20)
fast-forwarded through `dp-v21` (gallery v21: 519 second references, scans next to the 162 px DP6 / Pt1-4
images) and `dp-v22` (gallery v22: 45 more DP-era Japanese cards from the final pokeassets table, picker-only;
`hints_v22.json` = 90 hints, supersedes hints_v20). Bundle `/tank/pokescan/bundle-v22`, sha 3d0eb299dc48,
82,971 prints; client towers byte-identical to v20. Server gate 132/132 identical to v20/v21, negatives 0/134,
device gate unchanged; full suite, test_serve_live 80/80 and node 170/170 green. Wild: v21 lifts the pre-selected
DP6/Pt card agreeing with the title 208 -> 357 of 559; v22 2 saves -> taps; 0 new contradictions in either.
Deploy (Proxmox host), the v20 pattern:
```
cd /root/denils && git pull
git -C /tank/pokescan/src fetch origin && git -C /tank/pokescan/src checkout b07fabc
git -C /srv/repos/PicaLens/scanner/worktrees/dpv22 push origin live-v22:live      # ff from 7045960 only, never force (historical: worktree removed 2026-10-03)
# also ff master / plan-a to b07fabc
pct set 140 -mp0 /tank/pokescan/bundle-v22,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140
# denils: commit = "b07fabc" in modules/aspects/features/pokescan-serve.nix and bundle-v22 in its
# comment + modules/aspects/hosts/pokescanner.nix (pct set line + bundle comment; rollback bundle-v20);
# commit, push, nix run .#update-pokescanner, then:
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256}'   # b07fabc / v22 / 3d0eb299dc48
```
Check /live (200), `/live/models/manifest.json` (tower file names unchanged, cache key follows the bundle
version, no Cloudflare trap) and a real /identify (a DP-era card -> AMBIGUOUS with the DP-era print in the
picker). If `live` moved past 7045960 first, merge it into live-v22 and re-run the tests. **Rollback:**
`checkout 7045960`, mp0 -> `/tank/pokescan/bundle-v20`, denils `commit = "7045960"`, update + restart;
health = 7045960 / v20 / 60f0cc5fc797.

**v23 — DEPLOYED 2026-10-02** as 1818455 (live-v23 47a0e6b + owner session log `live-sessionlog`), denils c0d00ab; rollback b07fabc + bundle-v22. The recipe below is the deployed 1818455. pokescan branch `live-v23` 47a0e6b = live b07fabc (v22)
fast-forwarded through `en-sinks` (veto-only: e-card H-holo twins need their own number evidence; a WotC
Black Star promo save under 0.82 without its own number becomes a tap) and `en-numbers` (bank v22g: a
last-resort WotC/Evolutions number-strip template tier, so Base Set / Base Set 2 / Legendary Collection /
Evolutions twins resolve by set total). No gallery change (still v22). Bundle `/tank/pokescan/bundle-v23`,
sha 84df050d5e36; client towers byte-identical to v22. Server gate 132/132 identical to v22, negatives 0/134,
device gate 0/132 changed (same 11 failing rows); suite 984 pass (1 timing test that passes alone), node 170/170.
Wild (report-only): en-sinks dev wrong saves 16 -> 8 on e-card twins, -4 Pichu promo; en-numbers 22 taps -> saves
(dev), 0 new contradictions. SWSH299-301 not referenced: no approved source has them.
Deploy (Proxmox host), the v22 pattern:
```
cd /root/denils && git pull
git -C /tank/pokescan/src fetch origin && git -C /tank/pokescan/src checkout 1818455
# live / master / plan-a contain 1818455 (merged, never forced); the old `worktrees/v23` push step is historical (worktree removed 2026-10-03)
pct set 140 -mp0 /tank/pokescan/bundle-v23,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140
# denils: commit = "1818455" in modules/aspects/features/pokescan-serve.nix and bundle-v23 in its
# comment + modules/aspects/hosts/pokescanner.nix (pct set line + bundle comment; rollback bundle-v22);
# commit, push, nix run .#update-pokescanner, then:
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256}'   # 1818455 / v22 / 84df050d5e36
```
`bundle_version` stays v22 (same gallery), so the device model cache key does not change. Check /live (200),
`/live/models/manifest.json` (tower file names unchanged) and a real /identify (an English Base Set card).
**Rollback** (then; current rollback is d43e4cc + bundle-v22, above): `checkout
b07fabc`, mp0 -> `/tank/pokescan/bundle-v22`, denils `commit = "b07fabc"`, update + restart; health =
b07fabc / v22 / 3d0eb299dc48.

**v24 + session log v2 — DEPLOYED 2026-10-02** as 9d26328 (`live-sessionlog2` on en-langflag d43e4cc = v24:
th/id language flag → one tap, 0 new saves; session log v2 adds false detections, card gaps and the model-wait split),
denils b65ced3; rollback d43e4cc + bundle-v22. Gallery still v22, bundle still `/tank/pokescan/bundle-v23`
(sha 84df050d5e36), so the device model cache key did not change. Gate 0/132 changed on v23, negatives 0/134,
device unchanged. Deploy (Proxmox host), the v23 pattern with the code step only:
```
cd /root/denils && git pull
git -C /tank/pokescan/src fetch origin && git -C /tank/pokescan/src checkout 9d26328
# master / live / plan-a already contain 9d26328 (merged, never forced)
# denils: commit = "9d26328" in modules/aspects/features/pokescan-serve.nix (bundle-v23 unchanged; rollback d43e4cc + bundle-v22);
# commit, push, nix run .#update-pokescanner, then:
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256}'   # 9d26328 / v22 / 84df050d5e36
```
**Rollback:** `checkout d43e4cc`, mp0 -> `/tank/pokescan/bundle-v22`, denils `commit = "d43e4cc"`, update + restart;
health = d43e4cc / v22 / 3d0eb299dc48. Owner review of the session log: `/live/sessions`.

**v24.1 — DEPLOYED 2026-10-04 01:39 Berlin (2026-10-03 23:39 UTC)** as `2adb928` (`live-hints-sso` = `live-hints` 99136fb + `live-sso`
2c3334f on master; owner yes 2026-10-03, PicaLens #4). Code-only plus hint pictures, no denils / Authentik / OPNsense
change. Gate 132/132 identical to v24 (T11 p50 776 ms), negatives 0/134, device 0/132 state changes, hints gate PASS.
```
cp -a /tank/pokescan/hints-v22-thumbs /tank/pokescan/bundle-v23/hints_v22      # 31 WebP + hints_v22_manifest.json
git -C /tank/pokescan/src fetch /srv/repos/PicaLens/scanner/pokescan master && git -C /tank/pokescan/src checkout 2adb928
ssh root@10.0.1.40 systemctl restart pokescanner
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_sha256,similar_hints,auth_via}'   # 9d26328 (label) / 84df050d5e36 / 90 / "lan"
```
Auth after v24.1: the `/live` page needs no bearer behind Caddy + Authentik. The service treats a request from Caddy
(10.0.0.1) carrying `X-Authentik-Username` in `POKESCANNER_SSO_USERS` as the owner on the owner routes too
(`/identify`, `/price`, `/bundle`, `/trace`, `/collection/add`, `/live/loadlog`, `/public/stats`); an SSO POST must
carry `Origin: https://scan.nilss.dev` (`POKESCANNER_SSO_ORIGINS`), else 403. A direct LAN peer still needs the bearer
on those routes (pokecollector, scripts). uvicorn's proxy headers are off. `/hint/*` has `/ref`'s auth. Smoke test
(2026-10-04, from 10.0.0.1): with the header `auth_via: "sso"`, identify 200 / cross-origin 403; without it or as another
user 401. Public without a cookie: every path 302 to auth.nilss.dev.

**v24.2 — owner yes 2026-10-04 ~05:00** (`ux` with `picker` + `jareader`, PicaLens #30 / #7 / #6). pokescan master
= `live` = `plan-a` = **`78dc0f0`** (`release-v24.2`). New bundle **`/tank/pokescan/bundle-v24`** (sha 161c5780d77a: bank
`number_classical_v22g_jar5b`, `supported_sets_v22_picker.json`, sidecar `identity_prints_v22_picker.json`, `hints_v22/`;
client towers identical to bundle-v23, so `bundle_version` stays v22 and the device model cache key does not change).
bundle-v23 untouched. No denils / Authentik / OPNsense change: mp0 is hand-applied (`proxmox.mountPoints = [ ]` in
`hosts/pokescanner.nix`), so the denils comments that still name bundle-v23 and the `commit` label (9d26328) are stale
labels only. Gate 0/132 changed vs v24.1, negatives 0/134, device 0/132, hints PASS, wild 0/800.
```
git -C /tank/pokescan/src fetch /srv/repos/PicaLens/scanner/pokescan master && git -C /tank/pokescan/src checkout 78dc0f0
pct set 140 -mp0 /tank/pokescan/bundle-v24,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140
curl -s http://10.0.1.40:8000/health | jq '{commit,bundle_version,bundle_sha256,similar_hints}'   # 9d26328 (label) / v22 / 161c5780d77a / 90
```
**Rollback:** see "Current rollback" above (mp0 → bundle-v23, `checkout 2adb928`, `pct reboot 140`).

**Change SSO scope** (which paths need login): edit the `ForwardAuth` flag on the
`scan.nilss.dev` handles in `/conf/config.xml` (script pattern: python + `ET`, never sed on
OPNsense's csh), then the two `configctl` commands. `/outpost.goauthentik.io/*` must stay
routed to 10.0.1.10:9000 without forward-auth or logins loop.

## Where the numbers came from

- Service latency 1.3–2.1 s/frame and 1.1 GB RSS were measured while the Proxmox host ran the
  cardphotos re-scan at load ~40; re-measure on a quiet host (expect ~0.7–1 s).
- No upload downscale: at 1600 px long side 2 of 137 test frames flipped from correct to wrong
  (`POKESCANNER_WORK_SIDE=0`); uploads > 4096 px or > 15 MB get 413.
- 830 pokescan tests, 35 serve/live tests, 12 node consensus tests; pokecollector 1018 backend
  / 337 frontend.
