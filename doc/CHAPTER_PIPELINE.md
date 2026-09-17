# Chapter Pipeline

完整中文使用说明请阅读 [项目使用手册](USER_GUIDE_ZH.md)。

Chapter Pipeline is the Docker WebUI workflow for long manga series. It keeps
the existing detector, PaddleOCR, `lama_large`, renderer, `translate_json_only`,
and `load_text` implementations, and adds resumable orchestration around them.

## Fixed Layout

- Current host RAW: `D:\Resources\Download\manga-dl\Love Quest Raw\RAW`
- Read-only source in container: `/data/love-quest/RAW`
- Host results: `D:\Resources\Download\manga-dl\Love Quest Raw\results`
- Results in container: `/data/love-quest/results`
- Versioned intermediates: `results/RAW/Chapter N/.pipeline/`
- Upload staging and job database: `packaging/data/pipeline/`

Only top-level image files in each chapter directory are inventoried. Nested
directories such as `Chapter 32/manga_translator_work` are ignored.

Language routing is fixed:

- `Chapter 1` through `Chapter 14`: Korean, `paddleocr_korean`
- `Chapter 15` through `Chapter 33` and `Chapter 24.5`: Spanish,
  `paddleocr_latin`

## Start

```powershell
docker compose -f packaging/docker-compose.yml up -d --build manga-translator-gpu
```

Open `http://127.0.0.1:8001/chapters`. The page can scan `RAW`, upload a folder
while retaining `webkitRelativePath`, create and control jobs, confirm uncertain
characters, retry stages, retranslate a page, and download chapter ZIPs.

The model-manager container listens only on the internal Compose network. It
allowlists `qwen35-4b-vl` and `local-embedding`; the Web container never receives
the Docker socket.

## Stage Model

The default persisted order is:

1. `ocr`
2. `translate` (`openai_hq` compatible high-quality mode)
3. `inpaint`
4. `render`
5. `summarize` (optional)
6. `embed` (disabled by default)

OCR is stage-first across all selected chapters. The Korean or Latin OCR model
is loaded once, all OCR pages are processed, and the model is unloaded before
HQ translation starts. Character attribution is not part of the default flow.
OCR-only jobs stop after structured JSON extraction. An `OCR -> INPAINT` job
continues with LaMa without starting translation or rendering.
When enabled, HQ translation sends at most two pages or 60 regions per request
and automatically retries smaller page or region batches if a response count
does not match.

Each stage writes a versioned JSON manifest and page artifacts. Stage
completion, attempts, hashes, timings, memory metrics, and page checkpoints are
stored in `pipeline.db`.

Changing a translation provider reuses OCR and inpaint artifacts. Running only
OCR or `OCR -> INPAINT` does not load Qwen or the renderer. A completed stage is
skipped only when its exact version manifest is still present.

## Provider Switching

Local Qwen is the default:

```text
MT_TRANSLATION_PROVIDER=local
OPENAI_API_BASE=http://manga-translator-model-manager:8080/v1
OPENAI_MODEL=local-qwen35-4b-vl
```

An OpenAI-compatible online provider can be enabled without code changes:

```text
MT_TRANSLATION_PROVIDER=online
MT_ONLINE_OPENAI_BASE=https://example.invalid/v1
MT_ONLINE_OPENAI_API_KEY=...
MT_ONLINE_OPENAI_MODEL=...
```

OCR and translated page JSON keep the same schema across providers.

## Review Rules

- Only confirmed terms or automatically accepted terms with confidence at least
  `0.95` are treated as mandatory.
- Chapter summaries and events retain page/region citations.
- Embeddings are disabled unless `MT_ENABLE_EMBEDDINGS=true`; exact/FTS
  translation-memory retrieval is used first.

## Batch OCR + Erase Workflow

Validated loop (2026-09-17, Wireless Onahole 70–90 Korean / 91+ Spanish) for
batching new chapters of any series. Per-series behaviour lives in
`config/series/<slug>.yaml`; see `doc/SERIES_CONFIG.md`.

### 1. Pre-flight

- Container healthy, correct branch, both copies of `config/filter_list.json`
  and `packaging/data/config/filter_list.json` in sync, disk headroom.
- Inventory page counts of the target chapters; flag oversized chapters
  (they may be bundled volumes needing a separate decision).

### 2. Pilot batch (3 chapters, 30–40 pages)

```bash
MSYS_NO_PATHCONV=1 docker exec manga-translator-gpu python -m \
  manga_translator.chapter_pipeline.prepare \
  --stages ocr inpaint \
  --chapters "Chapter A" "Chapter B" "Chapter C" \
  --report-path /data/pipeline/reports/pilot-<lang>.json
```

(`MSYS_NO_PATHCONV=1` is only needed from Git Bash; without it `/data/...`
arguments are rewritten to `C:/Program Files/Git/data/...`.)

The pilot answers four questions: is the language routing correct, is
recognition quality acceptable (accents, punctuation), is the parameter
baseline sufficient, and how do the filter rules hit (watermarks vs
false positives on dialogue).

### 3. Acceptance (four levels, all mandatory)

1. **Report**: `publish_errors: []`, no errored chapters, `report_path`
   intact.
2. **Counts**: published page count == RAW page count per chapter; region
   count not abnormally low (a collapse means the wrong OCR model ran).
3. **Language**: scan `.pipeline/ocr/<hash>/*.json` — Korean-region count
   should be 0 (or single digits) for a Latin block, empty-text regions 0,
   `prob` distribution healthy.
4. **Visual** (mandatory — statistics can be all green while the output is
   broken; see the page-001 incident below): spot-check 2 chapters × 3–5
   pages per batch. To debug, decode `mask_raw` (base64 PNG) and overlay it
   on the raw page to see where the mask actually landed.

### 4. Freeze parameters/filters, then run full batches

- Parameter overrides go into the series YAML language rules, never code.
- **Finalize the filter list before the full run** — any change invalidates
  every OCR cache (filter rules are part of the stage version hash).
- Run the rest in 2–3 batches with separate reports; spot-check between
  batches; update series data status and commit afterwards.

### Throughput reference (720px-wide webtoon strips, lama_large)

OCR ≈ 3.7 s/page (boundary stitching adds ~10%), inpaint ≈ 1.3 s/page;
roughly 9–10 minutes per 100 pages.

### Pitfalls learned

- **Code changes do not invalidate artifacts**: `version_hash` covers only
  stage/model/config/input. After changing pipeline code, delete the affected
  chapters' `.pipeline` directories and re-run.
- **Page 001 mask misalignment** (fixed 2026-09-17): a first page gets the
  next page's head appended with `offset == 0`, which used to short-circuit
  coordinate restoration, leaving the mask at stitched height and smearing
  the inpaint. Kept here because any future coordinate-frame change must be
  checked on the first and last page of a chapter, not just middle pages.
