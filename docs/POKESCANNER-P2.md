# pokescanner P2 — on-device embedding, hybrid retrieval

Full plan + a pickup prompt (bottom). Design context: `POKESCANNER-PLAN.md` §7 P2, decisions §2
(owner 2026-09-28: int8 on-device CLIP ~85 MB OK, fp32 rejected (~1 GB resident kills mobile
tabs), no smaller tower, P3 = fully offline later). Latency work that runs in parallel:
`POKESCANNER-LATENCY.md`.

## Goal

The phone runs the localiser + CLIP tower itself and sends **two 512-d vectors + a ~50 KB
plane crop** instead of a 200 KB JPEG per frame. The server keeps retrieval, number OCR, twin
check and the decision logic, so the response is the same §4.2 JSON and the P1 page UI is
unchanged. Win: no upload of the frame, no server tower time (the 765 ms stage disappears),
per-frame cost becomes the phone's tower time (~0.3–0.5 s WebGPU, ~1–1.5 s Wasm) + a ~50 ms
server call. Fallback: any failure on the device → the P1 JPEG path, automatically.

## What exists (don't rebuild)

| Piece | Where | Notes |
|---|---|---|
| fp32 tower | `data/exp/onnx/clip_b16_224.onnx` (345 MB), input NCHW 224, `CLIP_MEAN/STD` in `src/pokescan/retrieve/clip.py` | `_prep` = the exact preprocessing to port |
| weight-only int8 export | `scripts/export_int8_weightonly.py` (per-output-channel, cosine p50 0.9996 to fp32, `docs/NEXT-2026-09-22.md`) | quality-safe; ~85–105 MB; the on-device candidate. NOT faster on CPU server, irrelevant here |
| localiser | `unet-m3.onnx` + `.data` (650 KB), `src/pokescan/localize/maskfit.py` (`MaskLocalizer`: threshold 0.5, mask → largest contour → quad fit `FitParams`) | port mask→quad in JS; the classical fallback + `unet-m4` refine stay server-side (P2 sends the crop, the server can refine on the crop if needed) |
| rectify | `src/pokescan/geometry.py` `rectify_card` → `PLANE_W × PLANE_H` portrait plane (constants in `rotation.py`) | homography warp; port with a 4-point perspective on canvas (or a 3×3 in a WebGL/WebGPU shader; a CPU implementation on a 224-target is fine) |
| rotations | `src/pokescan/rotation.py` `select_rotations(quad, "geo2_axis")`: 2 rotations when the long axis is sure, else 4 | port `candidate_pair` + `axis_is_sure` (pure geometry) |
| service | `src/pokescan/serve/app.py` `/identify` multipart; `schema.py` | add JSON mode |
| page | `src/pokescan/serve/static/live/{live.js,consensus.js,…}` (+ `live-ui` candidate strip, `live-fast` framegate/pipeline lanes merging into `live` on 2026-09-29) | P2 swaps only the frame loop's "make request" step |

## Work breakdown (each step is testable on its own; ~1–2 days total)

**S0. Branch.** `p2-device` off `origin/live` (after `live-ui` and `live-fast` have merged, or
rebase later; until then don't edit `live.js`). Worktree `/srv/repos/pokescan-wt/p2`, `data`
symlink to `/srv/repos/pokescan-wt-r2/data`, venv `/srv/repos/pokescan-wt-r2/.venv`.

**S1. Server JSON mode** (`app.py`, `schema.py`, tests in `tests/test_serve.py`):
`POST /identify` with `Content-Type: application/json`
`{"vectors": [[512 floats], …], "rotations": [0, 2], "crop": "<base64 webp of the rectified plane>",
"quad": [[x,y]×4], "image_size": [w,h], "client": {"tower": "clip_b16_int8_wo", "ep": "webgpu"}}`.
Server: L2-normalise, run the same retrieve/twin/OCR/decision on the given vectors + crop (find
the point in `CardRecognizer.identify` after the tower call and add a `identify_from_embedding`
entry — the crop replaces the rectified plane for OCR/twin/card-back). Same response shape,
`timings_ms.retrieve` now ≈ the cosine search only, plus `client` echoed in `matcher`. Reject
wrong dims / non-finite / > 4 vectors. Bearer auth unchanged. Trace: store the crop as `plane.webp`
and a `source: "client"` marker instead of `source.jpg`.

**S2. Tower export + gate.** `scripts/export_int8_weightonly.py` → `data/exp/onnx/clip_b16_224.int8wo.onnx`
(+ `.data` if external). Check ORT-web compatibility: opset ≤ 17, no external data > 2 GB
(fine), `DequantizeLinear` per-channel supported by ORT-web Wasm and WebGPU EPs (verify in the
ORT-web version you pin; if the WebGPU EP lacks per-channel DQ, fall back to `MatMulNBits` 8-bit
or fp16 weights, ~170 MB, owner said ~400 MB is the pain limit). Gate: run
`scripts/gate_cascade.py` with this tower on the server side once (numbers must match the
weight-only fidelity claim: top-1 within 0.5 pp of fp32); record in PLAN-A-LOG. Also export the
localiser as a single-file onnx (`unet-m3` with embedded data) for the browser.

**S3. Serve the models.** New static route `/live/models/<file>` from the bundle dir with
`Cache-Control: public, max-age=31536000, immutable` and a `/live/models/manifest.json`
(`{tower: {file, sha256, bytes, opset}, localizer: {...}, bundle_version, gallery_version}`);
`export_serve_bundle.py` copies the two files into the bundle and `bundle.json` hashes them.
Hash-check on the client after download. SSO: the route sits under `/live*`, already
forward-authed on scan.nilss.dev; nothing to change in Caddy.

**S4. Client inference worker** (`static/live/p2/`): `worker.js` (module worker; imports
`onnxruntime-web` from a pinned CDN URL or vendored into the static dir — vendor it, the page is
behind SSO and CDN drift is a trap), `preprocess.js` (letterbox/resize to the U-Net input, mask
threshold, largest-contour, quad fit — port `maskfit.py`; then `rectify` homography to
`PLANE_W×PLANE_H`, `select_rotations`, 224 crop + CLIP mean/std → Float32 NCHW batch), and
`tower.js` (create session once with `executionProviders: ["webgpu", "wasm"]`, `graphOptimizationLevel: "all"`,
Wasm threads on if `crossOriginIsolated` — needs COOP/COEP headers on `/live*`; add them in the
FastAPI static responses). Cache model bytes in the **Cache Storage API** (not localStorage);
first-run progress bar with MB downloaded; a "device mode" toggle in the settings drawer that
persists in localStorage, default ON when WebGPU is available, OFF otherwise (Wasm at 1–1.5 s
per frame is not a win over the server after the latency lanes).

**S5. Frame loop swap** (`live.js`, minimal diff): `captureAndSend(frame)` → if device mode and
the worker is `ready`, post the frame's `ImageBitmap` to the worker, get `{vectors, rotations,
crop, quad, image_size, timings}` back, POST JSON; else the existing multipart path. Feed the
result to `Consensus` exactly as before. On any worker error (OOM, EP failure, > 3 s per frame)
flip device mode OFF for the session with a toast, keep scanning via the server.

**S6. Tests.** Node tests for `preprocess.js` geometry (quad fit on a synthetic mask, rotation
pair, homography maps corners → plane corners); a Python fidelity test: run the JS preprocessing
in headless Chromium on a fixture frame (`tests/test_serve_live.py` harness), export the batch,
compare to `ClipRetriever._prep` output (max abs diff < 1/255) and the resulting embedding cosine
to the server fp32 path ≥ 0.99; an end-to-end headless test that device mode fires the same card
as the JPEG path on the fixture. Real-phone test by the owner: iPhone Safari (WebGPU in iOS 18+;
Wasm fallback), Android Chrome.

**S7. Ship.** Merge into `live`, deploy (`git -C /tank/pokescan/src checkout <rev>` +
`systemctl restart pokescanner`, bump `commit=` in `pokescan-serve.nix`), status.md + PLAN-A-LOG
+ this file's Results. Measure on the phone: time per frame in device mode vs server mode
(the debug drawer shows both), download size, first-run time.

## Acceptance

- Device-mode identifies the fixture cards identically to server mode (same `print_id`,
  score within 0.02).
- Weight-only int8 tower gate: top-1 within 0.5 pp of fp32, no new wrong-confident.
- Download ≤ 120 MB one-time, cached; page still works with device mode off.
- No new required Caddy/Authentik changes; `/identify` JSON mode behind the same bearer.

## Can it run in parallel with everything else? (asked 2026-09-29)

Yes, with three guards:
1. **`live.js` is contended** by `live-ui` (candidate strip) and `live-fast` (framegate/pipeline)
   until they merge into `live` today. Start P2 with S1–S4 (server JSON mode, export, model
   route, worker) which touch none of their files; do S5 after `git rebase origin/live`.
2. **Host CPU.** The gate re-run (S2) and lane 2's int8 experiments both hammer the 12900H, and
   the cardphotos re-scan (6 workers, other session) may still be running. Use `nice -n 10`,
   `--threads 6`, and run the gate once, not per iteration. Check `uptime` first; if load > 12
   defer the gate.
3. **Other sessions**: pokescan `plan-a` (gallery v13, paused) — P2 is on `live`-derived
   branches, no overlap; denils — only the `commit=` bump at S7, pull first; pokeassets /
   cardphotos — no shared files. The trace dir and bundle dir on CT 140 are append-only.

## Pickup prompt (paste into a new Claude Code session)

```
Implement pokescanner P2 (on-device CLIP embedding, hybrid retrieval) following
/srv/repos/pokecollector/docs/POKESCANNER-P2.md exactly, steps S0–S7 in order. Read first:
that file, docs/POKESCANNER-PLAN.md §2/§4.2/§7, docs/POKESCANNER-LATENCY.md (Results), the
pokescan files it names (retrieve/clip.py, localize/maskfit.py, geometry.py, rotation.py,
serve/app.py, serve/schema.py, serve/static/live/*), docs/PLAN-A-LOG.md last entries, and the
memory notes pokescanner-integration.md + pokescan.md. Work in worktree
/srv/repos/pokescan-wt/p2 on branch p2-device off origin/live; don't touch live.js until
origin/live contains the live-ui and live-fast merges (check git log), then rebase. Vendor
onnxruntime-web, hash-check downloads, Cache Storage for model bytes, fallback to the server
JPEG path on any device error. Heavy CPU only with nice -n 10 and --threads 6, gate once. Tests
per S6 must pass (PYTHONPATH=src /srv/repos/pokescan-wt-r2/.venv/bin/python -m pytest
tests/test_serve.py tests/test_serve_live.py -q + node tests). Deploy per S7 only after tests are
green and the gate numbers are recorded; never run provision-pokescanner; after a nix switch
always ssh root@10.0.1.40 systemctl restart pokescanner and re-check /health. Commit often on
p2-device, push to origin; append Results to POKESCANNER-P2.md and a status.md line on
fork/pokescanner (git pull --ff-only first). Report: what works on which EP, download size, per-
frame ms, gate delta, deployed rev, anything left out.
```
