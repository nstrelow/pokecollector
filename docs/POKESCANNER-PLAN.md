# pokescanner integration plan

Status: **approved plan, nothing implemented yet** (2026-09-28).
Branch: `pokescanner` on `nstrelow/pokecollector`, based on upstream `main` d18db13.
Supersedes PR #4 (`local-image-matching`, pHash) — that branch is abandoned, not rebased.

The scanner engine lives in a separate repo, `nstrelow/pokescan` (`/srv/repos/pokescan`),
pinned for this PoC at **`origin/plan-a` 8f0ff3b with the v12 bundle** (`data/bundle.json`
hash-checks every asset; `Bundle.load` refuses anything else).

## 1. Goal

A demo-able proof of concept: "this is my scanner and this is what it detected" —
photograph or live-scan one card, see the detected boundary, the rectified card, the
top-k candidates with scores, every flag the model raised, and the timings; then add the
card to the collection with one tap. Primary use: bulk-adding the owner's own cards
(PRE and newer, mostly full arts), one card at a time, live mode auto-firing.

## 2. Decisions taken (owner, 2026-09-28)

| # | Question | Decision |
|---|---|---|
| 1 | PR #4 (pHash) | Abandon; clean branch off upstream `main`. pokescan makes pHash obsolete. |
| 2 | Where the scanner runs | New NixOS LXC via denils (`pokescanner`), bundle bind-mounted from `/tank`. Not in CT 100, not on the host. |
| 3 | Live client mode | P1 = browser camera → frames to server (tonight). P2 = on-device int8 CLIP. P3 = fully offline. See §7. |
| 4 | Debug output | Live overlay **and** stored per scan, kept forever (extends upstream `scan_trace`). |
| 5 | pokescan pin | `origin/plan-a` 8f0ff3b + v12 bundle. Upgrades = config change (bundle hash). |
| 6 | Languages | pokecollector keeps `en,de`. Scanner UI offers an en/de session toggle. ja/zh-tw hits are still returned, mapped to ids pokecollector may not have (see §5.3). |
| 7 | Capture model | One card per frame; live mode auto-fires on temporal consensus. No multi-card spread. |
| 8 | Exposure | `scan.nilss.dev` behind Authentik (HTTPS is required for `getUserMedia` anyway). Backend ↔ scanner over LAN. |
| 9 | Non-IDENTIFIED outcomes | Never refuse: show closest match + "set may not be supported" hint. Supported-sets list shown in scanner settings. |
| 10 | Upstream | Generic **external matcher** contract (provider-style, like the OpenAI path), fork-only for now, upstreamable later. |
| 11 | Users | Single-user instance → global admin/env config `EXTERNAL_MATCHER_URL`, no per-user toggle. |
| 12 | Client download size | 400 MB is acceptable *if it works*, but fp32 CLIP ≈ 1 GB resident kills mobile tabs → P2 uses the **weight-only int8 export (~85 MB)**, no retraining. |

## 3. Architecture

```
phone / browser                          pokecollector (CT 100, docker)             pokescanner (new NixOS CT)
┌──────────────────────┐   photos   ┌──────────────────────────────┐   HTTP    ┌──────────────────────────────┐
│ ScanQueue review page│ ─────────▶ │ scan_queue worker            │ ────────▶ │ FastAPI  POST /identify       │
│  + debug panel       │ ◀───────── │  provider = "external"       │ ◀──────── │   pokescan.identify(frame)    │
└──────────────────────┘            │  scan_trace (+ matcher blob) │           │   + locate_s1 + rectify_card  │
                                    └──────────────────────────────┘           │ GET /health /bundle /ref/{id} │
┌──────────────────────┐  frames (P1) / vectors (P2)                           │ static: /live  (P1/P2 client) │
│ scan.nilss.dev /live │ ────────────────────────────────────────────────────▶ │                               │
│ camera + overlay     │ ◀──────────────────────────────────────────────────── │ POST /collection/add (proxy)  │
└──────────────────────┘                                                       └──────────────────────────────┘
```

Two entry points, one engine:

* **Queue path** (existing pokecollector UX): photos are staged in `UnifiedCardScanner`,
  queued via `POST /api/cards/recognize/jobs`, processed by the worker, reviewed in
  `ScanQueue.jsx`, added via `resolve-and-add`. Only the *provider* changes.
* **Live path** (new, served by pokescanner itself): a standalone page with a camera
  feed. It calls pokescanner directly, and adds cards to pokecollector through a thin
  proxy on pokescanner that holds a pokecollector API token (so the page needs only
  Authentik).

## 4. pokescanner service (repo `pokescan`, new package `pokescan.serve`)

FastAPI + uvicorn, one worker process, `threads` pinned (pokescan is GIL/BLAS-bound; two
uvicorn workers would double RAM for no throughput). CPU only.

### 4.1 Endpoints

| Route | Purpose |
|---|---|
| `GET /health` | `{ok, bundle_version, gallery_version, commit, uptime}` |
| `GET /bundle` | `supported_sets_vN.json` passthrough + `uncatalogued_scripts` — consumed by the settings page |
| `POST /identify` | multipart `file` (JPEG/PNG/WebP/HEIC) **or** JSON `{vector: float[512], crop: base64}` (P2). Query `session_lang=en\|de`, `debug=1`. Returns §4.2. |
| `GET /ref/{print_id}` | reference image webp from `data/refs_vN/` (for candidate thumbnails; pokecollector may lack the image for ja/zh-tw) |
| `GET /trace/{trace_id}` , `GET /trace/{trace_id}/{plane\|overlay}.webp` | stored debug artefacts (§6) |
| `POST /collection/add` | live-page helper: `{print_id, quantity, variant, condition}` → forwards to pokecollector `POST /api/collection/` with a server-side token |
| `GET /live` | static P1/P2 client (§7) |

### 4.2 `/identify` response — the external matcher contract

This is the interface pokecollector depends on. Anything that returns it is a valid
matcher; pokescan is one implementation.

```jsonc
{
  "matcher": {"name": "pokescan", "commit": "8f0ff3b", "bundle": "v12"},
  "state": "IDENTIFIED",            // IDENTIFIED | CONFIRM_LANGUAGE | AMBIGUOUS |
                                    // POSSIBLY_UNSUPPORTED_SET | NOT_IN_CATALOG |
                                    // NO_CARD | CARD_BACK | TOO_BLURRY
  "confident": true,                // state in ACCEPTING_STATES and no guard flag
  "hint": null,                     // e.g. "set may not be supported yet"
  "print_id": "en:sv03.5-043",      // pokescan id: "<tcgdex-lang>:<tcgdex-card-id>"
  "identity_key": "sv|sv03.5|043",  // stable across gallery builds (identity_id is NOT)
  "candidates": [                   // top-k, ordered
    {"print_id": "en:sv03.5-043", "tcg_card_id": "sv03.5-043", "lang": "en",
     "set_id": "sv03.5", "number": "043", "name": "Oddish", "rarity": "Common",
     "score": 0.91, "margin": 0.12, "image": "/ref/en:sv03.5-043"}
  ],
  "language": {"options": ["en:sv03.5-043", "de:sv03.5-043"],
               "source": "detected", "scores": {"en": 0.8, "de": 0.2},
               "unsupported": null},
  "number": {"reading": "043/165", "verdict": "agree"},
  "twin": {"ambiguous": false, "verdict": null, "scores": null},
  "flags": {"low_confidence": false, "picker_only": false, "via_fallback": false,
            "via_refine": false, "slab_trim": false, "classic_reprint": false,
            "unreferenced_guard": false, "card_back_score": 0.03,
            "script": "latin", "orientation": 0},
  "geometry": {"quad": [[x,y],[x,y],[x,y],[x,y]],   // in *uploaded* image pixels
               "source": "primary", "image_size": [w, h]},
  "timings_ms": {"frame_gate": 3, "localize": 90, "rectify": 4, "thumb_head": 20,
                 "retrieve": 310, "ocr": 60, "fuse": 2, "twin": 15, "total": 504},
  "trace_id": "2026-09-28T21-04-11Z_ab12cd"   // only when debug=1; plane/overlay stored
}
```

Work in pokescan to produce this (none of it exists today):

* `pokescan/serve/schema.py` — `result_to_dict(Result, quad, image_size, timings)`.
  `Result` is a plain dataclass with enums; there is no `to_dict`.
* Capture the quad without a second pass: `CardRecognizer.identify` computes it internally;
  add an optional `return_geometry=True` (or a `debug` sink) rather than the cardphotos
  monkeypatch of `rec._locate`.
* Names/numbers/rarity come from `catalog_v12.sqlite` (284 MB, read-only, opened once).
  Only the `prints` table is needed → export a **`names_v12.json`/sqlite subset** (~5 MB)
  at bundle build time so the service doesn't ship the full catalog.
* EXIF transpose on upload (cardphotos does this with PIL; phones send rotated JPEGs).
* Reject frames > 4096 px on the long side; downscale to 1600 px before `identify`
  (pokescan's localiser works on a downscaled copy anyway — verify the S1 input size).

### 4.3 Packaging / deployment (denils)

* New host `pokescanner` in `/root/denils`: NixOS LXC, 4 vCPU, 3 GB RAM (measured: bundle
  ~425 MB on disk, ~1.2 GB resident after warm-up; re-measure on first boot), bind mount
  `/tank/pokescan/bundle-v12` read-only. Python via `python3.withPackages` (numpy,
  opencv-python-headless, onnxruntime, fastapi, uvicorn, httpx, pillow). No torch.
* The bundle directory is produced by `scripts/build_bundle.py` at 8f0ff3b and rsynced to
  `/tank/pokescan/bundle-v12/` (append-only: v13 gets its own dir).
* systemd unit, `OMP_NUM_THREADS`/`threads=4`, warm-up call at start (first `identify` is
  ~3 s because of ORT session creation).
* Gatus + homepage entries are emitted by the denils feature aspect (see memory:
  denils-quirk-emission).
* Reverse proxy: `scan.nilss.dev` → pokescanner `:8000`, Authentik forward-auth on `/live`
  and `/collection/*`; `/identify` from pokecollector's backend comes over LAN without auth
  (allowlist CT 100's IP), or with a shared bearer `EXTERNAL_MATCHER_TOKEN` — use the token,
  it's one line and survives IP changes.

## 5. pokecollector changes (this repo)

Keep the diff small and provider-shaped so it can be upstreamed later.

### 5.1 Config

`.env.example` / `docker-compose.yml`:

```
EXTERNAL_MATCHER_URL=http://10.0.1.xx:8000      # pokescanner base URL
EXTERNAL_MATCHER_TOKEN=                          # optional bearer
EXTERNAL_MATCHER_LABEL=pokescan
EXTERNAL_MATCHER_TIMEOUT=20
```

When `EXTERNAL_MATCHER_URL` is set, provider `external` becomes the installation default
(`installation_model`/`enabled_providers` in `services/scan_providers.py`); the Gemini /
OpenAI paths remain selectable. No capability probe (it isn't an LLM); `/health` is the
probe, surfaced in `ScannerSettingsCard`.

### 5.2 Backend

* `services/external_matcher.py` — httpx client: `identify(image_bytes, session_lang) ->
  MatcherResult`, `bundle()`, `health()`. Retries once on connect error; a 503 from the
  matcher ⇒ `fail_claim` with a retryable error so the queue backs off instead of
  failing the item.
* `services/scan_providers.py` — `ScanProvider(name="external")`:
  `requires_credential() -> False`, `rate_limit_scope` no-op, `is_llm() -> False`.
* `services/scan_queue.py` `default_scan_processor` — if `provider.name == "external"`:
  call the matcher, **skip `match_card_info`** (no text fields to match), and write
  `result = {"recognized": {...nulls, "language": lang}, "matches": [...], "_source":
  "external", "_identity_decision": state, "_identity_confident": confident,
  "_matcher": {...full §4.2 payload minus candidates...}}`.
  Composite (multi-photo) jobs are **not** supported by the external provider — the
  worker falls back to per-item processing (`default_composite_processor` returns
  unresolved when `provider.name == "external"`).
* Candidate mapping (`services/external_matcher.py::to_matches`):
  `print_id "en:sv03.5-043"` → `{"id": "sv03.5-043_en", "tcg_card_id": "sv03.5-043",
  "lang": "en", "set_id": "sv03.5", "number": "043", "name", "rarity", "image":
  <pokecollector card image if the row exists, else matcher /ref URL>, "_score",
  "_margin", "_match_percent": round(score*100)}`. `zh-tw`/`zh-cn` map unchanged
  (pokescan already uses TCGdex api-lang codes). `resolve-and-add` validates
  `confirmed_card_id` against `matches[].tcg_card_id`, so this shape works unchanged.
* `ensure_card_exists` already falls back to live TCGdex for ids not in the `cards` table
  (ja hits with an `en,de` sync) — leave as is; it is slow but correct.
* `services/scan_trace.py` — add `matcher` to the trace JSON (the full §4.2 payload) and
  store the plane/overlay webps next to the source image. Traces are currently opt-in per
  user (`scan_diagnostics_enabled`) and never auto-deleted → matches decision #4; the owner
  turns the setting on once.
* New read endpoint `GET /api/cards/recognize/jobs/{job}/items/{item}/matcher` returning
  the stored `_matcher` blob + trace artefact URLs (needed by the debug panel; the item
  `matches` payload is already served but the blob is not).
* `GET /api/settings/scanner/external` → proxied `/health` + `/bundle` (supported sets).

### 5.3 Frontend

* `components/ScanReview.jsx` / `pages/ScanQueue.jsx` — **debug panel** (collapsible,
  default open when `_source == "external"`):
  1. source photo with the quad drawn (SVG overlay, quad is in upload pixels — scale by
     the rendered size);
  2. rectified plane (from the trace);
  3. state badge + hint + flags as chips (`low_confidence`, `twin_ambiguous`,
     `via_fallback`, …), script, language source/scores;
  4. number reading + verdict;
  5. candidate list: thumbnail (`image`), name, set, number, lang, score bar, margin;
     the existing `CandidatePrintingPicker` handles the choice;
  6. timings bar (stacked ms per stage).
* `components/ScannerSettingsCard.jsx` — external matcher block: health, commit/bundle,
  supported-sets table per language (from `/bundle`), the note that ja/zh-tw hits are
  returned but not synced.
* `UnifiedCardScanner.jsx` — session language toggle (en/de) sent as a form field on
  `POST /recognize/jobs` (`session_lang`), stored on the job and forwarded to the matcher.
* i18n: en + de strings only (fork), other 18 languages fall back to the key.
* Link "Live scanner ↗" → `https://scan.nilss.dev/live`.

### 5.4 Tests

* `backend/tests/test_external_matcher.py`: mapping of every `state`, id mapping incl.
  `zh-tw`, 503 ⇒ retryable, timeout ⇒ failed item with message, composite ⇒ unresolved.
* Fixture: one recorded §4.2 payload per state (record them from the real service once).
* Frontend: debug panel renders a fixture without the trace artefacts (404 tolerant).

## 6. Debug trace format (stored, kept forever)

`data/scan-traces/<user>/<trace_id>/`:

```
source.jpg        sanitized upload (existing)
trace.json        existing fields + "matcher": §4.2 payload + "session_lang"
plane.webp        980x700 rectified card (from pokescanner /trace)
overlay.webp      source with quad + top-1 label burned in (for sharing screenshots)
```

The same layout is written by pokescanner itself for live-page scans
(`/var/lib/pokescanner/traces/`), so both paths produce identical artefacts and the
review UI can show either. These traces double as future regression/training data.

## 7. Live client (`/live`) — phases

Single static page (vanilla JS or Preact, no build step required for P1), served by
pokescanner, PWA manifest + service worker from P2 on.

### P1 — server inference, tonight

* `getUserMedia({video: {facingMode: "environment", width: 1280}})`, canvas capture.
* Loop: grab frame → JPEG q80 (~150 KB) → `POST /identify?debug=0&session_lang=` →
  draw quad + state + top-3 with scores over the video. One request in flight at a time
  (~1–1.5 fps on the CT).
* **Temporal consensus** (pokescan has none; `low_confidence` is documented as "don't fire
  yet"): keep the last 5 results; fire when ≥ 3 consecutive results have the same
  `identity_key`, state ∈ {IDENTIFIED, CONFIRM_LANGUAGE} and no `low_confidence`; then
  lock for 2 s and show the card sheet (name, set, number, thumbnail, quantity/variant,
  **Add** button → `POST /collection/add`). Unlock when 3 consecutive frames disagree or
  return NO_CARD (card removed). A "snap" button bypasses consensus.
* Non-confident states show the closest match with the hint and a picker of the top-3
  ("is it one of these?") — never a refusal (decision #9).
* Debug drawer (swipe up): plane, flags, number reading, timings — the same fields as §5.3.
* Language toggle en/de in the header (persisted in localStorage).

### P1.1 — Candidate strip (owner request 2026-09-29)

Replaces the stacked top-3 list. Goal: show *more* candidates while taking *less* of the
viewfinder, and make each candidate scannable at a glance.

* **Layout**: one horizontal, swipeable strip pinned to the bottom edge of the viewfinder
  (scroll-snap, momentum). Fixed height ≤ ~25 % of the portrait viewport (safe-area aware),
  so the video never shifts when candidates change; a skeleton row shows while a request
  is in flight.
* **Card tile** (thumb 56–72 px wide, 2:3): `/ref/{print_id}` thumbnail; below it the
  **card number** `043/198` (monospace-ish, primary), the **set id** (`sv03.5`, small caps,
  secondary), the **language** as a flag emoji + code (`🇩🇪 de`; text fallback when the
  platform has no flag glyphs), and the **confidence** as a coloured pill
  (`87 %`: ≥ 80 green, 50–80 amber, < 50 grey).
* **Hierarchy**: the top candidate is first, slightly larger and outlined, with a "best"
  marker; the rest are uniform. Consensus/lock state is shown on the strip, not in a
  separate banner.
* **Interaction**: tap = select (opens the add sheet with that print); long-press or the
  ⓘ button = details drawer (plane, flags, number reading, timings). 44 px tap targets,
  `aria-label` per tile, keyboard focusable.
* **Same treatment later** for pokecollector's `MatcherDebugPanel` candidate list (§5.3),
  so both surfaces read the same way.

### P2 — on-device embedding, hybrid retrieval (1–2 days, no retraining)

* Export: `scripts/export_int8_weightonly.py` → `clip_b16_224.int8.onnx` (~85 MB); plus
  `unet-m3.onnx` (650 KB). Re-run the 132-photo gate + wild 400 with the int8 tower on
  the server to confirm auto-correct/silent-wrong don't move; if they do, fp16
  (~170 MB) is the fallback.
* Client: onnxruntime-web, WebGPU EP with Wasm-SIMD fallback, in a Worker with
  OffscreenCanvas. Port S1 (U-Net mask → largest contour → quad fit), rectify (homography
  warp on canvas), the 2-rotation `geo2_axis` rule, CLIP preprocessing (224 crop, mean/std).
  The classical localiser fallback is **not** ported in P2 (server keeps it).
* `POST /identify` JSON mode: `{vectors: [[512],[512]], crop: <webp of the plane, ~50 KB>,
  quad, image_size}` → server does retrieve/twin/S7 on the crop. Nothing else leaves the
  phone. Same response shape → the P1 UI is unchanged; only the frame loop swaps.
* Model files cached by the service worker; a first-run progress bar; expect ~0.3–0.5 s
  per frame on WebGPU phones, ~1–1.5 s on Wasm.

### P3 — fully offline (2–3 days more, no retraining)

* Ship `gallery_clip_b16_v12.npz` as fp16 (75 MB) or int8 (~38 MB, quality check) as a
  flat binary + `number_keys` + `names_v12` packed JSON; brute-force cosine over 51k × 512
  in Wasm (~20 ms) or WebGPU.
* Port S7 classical reader + twin verifier (`real1.onnx`, 0.74 MB) + card-back check.
* Add-to-collection queues offline and syncs when back online.
* Total download ≈ 160 MB, one-time, bumped per gallery version.

Not in scope for any phase: a smaller tower (MobileCLIP) — needs re-embedding the gallery
and re-validation (weeks); multi-card spreads; counterfeit detection.

## 8. Execution order (P1, tonight)

1. **pokescan**: `git pull` the main checkout to 8f0ff3b; `build_bundle.py` → verify
   `bundle.json` hashes; `pokescan.serve` with `/health`, `/identify` (multipart, debug),
   `/ref`, names subset; `result_to_dict`; quad capture; smoke test with 3 photos from
   `/tank/pokecard-inbox/wild`.
2. **denils**: `pokescanner` host, bind mount, unit, proxy + Authentik, `provision-pokescanner`
   (not `update-`; new host), gatus entry. Verify `/health` from CT 100.
3. **pokecollector** (this branch): §5.1 config, `external_matcher.py`, provider `external`,
   queue integration, tests; deploy to CT 100 (Komodo compose, the fork image is built
   locally — upstream now ships prebuilt images, our compose keeps `build:`).
4. **Debug panel** + settings block + session toggle.
5. **`/live` P1** page + consensus + add sheet; Authentik app for `scan.nilss.dev`.
6. Scan 20 of the owner's PRE / full-art cards end to end; record every trace; fix what
   breaks; note real latency + RAM in this doc.

Then P2 as a separate branch/PR (`live-ondevice`), P3 after P2 is measured.

## 9. Risks / open items

* **RAM on the new CT**: 1.2 GB resident is an estimate; measure before sizing.
* **Upload orientation**: EXIF must be applied server-side (phones), tested with iOS HEIC.
* **Queue vs live double-processing**: a live-page add goes straight to `/api/collection/`;
  the queue path is untouched — no interaction, but two trace stores (§6).
* **ja/zh-tw candidates** with an `en,de` sync show pokescanner's `/ref` thumbnails and go
  through `ensure_card_exists` live fetch on add. Acceptable for the PoC; revisit if the
  owner starts scanning ja (decision #6 says en+de for now).
* **Composite jobs** (2–4 photos per provider call) are LLM-specific; disabled for
  `external`. `enqueue_scan_job` still runs the provider capability/credential check —
  the `external` provider must satisfy it without a key (test it; PR #4 hit this gap).
* **Gallery version drift** between pokescanner (`v12`) and what the docs/gate numbers
  claim: the `/health` + trace `matcher.bundle` field records it per scan.
* **Authentik + PWA**: the service worker must not cache the auth redirect; scope the SW
  to `/live/` and exclude `/collection/*`.
