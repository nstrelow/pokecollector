# pokescanner — TODO

Open work on the scanner, newest first. State lives in `docs/status.md`; this file is the
backlog. Each item says why, what to measure first, and when it is done.

## Open tasks checklist (2026-09-30)

State: pokescan `live` = ddb93b8 (bundle v14, speed-ups e03d629, owner-only lockdown), denils e8ca056.

### Owner
- [ ] Open `/live` on the phone after the Authentik login: confirm page, card thumbnails (/ref) and prices load under SSO.
- [ ] Phone retest of the speed-ups; screenshot the drawer. Expect: `localiser webgpu (TTA 4)`, `frame→GPU texture`,
      `decode` ~0, separate `readback`, new `steps:` line (localize prep/wait/run/post, tower wait/run), real KB on the crop row.
      Phone re-downloads the tower once (cache key `pokescan-models-v14`).
- [ ] Decide GitHub: (a) make `live` the default branch of nstrelow/pokescan, (b) separate clean scanner-app repo, (c) make public after review.
- [ ] OK for ORT `allow_spinning=0` in the server sessions (clip.py, maskfit.py, numbers/reader.py, verify.py): ~-100 ms in Server mode.
- [ ] OK for CT 140 RAM 3 -> 4 GB (main session runs `pct set`, ~30 s downtime; service had 429 MB in swap).
- [ ] Test Firefox CPU mode (`wasm x4` or thread retry) and the "sign in" -> add-to-collection round trip.
- [ ] Rotate the pokecollector API token (was pasted in chat once); then update the agenix secret.
- [ ] Anonymous test-data collection from visitors was denied as PII; only revisit if the owner adds a permission rule / re-authorises explicitly.

### Claude
- [ ] Read the phone's `steps:` line; decide next speed step (lock waits vs tower vs net). Ideas: ORT WebGPU graph capture + gpu-buffer outputs for the tower, cheaper `grab` (39-71 ms, two willReadFrequently canvases), batch-1 tower warm-up (needs fallback-test rework).
- [ ] Cloudflare edge layer (deferred; see the section at the bottom): Access with Authentik as IdP, EU/EEA+UK+CH geo filter, bot filter. Needs a scoped token in /root/.config/cloudflare/token or owner clicks.
- [ ] `/docs` and `/openapi.json` still open to LAN callers; lock or disable if wanted.
- [ ] Price backlog: variant-aware headline, ja->en fallback, history.
- [ ] Sideways two-view decision (TTA 4 loses 4 sideways auto-corrects; upright rows gain +1).
- [ ] SSO instead of the browser bearer token in the page, then rotate the token.
- [ ] Server RANSAC least-squares refinement.
- [ ] v15 (+ Japanese part 2) deploy is owned by the `pokescanner` session; it pulls `live` first.
- [ ] Public demo code is inert (publicUntil empty); delete it or keep for a future hardened demo.

## On-device speed (P2 live mode)

Measured 2026-09-29 on the owner's Android phone, Chrome, WebGPU: **~1.1 s per device frame
(0.8 fps)**, server share 35 ms. Target: 3–5 fps on the same phone. Server mode feels like
more fps but is choppier: 2 frames in flight, each result 0.7–1 s old when it arrives.

### 0. Get the per-stage breakdown from a real phone (first)

The worker already times `decode · localize · rectify · tower · encode` (`p2/worker.js`,
`p2/pipeline.js`); the drawer shows them since the focus-guide deploy (2026-09-29).
Owner: open the drawer during a scan and screenshot the "device timings" row and the
`shader-f16` flag. Pick the order of items 1–5 from that.

Owner's numbers 2026-09-29 (fp16 tower, bundle-v12p3): 969 ms/frame = loc 234 · warp 179 ·
clip 222 · srv 18, ~316 ms outside the worker stages. Since pokescan `f3d480f` the drawer also
shows the page's side of a frame: `page: grab · bitmap · xfer · body · net` (grab = canvas copy,
bitmap = createImageBitmap, xfer = worker round trip minus its own total, body = JSON build,
net = /identify). Owner: screenshot both lines again.

### 1. ~~One rotation per live frame instead of two~~ — DONE 2026-09-29 (pokescan `1ac08da`)

Live camera frames (device and server mode; Snap reuses them) embed exactly ONE view,
`rotation_mode=live1`: the rotation whose plane top edge is highest in the camera image.
Owner: "never do flips in live feed, not needed, people should hold their phone upright" —
no upside-down fallback, no state machine. Photo uploads (📷) keep `geo2_axis` (both views
along the long axis) because EXIF orientation is unreliable with the phone lying flat.
Server: `/identify?rotation_mode=live1` (multipart); device: `dev.process(bmp, {rotationMode})`.

Results (testset-v2 gate, 132 photos; details in pokescan PLAN-A-LOG "one rotation per live
frame"):
- Card upright/upside down in the image (64 photos, the live-feed case): top-1 63 → 63,
  auto-correct 55 → 55 — identical to the pair at half the tower work.
- Card lying sideways in the image (64 landscape photos): 64 → 36; one view is a coin flip
  between 90° and 270°. Squarish quads (4): 4 → 2. **0 silent wrong, 0 false accepts** in
  every group; misses become AMBIGUOUS/picker. A sideways card in a portrait live feed means
  the card is held across the phone. If that matters in practice: embed the pair only for
  sideways cards (not a flip — no "up" exists there). Owner call, not done.
- Cost: server S1–S3 p50 2587 → 1478 ms on the loaded box; a real frame retrieve 518 → 276 ms
  (total 617 → 411 ms); tower alone (int8wo, Wasm, 1 thread) 1 row 2550 ms vs 2 rows 5045 ms.
  On the phone the tower stage should roughly halve — confirm with the drawer's device timings.

### 2. ~~GPU-friendly tower file~~ — CODE DONE 2026-09-29 (pokescan `9996b47`); bundle switch pending

The int8wo tower is `DequantizeLinear` on weights; ORT re-runs the DQ every frame on WebGPU.
Now `manifest.towers = {webgpu_f16, webgpu, wasm}`: fp16 (173 MB) for adapters with
`shader-f16`, fp32 (345 MB, the server's own file) for WebGPU without it, int8wo (88 MB) for
Wasm/CPU incl. Firefox Android. Picked in `p2/select.js`; the download sheet shows the chosen
file's size; the drawer shows `tower … (fp16|fp32|int8wo[, X failed])`; a GPU tower that fails
to load falls back to the int8wo file on Wasm. JSON `/identify` with `debug=1` returns
`parity` (server fp32 cosine per rotation) to check a phone's fp16 output against the server.

Fidelity (200 testset-v2 planes, cosine vs fp32): fp16 file on ORT CPU p50 0.999999 /
min 0.999993; fp16 emulation p50 0.999998 / min 0.99999; int8wo p50 0.99945 / min 0.99805.
Gate (fp16 emulation): top-1 99.24 % = fp32, 0 silent wrong, 0 false accepts; recovers the
int8 flip ja:M5-075. Real ORT-web WebGPU EP (SwiftShader, headless): fp32 cosine 1.000000;
fp16 unverified on a real f16 GPU (SwiftShader has no `shader-f16`) — use the debug parity.

Pending: switch CT 140 to `/tank/pokescan/bundle-v12p3` (main session):
`pct set 140 -mp0 /tank/pokescan/bundle-v12p3,mp=/var/lib/pokescanner/bundle,ro=1 && pct reboot 140`.
Until then the manifest has `towers: {wasm}` only and behaviour is unchanged.

### 3. ~~Move rectify + CLIP prep onto the GPU~~ — done 2026-09-29 (pokescan abc5c90)

`p2/gpuprep.js`: two WebGPU compute passes on ORT's own device (the float32 `warpPlane`
formula; rot90 + INTER_AREA with geometry.js' own taps + CLIP normalise); the tower reads the
input as a GPU buffer (`ort.Tensor.fromGpuBuffer`), the plane is read back only for the crop.
On whenever the tower runs on WebGPU; JS stays the fallback (Wasm tower, create failure, any
GPU error -> off for the session). No new model files, no bundle change.
Parity (headless Chromium, SwiftShader WebGPU): plane **bit-exact** on a synthetic frame
(rotations 0-3) and 4 real testset-v2 photos at 1280 px, CLIP input max |diff| <= 4.8e-7,
fp32 tower cosine (JS input vs GPU buffer) **1.0000000** -> same vectors, same print_id.
Time there (SwiftShader emulates the GPU on the CPU): JS warp+prep 225-343 ms vs GPU ~33 ms
+ 5-7 ms readback. Expected on the owner's phone: ~200-250 ms off the 969 ms frame; the perf
line now reads `warp gpu` (the GPU time is inside `clip`), the drawer `prep gpu` and a
`readback` stage. Still on the CPU: the U-Net's letterbox/TTA/sigmoid + quad fit (`loc`) and
the crop encode — both made cheaper in pokescan `f3d480f` (item 4).

### 4. ~~Overlap frames~~ + cheaper encode/localize — DONE 2026-09-29 (pokescan `f3d480f`)

- **Overlap:** the worker runs frames concurrently; Wasm sessions each have a run lock, all
  WebGPU sessions share one (two ORT runs on its device hang), GPU prep -> tower -> plane copy
  is exclusive (shared buffers). The page keeps **2 device frames in flight when the tower is
  on WebGPU** (1 on Wasm: no gain on one CPU). A frame's 3 s timeout grows by 3 s per frame in
  flight with it. Tested: two different cards + an empty frame in flight each equal their solo
  run (Wasm and SwiftShader GPU prep); without the GPU-prep lock that test fails.
- **Crop encode:** JPEG 0.85 instead of WebP 0.8 — Chromium 9.6 vs 106 ms (83 vs 39 KB);
  testset-v2 JSON-path tally identical to lossless and WebP.
- **Localize JS:** cached dihedral gather maps, typed area-resize tables, one sigmoid+undo
  pass; bit-identical. node: letterbox 71 -> 26, dihedral 33 -> 6, clip_prep 66 -> 30 ms.
- Not measurable end to end here (the headless Wasm tower is 2 s on this host; SwiftShader
  WebGPU timed out under load). Expected on the owner's phone with the GPU prep: ~0.5 s of
  work per frame, period near the longest stage with 2 in flight -> **~2-3 fps**. Confirm
  with the drawer's worker + page lines.
- Also: the on-device outline offset (above/right of the card) was result latency — the quad
  of a ~0.5-1 s old frame over live video. The outline now follows the scene (global luma
  shift at 10 Hz, `track.js`) and hides when the scene changed.
- Open, codec-independent: 2 IDENTIFIED-wrong on testset-v2's JSON live1 path (lossless too).

### 5. ~~Multi-threaded Wasm for phones without WebGPU~~ — DONE 2026-09-29 (denils `9d76c64`)

`POKESCANNER_LIVE_ISOLATED=1` (COOP/COEP) is on. Headless check: page, `/ref` thumbnails
(CORP), worker and ORT threads (`min(hardwareConcurrency, 4)`) all fine, nothing blocked;
Wasm tower 1 thread 2.6-3.7 s -> 4 threads 1.0-1.25 s. Not verifiable through
Cloudflare/Authentik from the server side (all subresources are same-origin, so it should be
fine): if Firefox Android shows a blank page or no thumbnails, unset the env var.

### 6. Slow on-device model load — UI + telemetry DONE 2026-09-29 (pokescan `f613790`, denils `7a0a8c0`); phone check open

Symptom: "Loading the model…" for minutes on the owner's Android (5G). CT 140's log shows the
19:40-19:55 loads never fetched a model file (bytes came from Cache Storage; ORT wasm requested
~3 s after the manifest), so the time was the GPU set-up (adapter / session create / warm-up),
shown as one indeterminate bar whose sliding chunk looked like "15 %/30 %". The 17:28 load of
the same fp16 tower was ready in <25 s, so the minutes-long cases are a stall on the phone's
GPU side, not the download.
Shipped: stage + percentage ("Downloading 42 % · 73 / 173 MB · 6.1 MB/s", "Cached ✓",
"Checking…", "Waiting for the GPU…", "Preparing GPU… 12 s · tower"), same line in the drawer;
cache hits trusted by verified marker + size (no 173 MB re-hash); streamed download into one
buffer + tee'd cache.put (errors reported); one requestAdapter per page with a 20 s give-up;
`storage.persist()`; drawer telemetry (source, MB, s, MB/s, put error, set-up step times,
a previous load that never finished); `POST /live/loadlog` -> `journalctl -u pokescanner | grep loadlog`.
- [ ] Owner: reload /live on the phone, note the stage/seconds it shows; then read the
  `loadlog` lines (event ready/stall/failed/cut-off, `steps`, `stages`) to see which step is slow.

### 7. ~~Device frame ~430 ms (Pixel 9 Pro Fold)~~ — DONE 2026-09-29 (pokescan `e03d629`, denils `cbdbf3c`)

- Live-frame localiser TTA 8 -> **4** (manifest `localizer.tta`; photos/server keep 8). Gate
  (int8wo, live1, testset-v2 132 rows): 0 silent wrong, 0 false accepts, corner error = TTA 8;
  TTA 2/1 rejected (2 wrong cards shown, +1 pp corner error). U-Net cost ~halves.
- GPU input path: ImageBitmap -> GPU texture -> WGSL letterbox/TTA batch + warp; no
  getImageData readback (bit-identical to JS on 40 frames, SwiftShader).
- Crop encode on one reused CPU canvas; drawer "crop N KB" replaces the stale "0 KB".
- Anonymous demo pacer: ~1 identify/s (0.5/s while locked/confident), Retry-After honoured.
- [ ] Owner: retest on the phone; drawer `device` line should say `localiser webgpu (TTA 4)`
  and `frame→GPU texture`; `device timings` worker line `decode ~0`, `readback` split from
  `encode`; new `steps:` line = localize prep/wait/run/post + tower wait/run (lock waits).

## Prices — SHIPPED 2026-09-29 (pokescan `68be661`, denils `aa30b8a`)

The live page shows market prices for the identified card.

- **Source: TCGdex directly**, not pokecollector. pokecollector's prices come
  from the same TCGdex `pricing` block, but only for cards it tracks
  (collection/wishlist/binder), behind a login, and `GET /api/cards/{id}` for an
  untracked card fetches TCGdex and writes a DB row, a side effect the scanner
  should not cause. Scanner print ids (`en:sv03.5-043`) map 1:1 to TCGdex
  `/{lang}/cards/{id}`.
- **Endpoint:** `GET /price/{print_id}` on the scanner (bearer like `/identify`;
  404 for unknown print ids without an upstream call). Returns
  `cardmarket {unit, updated, trend, avg, low, avg1/7/30, reverse{...}}` and
  `tcgplayer {unit, updated, variants{normal|holofoil|reverse-holofoil|…: market/low/mid/high}}`,
  either `null` when there is no price (zeros are dropped, never shown as 0).
- **Cache:** memory + `/var/lib/pokescanner/prices/prices.json`
  (`POKESCANNER_PRICE_CACHE`), 18 h TTL (`_TTL_H`), "no such card" 6 h,
  4 s timeout, one upstream call per print id, stale entry served on upstream
  failure with a 2 min backoff. Sync route (threadpool), never on `/identify`'s path.
- **UI:** the add sheet shows "Cardmarket trend · low · 30d", a rev. holo line,
  "TCGplayer normal · rev. …" and "via TCGdex · dd.mm."; the top candidate tile
  shows the trend in a small opaque badge (left, opposite the score pill) once
  that card's price is known. Fetched only when the sheet opens (lock / pick /
  snap / photo), cached per print id in the page.
- **Coverage (sample of 15 per language):** en and de ~90% Cardmarket + all
  TCGplayer (same product, so identical numbers across en/de/fr); ja ~50%
  (half the ja prints are not in TCGdex at all), Cardmarket only; zh-tw ~60%
  Cardmarket only, no TCGplayer.

Left:
- Variant awareness: the sheet shows normal + rev. holo side by side; tie the
  headline to the variant picked in the sheet.
- ja prints missing from TCGdex could fall back to the en sibling
  (pokecollector's `apply_cross_language_fallbacks` idea), clearly labelled.
- Price history / sparkline (pokecollector has `PriceHistory` for tracked cards).

## Other open items

- Rotate the API token (it was pasted into a chat once), recipe in `POKESCANNER-OPS.md`.
- ~~Deterministic RANSAC in JS and Python~~ — DONE 2026-09-29 (pokescan `e263e43`): the
  device fit uses a JS port of numpy's `default_rng`; 57/59 real masks bit-identical, max
  0.1 mask px. Server unchanged. Follow-up idea: the server's RANSAC is seed-sensitive
  (median 0.6 % quad spread across seeds), a least-squares refinement would steady it.
- ~~Move the en/de switch into the drawer~~ — DONE 2026-09-29 (pokescan `02ebafe`):
  "Default for English/German look-alikes", follows the last added Latin-script print.
- ~~Disk alert for CT 140 traces~~ — DONE 2026-09-29 (pokescan `fdfa89d`, denils `34b3150`):
  `/health` reports `disk.free_pct`; Gatus "Pokescanner disk" alerts via ntfy below 15 %.
- ~~Server-mode upload cost~~ — DONE 2026-09-29 (pokescan `9a00c3b`, denils `243ff1c`): live
  frames go up at 1280 px (was 1920), JPEG q0.8 encoded in a worker (OffscreenCanvas,
  toBlob fallback); photo uploads untouched. Gate (132 rows, multipart live1): top-1 75.00 ->
  76.52, auto 63.64 -> 64.39, silent wrong 0, FA 0; ~-49 % bytes. Check the drawer's `enc ms`
  on the phone (expect ~45-120 ms, ~70-100 KB, "worker"). Proposed, not done: ORT
  `allow_spinning=0` on the server sessions (replica retrieve 336 -> 214 ms), CT 140 +1 GB RAM
  (it swapped 429 MB); keep 4 threads (6/8 are slower on its P/E-core mix).
- ~~On-device load hung at "session localizer" (Firefox + Chrome Android)~~ — FIXED 2026-09-29
  (pokescan `8f83476`, denils `e0a4849`): ORT's day-cached pthread script from before
  `LIVE_ISOLATED` had no COEP, so its pthread workers never started under COEP. Runtime now
  loads from `/live/p2/ort/<file>?coi=1` (no 308), asset version carries `-coi`, and a 10 s
  watchdog restarts a stuck threaded start with 1 thread (remembered per UA). Drawer shows
  `wasm ×4` / `wasm ×1 (threads hung, retried)`. Phone check: expect ready with `wasm ×N`.

## Cloudflare edge layer for scan.nilss.dev (owner decided 2026-09-30, deferred)
Owner-only lockdown via Authentik + Caddy is live (see OPS "Owner-only lockdown"). Cloudflare part was
skipped for now; when picked up, the owner's choices are:
- Cloudflare Access app on scan.nilss.dev with **Authentik as OIDC IdP**, allow only djnilse@gmail.com,
  long session (~30 d) so the phone rarely re-logs in.
- Geo filter: allow **EU/EEA + UK + CH** only.
- Bot filter (Bot Fight Mode / managed challenge for non-authenticated traffic).
- Needs a scoped API token in /root/.config/cloudflare/token (chmod 600) or owner clicks in the dashboard.
  Tunnel = cloudflared on vigil CT 132 (dashboard-managed ingress).
