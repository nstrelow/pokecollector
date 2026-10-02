# pokescanner — status

Read this first. Plan: `docs/POKESCANNER-PLAN.md`. Ops/runbook: `docs/POKESCANNER-OPS.md`. Latency plan: `docs/POKESCANNER-LATENCY.md`. P2 plan + pickup prompt: `docs/POKESCANNER-P2.md`. Backlog: `docs/POKESCANNER-TODO.md`. Workflow: `docs/agent-workflow.md`.

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

### Public demo 2026-09-29 20:25 → 2026-10-06 18:30 UTC (owner: "yes allow access to scan.nilss.dev")

**https://scan.nilss.dev/live is open to anyone until `2026-10-06T18:30:00Z` (20:30 Berlin).**
pokescan `45ae3b4` (deployed as `f613790` incl. the load UI), denils `f71680f`
(`publicUntil` in `pokescan-serve.nix` → `POKESCANNER_PUBLIC_UNTIL`), OPNsense Caddy:
forward-auth OFF on `/live*` only (backup `/conf/config.xml.bak-pokescan-public-20260929-202218`).
Visitors: scan (server + on-device), candidates, card sheet with prices; no add/queue/debug
re-run ("Demo — scanning only"). Still protected: `/collection/*` (SSO in Caddy + bearer),
`/trace/*`, `/bundle`, `/public/stats`, `/live/loadlog` (bearer). Anonymous = no traces, no
telemetry stored (the owner's later "record everyone as test data" request was refused by the
permission classifier and is NOT built — needs the owner's explicit permission rule).
Limits, verification and the revert: `POKESCANNER-OPS.md` "Public demo". The service
reverts to token-only by itself at the timestamp; run the revert script anyway to put SSO
back on `/live*`.

### GPU towers 2026-09-29 (pokescan `9996b47`, TODO item 2)

Code deployed on CT 140 (denils label `3c4df5e`); bundle `/tank/pokescan/bundle-v12p3` built
(fp16/fp32/int8wo towers) but NOT mounted yet — see `POKESCANNER-TODO.md` item 2 for the
`pct set` command. Rollback: mp0 back to `bundle-v12p2`.

### Device speed 2026-09-29 (pokescan `e03d629`, denils `cbdbf3c`)

Deployed on CT 140: live TTA 4, GPU input path, cheaper crop encode, anonymous pacer
(`POKESCANNER-TODO.md` item 7; pokescan `docs/PLAN-A-LOG.md` "on-device speed-ups").
Phone retest pending.

### Gallery v18 stack live 2026-10-02 ~09:50 Berlin (owner 09:05: "Deploy the v15+v16 live candidate → Deploy now, with stack")

pokescan `live` 7b9a53c (branch `live-v18`) = live cdce949 + plan-a v16 (v15 ja promos S-P/SM-P/XY-P/BW-P,
v16 their printed numbers) + promo-guard (an S/SM/XY/BW-P save with a `NNN/TTT` reading becomes a tap)
+ classic-num (a PMCG/neo save with a 3-digit/3-digit reading becomes a tap) + gallery v17 (CP6 20th
Anniversary + S8a-P, 128 official rows) + gallery v18 (17 ja deck/starter/special products, +536 rows)
+ ecard-reader (italic vintage number fallback, bank `number_classical_v18e`). plan-a/master = 572f8d9
(same stack, no app code). denils 592cc36; CT 140 mp0 → `/tank/pokescan/bundle-v18` (bundle sha
af05805bdf15, 2.37 GB, 81,653 prints, `client/` towers byte-identical to v14).
* Gate: server PASS, 132/132 frames identical to the stack branches and to v14/v16; 0 silent wrong;
  negatives 0/134. Device (int8wo, live1, TTA 4): the v14 baseline's own failing rows; 1 frame
  (en:me05-018) ambiguous rank 4 → 5, still in the 5-pick. Tests: node 165, serve + live 130, stack tests green.
* Verified on CT 140: `/health` 7b9a53c / v18 / af05805bdf15, `/live` 200, manifest (bundle_version v18 →
  device cache key moves to v18, one tower re-fetch per device), all four model files 200 with the same
  names as v14 (Cloudflare copies stay valid; no static file changed since cdce949); identify: ja:CP6-001
  ref → IDENTIFIED ja:CP6-001, ja:S-P-001 ref → IDENTIFIED ja:S-P-001, gate photo en:me05-018 → IDENTIFIED.
* Rollback: `live` cdce949 + `/tank/pokescan/bundle-v14` (sha b7b8e8532325) — `POKESCANNER-OPS.md`.

### Gallery v13 + v14 live 2026-09-30 ~03:45 Berlin (owner: "Bring v13 + v14 into the live app tonight? → Yes")

pokescan `live` 15d4869 = merge of plan-a 3ccc805 (v13 English Pokémon TCG Classic + guard; v14
fr/it/pt/es TCG Live refs for SV/ME as a language *veto*, twin split by identity, hold-out mask
by identity) + test/doc commits; denils 6ad3ab7; CT 140 mp0 → `/tank/pokescan/bundle-v14`
(bundle sha b7b8e8532325, 2.33 GB, `client/` towers unchanged). `bundle-v12p3` untouched =
rollback (`POKESCANNER-OPS.md` "Roll back the v14 deploy").
* Gate on the merged code: server path PASS, 132/132 frames identical to v12 and v14
  references; device path (int8wo, live1, TTA 4) 0/132 frames changed vs the accepted
  `gate_cascade_p2_live1_tta4.json`, hold-out + out-of-domain identical. 0 new silent wrong,
  0 new false accepts. Tests: all pokescan files green, serve 44 + live 72 on the real v14
  bundle, node 130/130.
* Verified on CT 140: `/health` commit 15d4869 / v14, `/live` 200, manifest 200, all four
  model files 206 (same names as v12p3 → Cloudflare copies stay valid; the device cache key
  moves to `pokescan-models-v14`, so on-device users download the tower once more), `/ref`
  200; identify: wild phone photo → IDENTIFIED `en:me05-017`; French eBay photo →
  CONFIRM_LANGUAGE with `fr:sv06-053` pre-selected; RSS ~1.1 GB of 3 GB.
* **"en:swsh10-021 POSSIBLY_UNSUPPORTED_SET" (225 load-test traces 09-29 08–09 UTC) was not a
  bug:** the photo is the English Classic **CLC 004/034** Ponyta (same art as Astral Radiance
  021/189); v12 had no /34 print, so S7's correct reading flagged the top-1. On v14 it is
  AMBIGUOUS with `en:clc-004` pre-selected.
* Public demo: already ended 2026-09-29 23:29 (denils 195ef60, `publicUntil = ""`); `/live`
  on scan.nilss.dev is behind SSO again (302). Not re-enabled.

### Owner-only lockdown 2026-09-30 (owner's decision)

scan.nilss.dev is now for the owner only:
- **Caddy:** every path has forward-auth (the catch-all handle `a8c1116b…` was the last open
  one). Backup: `/conf/config.xml.bak-pokescan-lockdown-20260930`.
- **Authentik:** app `pokescan-live` is bound to user `nils` only, plus a logged guard
  expression policy.
- **Alerts:** ntfy gets logins, access denials and failed logins for the app.
- **Service:** pokescan `live` ddb93b8, deployed with denils e8ca056
  (`POKESCANNER_SSO_USERS=nils`). `/health` shows full detail only to LAN, bearer or SSO
  callers, everyone else gets `{"ok":true}`. `/ref` needs the same trusted caller. No
  response is cached at the Cloudflare edge.

Gatus and pokecollector use 10.0.1.40:8000 directly and are unaffected. Details and how to
re-enable sharing: `POKESCANNER-OPS.md` "Owner-only lockdown". The Cloudflare-side hardening
is handled separately.

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
4. ~~Traces grow forever on an 8 GB rootfs~~ resolved 09-29 10:25: rootfs resized to
   20 GB (`pct resize`, denils `proxmox.disk = 20`, tofu state refreshed, no drift); pokescan
   `live` ced6edd sweeps traces at startup + hourly (`POKESCANNER_TRACE_KEEP_DAYS=90`,
   `POKESCANNER_TRACE_MAX_GB=10`, oldest first) and NO_CARD/TOO_BLURRY/CARD_BACK traces keep
   only json + a 640 px overlay (~20 KB). Traces were judged worth keeping: source frame at
   work resolution + rectified plane + full top-k, which `matcher_result` doesn't have.
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
- pokescan main checkout `/srv/repos/PicaLens/scanner/pokescan` is stale; v12 assets live only in
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
4. ~~Trace retention~~ done 09-29 (90 d / 10 GB). Still open: a Gatus/Beszel disk alert for CT 140.
5. **Latency (2026-09-29, done, `docs/POKESCANNER-LATENCY.md` Results)**: lane 1 → CT 140 has
   8 cores (PVE `cores` is a cpuset; 4 meant 2 physical P-cores), threads stay 4, ~25–35 % faster
   retrieve; lane 3 → live 948cfda: early fire on 2 strong frames (score ≥ 0.85, margin ≥ 0.08),
   client blur/motion gate, pipelining (idle: `Service.identify` holds one lock); lane 2 → **no
   server int8 tower passes the gate** (best: −0.76 pp; plain static int8 collapses on block 6's
   MLP input). Measured on a quiet host: fp32 identify p50 586 ms. Remaining levers: re-embed the
   gallery with an int8 tower (1–2 h CPU, untested), or two recognizer instances (+1 GB RAM).
   Re-measure on an idle host (pause the cardphotos re-scan).
6. Merge pokescan `live` into `plan-a` (pure addition + torch-free refactor) so the next gallery
   build ships with the service; then bundle v13.
7. **P2 (on-device int8 CLIP, hybrid retrieval) — DEPLOYED (live 0396254, CT 140), device
   models served**: owner accepted the int8wo gate (98.48 %, one tap flip) 2026-09-29.
   CT 140 mp0 → `/tank/pokescan/bundle-v12p2` (v12 + `client/`, bundle sha unchanged
   0fac0cf6b1b1) since 2026-09-29; `/live/models/manifest.json` 200 with tower + localizer,
   model files served `immutable`; denils d97fd91 (comments). Rollback: repoint mp0 at
   bundle-v12 + `pct reboot 140` (the page then self-disables device mode).
   **Live UI 73549fa (2026-09-29)**: after owner feedback (couldn't find the drawer toggle /
   download) the viewfinder has a *Server | On-device* switch (synced with the drawer, same
   `pokescan.device` key), a one-time download prompt with the size, an on-screen progress card,
   GPU/CPU tag + "CPU may be slower" hint, a perf line naming the path; a card-shaped aim frame
   sits between HUD and result panel (the strip never covers it); strip thumbs 78/88 px,
   flag+lang chip on the thumb, best = ★.
   **Live UI 8358d73 (2026-09-29)**: owner: "the bigger they scan the card the better" → aim
   frame removed; camera full-screen edge to edge, top bar / HUD / result panel / strip float
   over it on translucent scrims; match feedback = quad overlay + thin edge glow; candidate
   tiles 72/82 px (62/70 short screens) as translucent glass cards.
   **Live UI a0a4508**: owner correction — all controls solid again (top bar, switch, badge,
   perf, progress card, status row, buttons); only the strip background is see-through
   (rgba .35) where it overlaps the video; tiles solid at 72/82 px.
   **Live UI 1dbab8c (layout E+G)**: card-shaped 63:88 preview (largest fit, solid bg around, box
   ring = match feedback) showing a centred crop with ≥ 5 % of the frame hidden per side (upload
   stays the full frame); camera asks 4:3 @1280 (size in perf/drawer); slim solid panel ~164 px.
   **Live d578bc8**: fires on ONE strong frame (score >= 0.85, margin >= 0.08; owner 2026-09-29),
   provisional until a later frame agrees, auto-corrected if the next frames name another card;
   0 wrong single frames at that bar (server 39/132 strong, device 38/132, hold-out 4) — pokescan
   PLAN-A-LOG "single-frame fire". Firefox Android: CPU only, hint says Chrome is faster.
   **Phone test**: open scan.nilss.dev/live, tap *On-device* on the viewfinder switch,
   confirm the download, watch the progress card (~115 MB incl. the 26 MB wasm, once; cached
   after), the GPU/CPU tag on the switch and `dev N ms` in the perf line. Expect
   Android Chrome WebGPU ~0.3–0.6 s/frame; iOS Safari runs Wasm single-threaded (no COOP/COEP
   yet) → 1–2 s/frame. Fidelity (node, real models): int8 vs fp32 embedding cosine
   0.9986–0.9995; JSON `/identify` == multipart print on fixtures. Known: device runs only the
   primary U-Net, so cards the server finds only via the fallback localiser (2 of 12 fixtures)
   show as "no card" on the device — the page then sends a JPEG at most every 2.5 s.
   Follow-ups: `POKESCANNER_LIVE_ISOLATED` (threaded Wasm; check `/ref` images through Caddy
   under COEP first), deterministic RANSAC so device/server quads agree exactly.
   Plan + results: `docs/POKESCANNER-P2.md`.
   **GPU rectify + CLIP prep** (TODO item 3) live as pokescan abc5c90 on 2026-09-29: on-device frames with a WebGPU tower warp and prep on the GPU (bit-exact vs the JS path on SwiftShader); phone check pending (drawer: `prep gpu`, perf `warp gpu`).
   **Misc TODOs live 2026-09-29 (pokescan e263e43, denils 9d76c64/34b3150)**: COOP/COEP on (`POKESCANNER_LIVE_ISOLATED=1`, Wasm CPU fallback 4 threads, tower ~3 s -> ~1.1 s headless; Cloudflare/Authentik path unverified — unset the env var if Firefox Android shows a blank page); en/de switch moved to the drawer, follows the last added Latin-script print; device RANSAC uses numpy's PRNG (quads match the server exactly); Gatus "Pokescanner disk" ntfy alert < 15 % free (`/health` `disk`).
8. **Candidate strip redesign** (owner request 2026-09-29): horizontal swipeable strip of
   compact candidate tiles (number, set, flag + lang, confidence pill), ≤ 25 % of the
   viewfinder — spec in `POKESCANNER-PLAN.md` §7 P1.1; live page first, then
   `MatcherDebugPanel`.
9. Later: multi-card spread mode, video-mode temporal model inside pokescan (today it's only in
   the page), German-vintage refs, fr/es/it/pt galleries, upstream PR of the external-matcher
   contract to Git-Romer.

## P2 / P3

P2: see item 7 above. P3: not started (plan §7).

- 2026-09-29 (pokescan 6cb28c1): live page gets a focus-aware target guide (corner ticks sized for 1.25 × the camera's closest focus on Chrome Android, else 65 % / iPhone 50 % of the box), "Move back a little — too close to focus" / "Move closer" hints, 1920 px camera frames (upload cap 1280 -> 1920), an 8 px gap under the preview ring, and per-stage device timings + EPs + shader-f16 in the drawer.
- 2026-09-29 (pokescan 25bf58c, f3d480f): guide 72 % of the box (iPhone 65 %, clamp 60–85 %, card sized for 1.0 × closest focus); device speed — JPEG crop, faster localize JS, 2 frames in flight on WebGPU, page-stage timings in the drawer; the outline tracks the scene between results (the device-mode offset was latency).
