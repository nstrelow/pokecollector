# pokescanner — ops runbook

How the deployed pieces fit, and how to change each one. State lives in `docs/status.md`,
design in `docs/POKESCANNER-PLAN.md`.

## Components

| Piece | Where | How it's managed | Repo |
|---|---|---|---|
| pokescanner service (`pokescan.serve`) | CT 140 `pokescanner`, 10.0.1.40:8000, NixOS, 8 cores / 3 GB (cores 4 -> 8 on 2026-09-29, ORT threads stay 4) | denils (`modules/aspects/features/pokescan-serve.nix`, `hosts/pokescanner.nix`), `nix run .#update-pokescanner` | `/root/denils` |
| pokescan source the service runs | `/tank/pokescan/src` (git checkout, branch `live`), bind-mounted ro at `/var/lib/pokescanner/src` | `git -C /tank/pokescan/src fetch && checkout <rev>`; `pokescanner.path` restarts the unit when files change; bump `commit = "…"` in the nix file (label only) | `nstrelow/pokescan` |
| model bundle v12 | `/tank/pokescan/bundle-v12` (1.6 GB incl. `ref_thumbs_v12/`), ro at `/var/lib/pokescanner/bundle` | rebuilt with `scripts/export_serve_bundle.py --out <newdir>` from a pokescan worktree; new versions get a NEW dir, then change `bundleDir` in the nix file | pokescan |
| model bundle v12p3 | `/tank/pokescan/bundle-v12p3` (1.98 GB: v12 + `client/` towers fp16 172.9 MB / fp32 345.1 MB / int8wo 87.8 MB + unet 0.6 MB; bundle sha 0fac0cf6b1b1) | CT 140 mp0 since 2026-09-29 evening; the page picks the tower per device (WebGPU+shader-f16 → fp16, WebGPU → fp32, else int8wo). Rollback = mp0 → bundle-v12p2 (+ `pct reboot 140`) | pokescan |
| service secrets | denils `secrets/pokescanner-env.age` → `/run/agenix/pokescanner-env` (root 0400, read by systemd `EnvironmentFile`) | keys: `POKESCANNER_TOKEN` (API bearer), `POKESCANNER_COLLECTION_USERNAME/PASSWORD` (pokecollector user `pokescanner`) | denils |
| traces | CT 140 `/var/lib/pokescanner/traces/` (local disk, NOT backed up); one dir per `debug=1` identify: `trace.json` (full top-k, geometry), `source.jpg`, `plane.webp`, `overlay.webp` (~120–760 KB); no-card states keep only json + small overlay | swept by the service at startup + hourly: `POKESCANNER_TRACE_KEEP_DAYS` (90) then `POKESCANNER_TRACE_MAX_GB` (10), oldest first — knobs in `pokescan-serve.nix` (`traceKeepDays`, `traceMaxGb`) | pokescan `serve/app.py` `sweep_traces` |
| CT 140 rootfs | `pool/subvol-140-disk-0`, **20 GB** since 2026-09-29 (was 8) | grow with `pct resize 140 rootfs <N>G` (online), then set `proxmox.disk` in `hosts/pokescanner.nix` and run a tofu `apply -refresh-only` in `~/.local/share/den-lxc/pokescanner` so `check-drift` is clean (never `provision-*`) | denils |
| pokecollector (fork branch `pokescanner`) | CT 100 `alpine-komodo`, docker stack `/etc/komodo/stacks/pokecollector`, https://poke.nilss.dev | by hand (see below) — NOT via Komodo | `nstrelow/pokecollector` |
| public entry | OPNsense Caddy: `scan.nilss.dev` → 10.0.1.40:8000; `poke.nilss.dev` → 10.0.1.10:3000 | `/conf/config.xml` `<reverseproxy>` (subdomain `158ae50d…` + 4 handles), `configctl template reload OPNsense/Caddy && configctl caddy reload` | — |
| SSO | Authentik on CT 100: ProxyProvider "pokescan live" (forward-auth single app), app `pokescan-live`, embedded outpost (`authentik_host` = https://auth.nilss.dev) | UI, or `docker exec authentik-server-1 ak shell -c '…'` | — |
| monitoring | Gatus "Pokescanner" (vigil) → `/health` every 5 min; homepage tile (arr) | emitted by the denils feature aspect; `update-vigil` / `update-arr` after changes | denils |

## P2 on-device assets (caching)

* `/live/models/manifest.json` is `no-cache`; the model files carry their sha in the name and
  are `immutable`. The worker checks the sha256 and keeps them in Cache Storage
  (`pokescan-models-v<bundle_version>`).
* `/live/p2/*.js` (worker, device, geometry, pipeline) are `no-cache` like `live.js`, so a
  deploy never pairs a new page with a stale worker; only `/live/p2/ort/**` (vendored
  onnxruntime-web, changes only with `VENDORED.md`) is `max-age=86400`.
* These routes are GET-only: `curl -I` gets 405, use `curl -s -o /dev/null -D -`.
* `POKESCANNER_LIVE_ISOLATED` (COOP/COEP → threaded Wasm) is still OFF.

## Auth model (read this before touching it)

- `/identify`, `/trace/*`, `/collection/add` require `Authorization: Bearer $POKESCANNER_TOKEN`.
  `/health`, `/bundle`, `/ref/*`, `/live*`, `/collection/status` are open on the service.
- pokecollector's backend calls `http://10.0.1.40:8000` over the LAN with the token
  (`EXTERNAL_MATCHER_TOKEN` in its `.env`).
- On `scan.nilss.dev`, Caddy puts `/live*` and `/collection/*` behind Authentik; the rest
  is bearer-only. **The live page therefore needs the token in the browser** (`?token=` once,
  stored in localStorage) — see "Known problems" in `status.md` for the planned fix.

## Caching

Cloudflare fronts scan.nilss.dev and ignored `no-cache` on the live page's `.js`/`.css` (stale UI after deploys). Since pokescan 9dfab0c `/live` is `no-store` and points at `/live/v/<content hash>/live.{js,css}` (immutable); relative module imports and the p2 worker inherit the prefix, the vendored ORT redirects to its stable `/live/p2/ort/` (1 day). A deploy therefore changes every asset URL automatically.

## Common operations

**Update the service code** (pokescan branch `live` or its successor):
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
git -C /srv/repos/pokecollector archive --format=tar.gz -o /tmp/pc.tgz fork/pokescanner
pct push 100 /tmp/pc.tgz /tmp/pc.tgz
pct exec 100 -- sh -c 'cd /etc/komodo/stacks/pokecollector && docker tag pokecollector-backend:local pokecollector-backend:prev && docker tag pokecollector-frontend:local pokecollector-frontend:prev && rm -rf backend frontend && tar xzf /tmp/pc.tgz && docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f compose.tz.yml build && docker compose -p pokecollector -f docker-compose.yml -f docker-compose.build.yml -f compose.tz.yml up -d'
curl -s https://poke.nilss.dev/api/health
```
Never redeploy/pull this stack from Komodo: its `file_paths` lack `docker-compose.build.yml`,
so it would pull upstream's ghcr 1.51.0 images (no external matcher). Fix in Mongo per
`memory/host.md` if you want Komodo to match. If `docker` commands fail on CT 100, the daemon
is stopped again: `pct exec 100 -- rc-service docker start` (containers survive via live-restore).

**Roll back pokecollector**: `compose down --remove-orphans` (no `-v`), restore
`/root/pokecollector-backups/stack-src-pre-pokescanner-20260929-0917.tgz` into
`/etc/komodo/stacks`, retag `pokecollector-{backend,frontend}:pre-pokescanner` → `:latest`,
`docker compose -p pokecollector -f docker-compose.yml -f compose.tz.yml up -d --no-build`.
DB: `pre-pokescanner-20260929-0917.dump` (`pg_restore -U pokemon -d pokemon_tcg --clean`), only
if 1.42.2 fails on the 1.51.0 schema (migrations were additive).

**Rebuild the bundle for a new gallery version** (from a pokescan worktree with `data/`):
```
PYTHONPATH=src .venv/bin/python scripts/build_names_subset.py      # names_vN.json
PYTHONPATH=src .venv/bin/python scripts/build_ref_thumbs.py        # ~1 GB, hours
PYTHONPATH=src .venv/bin/python scripts/export_serve_bundle.py --out /tank/pokescan/bundle-vN
```
Then `bundleDir` in `pokescan-serve.nix` → `update-pokescanner`. `Bundle.load` refuses any
file whose sha256 differs from `bundle.json`, so a half-copied dir fails loudly.

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
