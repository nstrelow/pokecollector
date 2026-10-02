# pokescanner — latency plan (2026-09-29)

Status: DONE (historical); current state in `docs/status.md`.

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
(`git -C /srv/repos/PicaLens/scanner/worktrees/live worktree add /srv/repos/PicaLens/scanner/worktrees/int8 -b int8-serve origin/live`,
symlink `data` → `/srv/repos/PicaLens/scanner/worktrees/r2/data`, venv `/srv/repos/PicaLens/scanner/worktrees/r2/.venv`).
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

Repo: pokescan, branch `live-fast` off `origin/live`, worktree `/srv/repos/PicaLens/scanner/worktrees/live-fast`.
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

### Lane 1 (2026-09-29) — CPU for CT 140

Method: real service on CT 140, `/identify?debug=1`, 4 photos (CLC-004 = POSSIBLY_UNSUPPORTED_SET en:swsh10-021;
three real phone frames: `..230416843` = ja:M1S-033, `..113758013` = en:me05-048, `..114712589` = ja:M1S-001),
10 calls each after 1 warm-up, median of `timings_ms.retrieve` / `total`. **The host was heavily loaded the
whole time (load avg 10 -> 48; the niced cardphotos re-scan workers + the int8 lane's build saturate the
20 threads), so absolute numbers are 1.5-2x worse than the idle baseline and noisy (+/-20 %);
only the ordering is meaningful.** Decisions come from A/B alternation, not single runs.

Retrieve / total ms per photo, CLC-004 | M1S-033 | me05-048 | M1S-001 (cores/threads, host load1 at start):

| config | retrieve | total | load |
|---|---|---|---|
| 4/4 (baseline, 1st) | 775 / 942 / 747 / 756 | 981 / 1304 / 897 / 887 | 12 |
| 4/4 (2nd) | 1005 / 1116 / 1085 / 982 | 1185 / 1811 / 1365 / 1353 | 43 |
| 4/4 (3rd) | 920 / 1074 / 1002 / - | 1087 / 1599 / 1277 / - | 26 |
| 8/8 | 793 / 984 / 816 / 789 | 960 / 1421 / 981 / 938 | 18 |
| 8/6 (run 1) | 556 / 784 / 622 / 598 | 677 / 1187 / 811 / 744 | 22 |
| 8/6 (run 2) | 873 / 778 / 751 / 726 | 1138 / 1416 / 968 / 963 | 41 |
| 8/6 (run 3) | 683 / 781 / 593 / 594 | 840 / 1295 / 754 / 721 | 30 |
| 8/6 (A/B) | 973 / 890 / 981 / 814 | 1243 / 1366 / 1318 / 1068 | 36 |
| 8/6 (A/B) | 793 / 891 / 866 / 678 | 919 / 1447 / 1211 / 951 | 30 |
| 6/6 | 938 / 1142 / 1010 / 1044 | 1127 / 1885 / 1315 / 1296 | 38 |
| 12/12 | 776 / 1029 / 954 / 913 | 943 / 1765 / 1234 / 1219 | 27 |
| 12/8 | 887 / 918 / 866 / 948 | 1038 / 1858 / 1177 / 1227 | 35 |
| 8/4 (run 1) | 639 / 627 / 467 / 480 | 753 / 968 / 631 / 653 | 43 |
| 8/4 (A/B) | 764 / 621 / 650 / 816 | 937 / 978 / 923 / 1064 | 33 |
| 8/4 (A/B) | 823 / 662 / 554 / 656 | 1028 / 994 / 728 / 917 | 35 |
| 8/5 | 729 / 731 / 662 / 621 | 993 / 1335 / 855 / 821 | 29 |
| 8 (cpuset 0-11) /6 | 886 / 1156 / 1005 / 1373 | 1146 / 1963 / 1380 / 1760 | 34 |
| 8 (cpuset 0,2,..,10 = 1 thread per P-core) /6 | 1282 / 1479 / 1107 / 1384 | 1725 / 2401 / 1493 / 1845 | 42 |
| **8/4 deployed (after switch)** | 385 / 566 / 761 / 729 | 467 / 805 / 1028 / 908 | 17 |

Findings:
- Under equal load, cores 4 -> 8 with threads=4 is ~25-35 % faster on retrieve (about 1000 -> 650-750 ms).
  More threads do not help and often hurt (ORT sync stalls when the host is oversubscribed): 8/8, 12/12 and 12/8
  are no better than 4/4; 8/6 is between. threads=4 stays; the extra cores just give the 4 threads and the
  HTTP/localize work room to run on free cpus.
- PVE applies `cores` as a cpuset (`cpuset.cpus.effective` was `0-3` at cores=4, `0-7` at cores=8). cpus 0-3
  are only two physical P-cores (HT siblings), which is why the baseline is so starved.
- Pinning (P-cores 0-11, or one HT sibling per P-core) was not better (worse, in fact, under this load);
  no cpuset override is kept.
- RSS ~1.0 GB (peak 1.1 GB) at threads=4, so memory stays 3072.
- The accept criterion (retrieve median < 500 ms) is NOT reached under load; the only sub-500 readings were
  at the lowest load (385 ms on CLC-004, 467 ms on me05-048 in one run). Re-measure on an idle host. The
  remaining lever is the tower itself (lane 2).

Permanent config: denils `hosts/pokescanner.nix` `proxmox.cores = 8` (commit 8dac08f), `threads = 4` and
memory 3072 unchanged; deployed with `update-pokescanner` + restart, tofu state reconciled via
`apply -refresh-only`, `check-drift` reports no drift. `/identify` decisions identical across configs.

### Lane 3 (page round trips), 2026-09-29, pokescan `live` @ 948cfda, deployed

- **What changed**: early fire (2 consecutive frames with score >= 0.85 and margin >= 0.08, else
  3-of-5; `margin` was already in `/identify`), client frame gate (`framegate.js`: skip if 160 px
  Laplacian variance < 40, or if < 3 mean-abs-luma from the last NO_CARD upload, re-ask after 5 s),
  pipelining (`pipeline.js`: 2 in flight when `/health.threads >= 8`, results delivered in send
  order), `enc ms` in the perf line + debug drawer. `live.js` touched only in the capture loop.
- **Headless (fake camera, scripted /identify)**: strong card fires on frame 2 (was 3), weak card
  (margin 0.03) on frame 3, static blurry frame = 0 uploads, static empty scene = 1 upload,
  threads=12 fires on frame 2 with 2 in flight. Saves one full round trip (~1.2 s at today's
  latency) for confident cards.
- **Thresholds**: sharp testset photos have Laplacian variance >= 660, the same photos blurred
  (sigma 1.5 % of long side) <= 37 -> `BLUR_MIN` 40. Strong rule on the 17 labelled testset frames:
  5 strong, 0 strong-but-wrong (small sample; watch for a wrong early fire in real use).
- **Encode**: `toBlob` median 21 ms for a noisy 1280x720 frame in software headless Chromium (a
  phone with a HW JPEG encoder should be well under that). q0.7 vs q0.8 through the real service
  on 87 photos: 21 % smaller but the decision changed on 14/87 (all borderline AMBIGUOUS /
  CONFIRM_LANGUAGE frames) -> kept 0.8.
- **Caveat for pipelining**: `Service.identify` holds a lock around the recognizer, so a 2nd
  request only overlaps upload/decode/encode, not compute; the real win needs lane 1/2 or removing
  the lock (ORT sessions can run concurrently at the cost of splitting the threads).
- Not verified on a real phone. Deploy note: `/health.commit` comes from `commit =` in
  `pokescan-serve.nix` (bumped to 948cfda), not from the git checkout.

### Lane 2 (2026-09-29) — int8 tower on the server: **nothing passes, nothing deployed**

pokescan branch `int8-serve` @ 16a4984 (pushed, not merged), `scripts/export_int8_static.py`,
`data/exp/results/int8_static.json` + `gate_cascade_int8_<cand>.json`, PLAN-A-LOG "Server int8 tower
(latency lane 2)". ORT 1.22 CPU EP, 6 threads, `nice 10`, host load 8-9 during the latency run.
Calibration: 530 planes from 250 real wild photos (cardphotos corpus) through the bundle's S1 + rectify,
bit-exact to `ClipRetriever._prep`; fidelity: 200 planes from 110 other wild photos; neither overlaps
the gate. Latency: gate's first 20 frames, the real 2-rotation batch, all towers interleaved, 5 rounds.
Gate: full `gate_cascade.py` with a new experiment-only `--clip-tower` flag; fp32 re-run as reference
(top-1 99.24 %, ambiguous 9.85 %, wrong-confident 0 — identical to the shipped v12 gate).

| cand | what | cos p50 / p1 | tower p50 / p90 ms | speedup | identify p50 ms | gate top-1 (Δpp) | ambig % | wrong-conf | verdict |
|---|---|---|---|---|---|---|---|---|---|
| fp32 | shipped | 1 / 1 | 278 / 327 | 1.00 | 586 | 99.24 | 9.85 | 0 | reference |
| C | fp32 saved ORT_ENABLE_ALL + sequential + spinning | 1.0 / 1.0 | 286 / 369 | 0.97 | 553 | 99.24 (0.00) | 9.85 | 0 | **FAIL** (no speedup; < 10 % so not shipped either) |
| B | MatMulNBits 8-bit, block 128, accuracy_level 4 | 0.9993 / 0.9986 | 207 / 318 | 1.34 | 529 | 98.48 (-0.76) | 10.61 | 0 | **FAIL** (speed + top-1) |
| A MinMax | static QDQ per-channel, MatMul+Gemm | 0.578 / 0.360 | 134 / 190 | 2.08 | 416 | 16.67 (-82.6) | 100 | 0 | **FAIL** |
| A Percentile | same, 99.999 pct | 0.737 / 0.542 | 137 / 192 | 2.03 | 448 | 53.03 (-46.2) | 93.2 | 0 | **FAIL** |
| A_minmax_nob6 | attn act-matmuls + block-6 MLP c_proj kept fp32 | 0.969 / 0.947 | 136 / 205 | 2.04 | 446 | 96.97 (-2.27) | 12.12 | 0 | **FAIL** |
| A_sq50_nob6 | + SmoothQuant α 0.5 | 0.973 / 0.951 | 133 / 197 | 2.09 | 427 | 97.73 (-1.51) | 10.61 | **1** | **FAIL** |
| A_sq50_core | + out_proj and final proj fp32 | 0.976 / 0.957 | 153 / 223 | 1.82 | 464 | 96.97 (-2.27) | 12.88 | 0 | **FAIL** |
| A_sq50_nofc2 | SmoothQuant, all MLP c_proj fp32 | 0.985 / 0.972 | 179 / 240 | 1.55 | 481 | 97.73 (-1.51) | 11.36 | 0 | **FAIL** |
| A_pct_nofc2 | Percentile, all MLP c_proj fp32 | 0.985 / 0.971 | 182 / 235 | 1.53 | 503 | 98.48 (-0.76) | 11.36 | 0 | **FAIL** (closest: 1 frame) |

Why: plain static int8 is destroyed by a single tensor — block 6's MLP down-projection input (one
massive-activation channel) alone takes cosine 0.98 -> 0.58. With it in fp32, every remaining group still
costs a little (per-tensor uint8 activations; MLAS has no per-channel activation path) and it sums to
~0.97-0.985 cosine, which moves 6-11 of the 132 gate frames. The gate is far stricter than cosine: even
B's 0.9993 flips the known 5.9e-5 near-tie frame (`item6`) and demotes one IDENTIFIED to AMBIGUOUS.
Fully static int8 would be ~2.1x on the tower (identify ~586 -> ~420 ms); what quality needs in fp32
brings it down to 1.5x. C proves ORT already applies all graph optimisations and spinning by default.

**Recommendation:** do not ship an int8 tower and do not change the ORT session; there is no
`bundle-v12-int8`, nothing was deployed. Latency comes from lane 1 + lane 3. The one untested path that
could rescue A_pct_nofc2 (1.53x, one frame short) is re-embedding the 60k gallery through the int8 tower
so query and gallery share the quantisation error (invariant 4) — ~1-2 h CPU plus a new table/bundle and a
re-gate; only worth doing if lane 1 + 3 leave the target unmet.
