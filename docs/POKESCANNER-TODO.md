# pokescanner — TODO

Open work on the scanner, newest first. State lives in `docs/status.md`; this file is the
backlog. Each item says why, what to measure first, and when it is done.

## On-device speed (P2 live mode)

Measured 2026-09-29 on the owner's Android phone, Chrome, WebGPU: **~1.1 s per device frame
(0.8 fps)**, server share 35 ms. Target: 3–5 fps on the same phone. Server mode feels like
more fps but is choppier: 2 frames in flight, each result 0.7–1 s old when it arrives.

### 0. Get the per-stage breakdown from a real phone (first)

The worker already times `decode · localize · rectify · tower · encode` (`p2/worker.js`,
`p2/pipeline.js`); the drawer shows them since the focus-guide deploy (2026-09-29).
Owner: open the drawer during a scan and screenshot the "device timings" row and the
`shader-f16` flag. Pick the order of items 1–5 from that.

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

### 3. Move rectify + CLIP prep onto the GPU

JS on the CPU today: quad fit, perspective warp, resize/normalise (headless on the loaded
host: localize post-processing ~330 ms, rectify ~140 ms, prep ~170 ms). Either WebGPU
compute shaders, or fold resize + normalise into the ONNX graph so the tower takes the
plane directly.

### 4. Overlap frames

The device pipeline is strictly serial per frame. Run the localizer of frame N+1 while the
tower works on frame N (two in flight inside the worker, the page drops stale results).

### 5. Multi-threaded Wasm for phones without WebGPU

`POKESCANNER_LIVE_ISOLATED=1` (COOP/COEP) enables threads for the CPU fallback (Firefox
Android, older iOS). Check that `/ref/*` thumbnails still load through Caddy/Cloudflare
with COEP before switching it on.

## Other open items

- Rotate the API token (it was pasted into a chat once), recipe in `POKESCANNER-OPS.md`.
- Deterministic RANSAC in JS and Python (same PRNG) so device and server quads match exactly.
- Move the en/de session-language switch into the drawer and default it to the last
  confirmed Latin-script language (it only breaks en/de look-alike ties; ja/ko/zh/th are
  detected by script).
- Disk alert for CT 140 traces (sweeper caps at 10 GB, rootfs 20 GB).
