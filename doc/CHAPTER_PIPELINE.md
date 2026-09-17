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
