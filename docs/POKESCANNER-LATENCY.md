# pokescanner — latency plan (2026-09-29)

Owner: "the result is super cool, but latency is there — can we make it better?"

## Measured baseline (CT 140, 4 vCPU, host load ~3, real photo, `debug=1`)

| stage | ms |
|---|---|
| frame_gate + localize (U-Net m3) | 157 |
| rectify | 13 |
| **retrieve** (CLIP ViT-B/16 fp32 `exp/onnx/clip_b16_224.onnx`, 345 MB, batch of the 2 allowed rotations, then cosine search over `gallery_clip_b16_v12.npz`) | **765** |
| ocr + fuse + twin | 18 |
| server total / HTTP total | 953 / ~1 200 |

The phone adds JPEG encode (quality 0.8, full camera resolution, ~200 KB) + upload. The live page
needs 3 agreeing frames out of 5 (`consensus.js`), one request in flight → a card takes ≥ 3 round
trips ≈ 4 s plus phone time. 80 % of server time is the fp32 tower on 4 threads.

Host: i9-12900H (6 P-cores/12 threads + 8 E-cores = 20 threads, AVX2 + AVX-VNNI, **no AVX-512**),
load ~3 when the cardphotos re-scan is idle. CT 140: `cores: 4`, `cpulimit: 0`, no cpuset pin.

Target: a confident card fires in ≤ 1.5 s end-to-end; server `/identify` ≤ 400 ms.

## Lane 1 — more CPU for the container (cheap, zero accuracy risk)

Files: denils `modules/aspects/hosts/pokescanner.nix` (`proxmox.cores`, `proxmox.memory`),
`modules/aspects/features/pokescan-serve.nix` (`threads = 4` → `POKESCANNER_THREADS` +
`OMP_NUM_THREADS`), `/etc/pve/lxc/140.conf` (cpuset via `lxc.cgroup2.cpuset.cpus` if pinning wins).

Steps:
1. Benchmark **on the CT** with the real service, not a script: `curl -F file=@<photo> /identify`
   ×10, take the median of `timings_ms.retrieve` and `total`. Baseline at cores=4/threads=4.
2. `pct set 140 -cores 8` (applies live), restart the service with `POKESCANNER_THREADS=8`
   (temporarily `systemctl set-environment` / drop-in, or just edit the nix and switch), re-bench.
   Also try threads=6 and threads=12 with cores=12 — ORT on ViT-B/16 usually flattens past the
   physical P-core count.
3. P-core vs E-core: read `/sys/devices/system/cpu/cpu*/topology/core_id` on the host to find
   the P-core sibling ranges (typically cpu 0–11 = 6 P-cores × 2 HT, 12–19 = E-cores). Try
   `lxc.cgroup2.cpuset.cpus: 0-11` (or one HT sibling per P-core, `0,2,4,6,8,10`) in
   `/etc/pve/lxc/140.conf` + restart the CT, re-bench. Keep whichever config is fastest; if
   pinning wins by < 10 % don't pin (it makes the CT compete with everything else on those cores).
4. Make it permanent in denils: `proxmox.cores`, `threads`, memory 3072 → 4096 if RSS with more
   threads exceeds 2 GB (check `systemctl status pokescanner` MemoryCurrent). If cpuset is kept,
   add it via the Den LXC `extraConfig`/raw-lxc option if one exists, else document the manual
   line in `POKESCANNER-OPS.md`. `git pull` first, `git push`, `nix run .#update-pokescanner`,
   `systemctl restart pokescanner` (known trap), `check-drift` must say no drift (cores/memory
   are tofu-managed: use `tofu apply -refresh-only` in `~/.local/share/den-lxc/pokescanner`,
   NEVER `provision-pokescanner`).
5. Record the table (config → retrieve/total ms) at the bottom of this file and in `status.md`.

Accept: retrieve median < 500 ms at the chosen config, `/identify` correct on the same photo, Gatus
still green, `check-drift` clean.

## Lane 2 — faster tower on the server (int8 with an accuracy gate)

Repo: pokescan, new worktree + branch `int8-serve` off `origin/live`
(`git -C /srv/repos/pokescan-wt/live worktree add /srv/repos/pokescan-wt/int8 -b int8-serve origin/live`,
symlink `data` → `/srv/repos/pokescan-wt-r2/data`, venv `/srv/repos/pokescan-wt-r2/.venv`).
Read first: `docs/NEXT-2026-09-22.md` §int8 (weight-only = quality-safe but **no CPU speedup**,
it dequantises to fp32; `quantize_dynamic` = fast but **−6.1 pp** on B/16 → rejected),
`scripts/export_int8_weightonly.py`, `scripts/exp_int8_fidelity.py`, `scripts/gate_cascade.py`
(the accuracy gate, `--threads`, `--holdout`), `src/pokescan/retrieve/clip.py` (`ClipRetriever`
session options, `_prep`), `src/pokescan/bundle.py` (`clip_tower` entry, sha256 manifest).

Candidates to build and measure (all with onnxruntime CPU EP, threads = lane-1 result or 4):
- **A. Static QDQ int8** with calibration: `onnxruntime.quantization.quantize_static`,
  `QuantFormat.QDQ`, `per_channel=True`, `reduce_range=False`, activation `QUInt8`, weight
  `QInt8`, calibration = 200–500 rectified card planes drawn from the eval bank the gate uses
  (real photos, both rotations, exactly `ClipRetriever._prep` output). Try `CalibrationMethod.MinMax`
  and `Percentile`. Keep LayerNorm/Softmax/GELU in fp32 if the quantizer's `op_types_to_quantize`
  needs it (`MatMul`, `Gemm` only is the safe first try).
- **B. MatMulNBits** (`onnxruntime.quantization.matmul_nbits_quantizer`, bits=8, block 128,
  `accuracy_level=4`) — weight-only but with an int8 compute kernel on x64; measure, it may or
  may not be faster than fp32 for 197-token GEMMs.
- **C. fp32 + graph optimisations only** as the control: `ORT_ENABLE_ALL`, saved optimised
  model, `execution_mode` sequential, thread spinning (`session.intra_op.allow_spinning`) — cheap
  wins that stack with A/B.

For each candidate: (1) latency with `scripts/bench_latency.py`-style timing on the same 20 real
frames (report p50/p90 of the tower forward alone and of `identify` total); (2) fidelity:
embedding cosine to fp32 p50/p1 on 200 planes (`exp_int8_fidelity.py` style); (3) the real
gate: `scripts/gate_cascade.py` with the candidate tower swapped in via a bundle whose
`clip_tower` points at it (build with `scripts/export_serve_bundle.py --out /tank/pokescan/bundle-v12-int8` or
a local dir), same `--langs`/holdout as the last shipped gate run (look up the most recent
`data/exp/results/gate_cascade*.json` for the reference numbers) — compare top-1, AMBIGUOUS rate,
wrong-confident count.

Accept a candidate only if: tower forward ≥ 1.5× faster than fp32 at the same threads **and**
gate top-1 within 0.5 pp of fp32 **and** wrong-confident count not higher. If nothing passes,
ship C alone if it helps ≥ 10 %, and write the numbers down — a negative result is still the
deliverable. If one passes: add the tower to the bundle (new dir `bundle-v12-int8`, `bundle.json`
sha entries, `Bundle.load` must verify), point `bundleDir` in `pokescan-serve.nix` at it,
`update-pokescanner`, restart, verify `/health` + the same real photos as before, record numbers.
Commit on `int8-serve`, push, don't merge into `live` until the owner has seen the numbers.

## Lane 3 — fewer round trips from the page

Repo: pokescan, branch `live-fast` off `origin/live`, worktree `/srv/repos/pokescan-wt/live-fast`.
Files: `src/pokescan/serve/static/live/consensus.js` (pure, node-tested in `tests/live/`),
`live.js` (capture loop, in-flight limit), `tests/test_serve_live.py`. Another lane (`live-ui`,
candidate strip) is editing `live.js`/`live.css`/`index.html` right now: keep `live.js` edits
minimal and confined to the capture loop, rebase onto `origin/live` before merging.

Changes:
1. **Early fire.** In `Consensus`, fire after **2 consecutive** agreeing fireable results when
   both have `score ≥ 0.85` **and** margin to the second candidate ≥ 0.08 (use the fields
   `/identify` returns — check `schema.py`: top-k scores; if margin isn't in the response add it
   server-side as `margin`, tiny change in `app.py`, tested). Otherwise keep 3-of-5. Never fire on
   a single frame. Unit-test both paths (`tests/live/consensus.test.mjs`).
2. **Client-side frame gate.** Before uploading, downscale the frame to 160 px and compute
   Laplacian variance (sharpness) + mean absolute difference to the previous sent frame (motion).
   Skip the upload when blurry (threshold tuned on the fixtures: blurry < ~40) or when the frame is
   near-identical to the last one that returned NO_CARD (nothing changed, don't re-ask). Cheap,
   pure JS in a new `framegate.js` with node tests on synthetic arrays.
3. **Pipelining.** Allow `maxInFlight = health.threads >= 8 ? 2 : 1` (read from `/health`, already
   fetched on load) and send the next frame as soon as a slot is free instead of after the
   response; results are fed to `Consensus` in send order (sequence number) so the 3-of-5 window
   stays ordered.
4. **Encode cost.** Measure `toBlob` time on the page (`performance.now()` around it, shown in
   the debug drawer as `enc ms`) and try quality 0.7 vs 0.8 on the same frame through `/identify`
   to check the decision doesn't change; keep resolution as is (1600 px downscale lost 2/137
   frames, don't revisit).

Accept: node tests + `tests/test_serve_live.py` green; headless run fires on frame 2 for a strong
card and still on frame 3 for a weak one; no upload for a static blurry frame. Merge into `live`
after rebasing on the `live-ui` result (or before it lands if it hasn't), push, deploy with
`git -C /tank/pokescan/src checkout <rev>` + `systemctl restart pokescanner`, bump `commit=` in
the nix file, update PLAN-A-LOG.

## Not now

- On-device int8 tower (P2, plan §7) — removes the upload entirely, multi-day.
- MobileCLIP-S0 tower — smaller/faster but −pp on the gate (`TOWER.md`), owner decided no smaller tower.

## Results

(lanes append here)
