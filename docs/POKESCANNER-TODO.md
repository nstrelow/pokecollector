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

### 1. One rotation per live frame instead of two (likely the biggest win)

**Why.** Every frame embeds the rectified card twice: the pair along the card's long axis
(upright and 180°) from `selectRotations` (`geo2_axis`, `p2/geometry.js:132`, Python
`rotation.select_rotations`). When the axis is unsure (squarish quad) it embeds **all
four**. The tower is a batch of R images, so time is ~linear in R. In a live camera view the
card is almost always upright relative to the phone, so the 180° pass is wasted work.

**Cost / difficulty: small.** It is not a model change: rotations are just images in the
batch, and the API already takes any subset. `CardRecognizer.identify_from_embedding`
accepts `vectors` with 1–4 rows plus a matching `rotations` list (validated at
`recognizer.py:1084`). What changes is the client and a policy knob:

- `selectRotations(quad, "live1")`: one rotation, the one of the long-axis pair whose card
  top faces the top of the camera image (pure function + tests, mirrored in Python
  `rotation.py` so server mode can use it too).
- Fallback, so an upside-down card still works: if the result is weak (not strong by the
  consensus bar, or `NO_MATCH`/`AMBIGUOUS`) for 2 frames in a row, the next frame embeds the
  pair again (or only the 180° one). Unsure axis stays on all four.
- Server mode: `/identify?rotations=live1` (or a `rotation_mode` query) so the server path
  also halves its retrieve stage (765 ms of the ~950 ms today were the batched tower).
- S7's upright roll (reads the card number from the bottom corners of the upright plane)
  uses the max over embedded rotations. With one row it just takes that row; check that
  OCR/fuse still get the right corners.

**Measure first.** Gate run on testset-v2 with `rotation_mode=live1` vs `geo2_axis`: top-1,
wrong-confident, and how many frames are upside down in the set (those should recover via
the fallback on the next frame, which a single-frame gate can't show, so also count them).
**Done when** device tower time halves on the phone and the gate shows no new wrong-confident.

### 2. GPU-friendly tower file

The int8 weight-only tower (`clip_b16_224.int8wo`, 88 MB) is `DequantizeLinear` on weight
initialisers feeding fp32 MatMuls. ORT does not constant-fold DQ nodes, so the WebGPU EP
likely dequantises ~86 M weights every frame, or runs some ops on the CPU. Options:
an fp16 file for WebGPU when the adapter has `shader-f16` (~172 MB download, native on the
GPU), or a MatMulNBits/int4-style format the WebGPU EP runs directly (download stays small).
Keep int8wo as the Wasm/CPU file. Needs the fidelity gate again (cosine vs fp32, gate top-1).

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
