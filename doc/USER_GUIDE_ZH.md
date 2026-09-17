# Manga Translator UI 使用手册

本文档说明当前仓库的 Docker WebUI、章节流水线、OCR 与 LaMa 擦除准备、
HQ 翻译、嵌字渲染、任务恢复和结果导出方法。

## 1. 适用场景

本项目提供三类使用方式：

1. 传统 WebUI：处理单图、文件夹、批量图片和普通翻译工作流。
2. 章节流水线：适合按 Chapter 目录组织、页数多、需要中断恢复的长篇作品。
   作品相关设置全部可配置，见 [作品配置](SERIES_CONFIG.md)。
3. `prepare` 准备流程：只执行检测、OCR 和可选的 LaMa 擦除，保存结构化 JSON 与擦除图，
   不执行翻译或嵌字。

当前章节流水线的默认阶段为：

```text
OCR -> HQ 翻译 -> LaMa 擦除 -> 嵌字渲染 -> 可选摘要 -> 可选 Embedding
```

角色归因不在默认流程中。需要时可以单独调用，但不会自动启动。

## 2. 目录约定

章节流水线不绑定任何特定作品。每部作品在自己的配置文件里声明路径、
章节语言规则和阶段默认参数，见 [作品配置](SERIES_CONFIG.md)。

```text
config/series/<slug>.yaml       # 作品声明：路径、语言规则、阶段默认参数
漫画库（compose 挂到 /data/library）/
└── <作品名>/
    ├── RAW/                    # 原稿，只读，程序不修改
    │   ├── Chapter 1/
    │   └── ...
    └── results/
        └── RAW/
            └── Chapter N/
                ├── 001.jpg
                ├── 002.jpg
                └── .pipeline/  # OCR / 擦除 / 渲染的版本化中间产物
任务数据库：packaging/data/pipeline/<slug>.db
```

当前 Compose 配置：

- 宿主机漫画库：`D:\Resources\Download\manga-dl`（由 `.env` 的 `MT_LIBRARY_HOST` 决定）
- 容器漫画库：`/data/library`
- 容器结果：`/data/library/<作品名>/results`
- 任务数据库：`packaging/data/pipeline/`（容器 `/data/pipeline`）

漫画库整体挂载。章节流水线只读原图，所有 OCR、翻译、擦除和渲染结果都写入
`<作品>/results/RAW/Chapter N/.pipeline/`。

每个章节目录只扫描顶层支持的图片文件。以下嵌套目录不会被识别为页面：

```text
Chapter N/manga_translator_work/
Chapter N/.pipeline/
Chapter N/json/
Chapter N/inpainted/
```

## 3. Docker 启动

### 3.1 标准启动

如果本地没有 GPU 镜像，执行：

```powershell
docker compose -f packaging/docker-compose.yml up -d --build manga-translator-gpu
```

如果已经有可用的 `manga-translator:gpu` 镜像，只启动或重建容器：

```powershell
docker compose -f packaging/docker-compose.yml up -d --no-build manga-translator-gpu
```

Compose 会同时启动：

- `manga-translator-gpu`：WebUI、OCR、LaMa、渲染和任务调度。
- `manga-translator-model-manager`：按需加载和卸载 Qwen VL。

旧的独立 Qwen 容器属于可选的 `legacy-qwen` profile，默认不启动：

```powershell
docker compose -f packaging/docker-compose.yml --profile legacy-qwen up -d manga-translator-qwen
```

### 3.2 入口地址

- 普通 WebUI：`http://127.0.0.1:8001/`
- 章节流水线：`http://127.0.0.1:8001/chapters`
- 管理页面：`http://127.0.0.1:8001/admin`
- OpenAPI 文档：`http://127.0.0.1:8001/docs`

首次部署请修改 Compose 中的管理员密码，不要长期使用示例密码。

## 4. 环境变量和 API

Compose 通过项目根目录 `.env` 注入配置，文件不会提交到 Git。

作品相关的设置（路径、章节语言、阶段默认参数）**不在环境变量里**，而在
`config/series/<slug>.yaml`，详见 [作品配置](SERIES_CONFIG.md)。

`.env` 里只放"启用哪部作品"和部署级参数：

```text
MT_SERIES=wireless-onahole                # 启用 config/series/<slug>.yaml
MT_LIBRARY_HOST=D:/Resources/Download/manga-dl   # 宿主机漫画库根目录
OPENAI_API_BASE=http://127.0.0.1:18081/v1
OPENAI_API_KEY=
OPENAI_MODEL=local-qwen35-4b-vl
GEMINI_API_BASE=https://generativelanguage.googleapis.com
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.8-flash
```

> Compose 的变量插值读取的是**运行 `docker compose` 时所在目录**的 `.env`，
> 所以请在项目根目录执行 compose 命令。

容器内的部署级变量（由 Compose 固定）：

```text
MT_SERIES_DIR=/app/config/series
MT_LIBRARY_ROOT=/data/library
MT_PIPELINE_ROOT=/data/pipeline
MT_MODEL_MANAGER_URL=http://manga-translator-model-manager:8090
MT_MODEL_MANAGER_TOKEN=...
MT_QWEN_MODEL_ID=qwen35-4b-vl
MT_QWEN_CONTEXT_SIZE=24576
MT_INPAINTER_MODEL=lama_large
MT_OCR_PAGES_PER_BATCH=8
```

仍然可用的路径覆盖变量（优先级高于作品配置，正常不需要设置）：
`MT_MANGA_DATA_ROOT`、`MT_MANGA_RAW_DIR`、`MT_MANGA_RESULTS_DIR`、
`MT_PIPELINE_DB`、`MT_PIPELINE_UPLOAD_DIR`、`MT_EXPECTED_CHAPTERS`、`MT_EXPECTED_PAGES`。

修改 `.env` 或 `config/series/*.yaml` 后需要重建容器：

```powershell
docker compose -f packaging/docker-compose.yml up -d --no-build manga-translator-gpu
```

### 4.1 测试 Gemini

```powershell
docker exec manga-translator-gpu python -c "import asyncio; from manga_translator import Config; from manga_translator.translators.gemini import GeminiTranslator; t=GeminiTranslator(); t.parse_args(Config()); print(t.model_name, bool(t.api_key)); print(asyncio.run(t._translate('KOR','CHS',['감사합니다'],None)))"
```

Gemini 偶尔返回 `503 high demand`，项目会自动重试。持续收到 `429` 时需要检查配额和 Key 轮换配置。

## 5. 章节流水线 WebUI

打开：

```text
http://127.0.0.1:8001/chapters
```

页面提供以下功能：

- 扫描服务器 RAW 目录。
- 上传文件夹并保留 `webkitRelativePath`。
- 选择章节和阶段。
- 创建、暂停、继续、取消任务。
- 重试失败阶段。
- 对单页重新翻译。
- 查看角色确认项。
- 下载按章节结构组织的 ZIP。

### 5.1 文件夹上传

浏览器上传时，前端会发送相对路径，例如：

```text
Wireless Onahole/RAW/Chapter 70/001.jpg
```

服务端会校验绝对路径、Windows 盘符、`../`、符号链接和超出上传根目录的路径。

路径开头与当前作品相关的目录名会被自动去掉，去掉哪些前缀由作品配置推导
（作品名、slug、原稿目录名，以及固定的 `RAW`）。例如上面的路径会归一到
`Chapter 70/001.jpg`。输入目录中的 `manga_translator_work` 等嵌套目录不会
进入任务清单。

### 5.2 章节语言

语言不再由代码写死，而是由作品配置的 `languages` 规则决定：

```yaml
# config/series/wireless-onahole.yaml
languages:
  - chapters: "1-90"
    source: ko
    ocr_model: paddleocr_korean
    ocr_hint: Korean
    translator_code: KOR
  - chapters: "*"
    source: es
    ocr_model: paddleocr_latin
    ocr_hint: Spanish
    translator_code: ESP
```

规则按顺序匹配，第一条命中的生效，所以最后一条通常是 `"*"` 兜底。
不需要手工选择 OCR 模型，章节流水线会按目录名里的章节号自动路由。

选择器支持 `"1-90"`、`"91-"`、`"-14"`、`"114.5"`、`"1-14,24.5"` 等写法。
未命中任何规则的章节会记为扫描错误，不会被猜成某种语言。

页面右上角标题悬浮会显示当前作品、配置文件路径、原稿/结果目录和语言规则。

### 5.3 阶段选择

只做 OCR：

```text
OCR
```

OCR 后做 LaMa 擦除：

```text
OCR -> INPAINT
```

HQ 翻译和嵌字：

```text
OCR -> TRANSLATE -> INPAINT -> RENDER
```

完整验收：

```text
OCR -> TRANSLATE -> INPAINT -> RENDER
```

`SUMMARIZE` 和 `EMBED` 属于可选阶段。摘要需要把多页文本发送给模型，建议在核心图片交付完成后单独执行。

## 6. OCR 与擦除准备（prepare）

`prepare` 是"不翻译"的准备流程：

1. 文本检测。
2. 文本区域合并。
3. 按作品配置的语言规则选择 OCR 模型并识别。
4. 生成页面 JSON 和版本化 OCR 检查点。
5. 可选：用 LaMa 按 OCR 蒙版擦除文字，并发布干净图。

它不会执行翻译、嵌字渲染或 ZIP 打包。

### 6.1 从 WebUI 创建

只做 OCR：

1. 打开 `/chapters`。
2. 点击“扫描 RAW”。
3. 全选章节。
4. 阶段只勾选“OCR”。
5. 点击“创建任务”。

OCR + 擦除：阶段同时勾选“OCR”和“LaMa 擦除”。

> WebUI 不暴露 OCR/擦除的细项参数，只会使用作品配置里的 `defaults`。
> 需要临时调参时用下面的 CLI。

### 6.2 使用命令行工具

`python -m manga_translator.chapter_pipeline.prepare` 负责"不翻译的准备工作"：
OCR，以及可选的 LaMa 擦除。所有参数默认取当前作品的配置。

处理当前作品的全部章节：

```powershell
docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare
```

只处理指定章节：

```powershell
docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare `
  --chapters "Chapter 70" "Chapter 71" "Chapter 72"
```

OCR 后接着做 LaMa 擦除，并把干净图发布到 `results/RAW/<Chapter>/`：

```powershell
docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare `
  --stages ocr inpaint `
  --chapters "Chapter 70" `
  --mask-dilation 20 --inpainting-size 2048
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--series <slug>` | 临时启用另一部作品（不改 `.env`） |
| `--input-root <path>` | 覆盖原稿目录（默认取作品配置） |
| `--chapters ...` | 章节目录名，缺省为全部章节 |
| `--stages ocr inpaint` | 要执行的阶段，缺省只做 `ocr` |
| `--pages-per-batch N` | 覆盖 OCR 批大小 |
| `--mask-dilation N` | 覆盖擦除蒙版膨胀像素 |
| `--inpainting-size N` | 覆盖擦除处理尺寸 |
| `--no-publish` | 擦除结果只留在 `.pipeline/inpaint/`，不发布到 `results/RAW/` |

报告默认写到：

```text
<作品 results>/prepare-report.json
```

### 6.3 OCR 参数

OCR 参数来自作品的 `defaults.stage_config.ocr`（见
[作品配置](SERIES_CONFIG.md#2-完整字段说明)）。历史验证过的一组韩漫长条参数：

```text
忽略非气泡文本：0.3
韩文 OCR：paddleocr_korean
西班牙文 OCR：paddleocr_latin
检测器：default
检测尺寸：2048
每批页数：8
文本阈值：0.45
边界框阈值：0.65
Unclip 比例：2.5
最小框面积比：0.0008
混合 OCR 后备：关闭
MangaLens 气泡限制：关闭
```

> 注意：`packaging/data/config/config.json` 里也有一份全局 OCR 配置
> （默认 `ignore_bubble: 0.0`、`use_hybrid_ocr: true`）。
> **只有作品配置里显式写出的键才会覆盖它**，没写的键会沿用 config.json。
> 因此建议在作品配置里把上面这些键都写全；CLI 在检测到缺少
> `stage_config.ocr` 时会打印警告。

#### 过滤列表（跳过水印 / 广告）

`config/filter_list.json` 里命中的文本区域会在 **OCR 阶段被整行丢弃**——
既不参与翻译，也不会被擦除或渲染，原图保持原样。匹配不区分大小写：

```json
{
  "contains": ["newtoki", "뉴토", "웹툰왕국", "제공사", "가장 빠른", "가장 바른"],
  "exact": []
}
```

- `contains`：原文包含该子串即过滤（用于水印这类 OCR 结果有波动的场景）
- `exact`：原文完全等于该字符串才过滤（用于固定文案）

> 上面这组规则是实测调出来的：韩漫常见的 `NEWTOKI / 뉴토끼` 系水印，OCR 会把
> `뉴토끼` 认成 `뉴토까`、`제공사이트` 认成 `제정사이트`、`웹툰` 认成 `원문`。
> 只按关键词无法覆盖全部变体，所以补了水印固定的开头 `가장 빠른` / `가장 바른`。
> 已用整章 126 个对白区域验证：命中全部水印、零误伤。若换作品请重新验证——
> 例如对白里真的出现「가장 빠른（最快的）」时会被误过滤，后果是该行保持原文不翻译。

两个要点：

1. **章节流水线始终启用过滤列表**，只看 `config/filter_list.json` 的内容。
   `config.json` 里的 `filter_text_enabled` 是传统 WebUI 的开关，对章节流水线无效。
2. **过滤规则参与 OCR 阶段版本哈希**。改完 `filter_list.json` 后重跑，
   OCR 会自动失效并重新识别，不需要手工清理缓存或数据库。

> 水印这类浅色文字**不要指望 LaMa 擦干净**——它会按笔画逐条填补，留下斑驳灰块，
> 加大 `mask_dilation` 只会把灰块摊得更大。正确做法是加进过滤列表让它保持原样清晰。

关闭 MangaOCR 后备的原因：

- 它只在主 OCR 返回空文本或低置信度时触发。
- 实测返回结果多为 `0.0` 或低于过滤阈值，没有有效补充。
- 韩文主 OCR 已完成识别时，后备只会增加耗时。

### 6.4 OCR 输出

每个页面会生成类似：

```text
<作品 results>/RAW/Chapter 1/.pipeline/ocr/<version>/001.json
```

页面 JSON 包含：

```json
{
  "schema_version": 2,
  "relative_path": "Chapter 1/001.jpg",
  "source_language": "ko",
  "artifact_version": "...",
  "review_status": "auto_accepted",
  "regions": [
    {
      "text": "원문",
      "translation": "",
      "lines": [],
      "speaker_id": null,
      "speaker_confidence": null,
      "speech_type": "unknown",
      "review_status": "auto_accepted"
    }
  ]
}
```

SQLite 只保存索引、状态、版本、哈希和关系。完整 OCR 内容保存在 JSON 文件中。

## 7. OCR 后连接 LaMa 擦除

创建 `OCR -> INPAINT` 任务后：

1. OCR 阶段先完成全部选中章节。
2. OCR 模型卸载。
3. LaMa 使用 OCR JSON 中的蒙版执行擦除。
4. 中间图片写入 `<作品 results>/RAW/Chapter N/.pipeline/inpaint/<version>/`。
5. 用 CLI 跑时还会把擦除结果发布到 `<作品 results>/RAW/Chapter N/`，
   可以直接取用；后续跑 RENDER 会用嵌字成品覆盖同一路径。

创建任务的阶段设置为：

```json
["ocr", "inpaint"]
```

执行方式二选一：

- WebUI 中只勾选“OCR”和“LaMa 擦除”（使用作品配置里的 `defaults`）
- CLI（可临时覆盖参数，推荐）：
  `python -m manga_translator.chapter_pipeline.prepare --stages ocr inpaint`

`acceptance` 默认执行 OCR、HQ 翻译、LaMa 和渲染，不适合只想做 OCR 加擦除的批次。

擦除参数（`mask_dilation`、`inpainting_size`、`models.inpaint`）来自作品配置的
`defaults`，可用 `--mask-dilation` / `--inpainting-size` 临时覆盖。
按版本失效规则，改擦除参数只需重跑擦除，OCR 结果会复用。

## 8. HQ 翻译

翻译阶段直接调用现有 `openai_hq` 兼容流程，不经过英语中转。

当前本地 Qwen 配置：

```text
模型：local-qwen35-4b-vl
上下文：24576
每批图片：最多 2 页
每批文本区域：最多 60
目标语言：简体中文
```

如果批次返回数量不匹配，系统会依次：

1. 拆成更小的页面批次。
2. 失败后拆成单页。
3. 单页仍失败时拆成单区域。

这样单个密集页面不会导致整章任务失败。

## 9. LaMa 和嵌字

LaMa 默认使用：

```text
inpainter=lama_large
inpainting_size=2048
mask_dilation=20
```

渲染阶段复用已有 `load_text` 渲染器：

- 使用 OCR 坐标。
- 使用 HQ 翻译文本。
- 使用已生成的 LaMa 修复图。
- 应用本地字体、排版和气泡适配。

最终图片写入：

```text
results/RAW/Chapter N/文件名.ext
```

版本化图片保留在：

```text
results/RAW/Chapter N/.pipeline/render/<version>/
```

## 10. 任务恢复和版本复用

任务状态按作品分开保存：

```text
packaging/data/pipeline/pipeline.db             # Love Quest（沿用历史）
packaging/data/pipeline/wireless-onahole.db     # Wireless Onahole
```

数据库文件名由作品配置的 `paths.database` 决定；未指定时默认放在
`<作品 results 的父目录>/pipeline.db`。

包含：

- `series`
- `chapters`
- `pages`
- `regions`
- `jobs`
- `chapter_stages`
- `artifacts`
- `model_runs`
- `translation_memory`
- `characters`
- `aliases`
- `terms`
- `summaries`
- `events`
- `embeddings`

进程终止后，正在运行的任务会标记为 `interrupted`。再次继续时会复用已完成阶段。

版本失效规则：

| 变更 | 需要重跑 |
| --- | --- |
| OCR 模型或 OCR 参数 | OCR 及全部下游 |
| 翻译模型、Gemini/OpenAI API 或翻译参数 | 翻译及渲染 |
| LaMa 模型或修复参数 | 擦除及渲染 |
| 字体、排版或渲染器 | 渲染 |
| 摘要模型 | 摘要及可选 Embedding |

页面 OCR JSON 已存在时，OCR 分块续跑会跳过该页，不会重复识别。

## 11. API

主要接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/chapters/status` | 流水线状态 |
| `GET` | `/chapters/models` | 模型管理器状态 |
| `GET` | `/chapters/inventory` | 扫描 RAW |
| `POST` | `/chapters/uploads` | 上传目录 |
| `POST` | `/chapters/jobs` | 创建任务 |
| `GET` | `/chapters/jobs` | 任务列表 |
| `GET` | `/chapters/jobs/{id}` | 任务详情 |
| `POST` | `/chapters/jobs/{id}/pause` | 暂停 |
| `POST` | `/chapters/jobs/{id}/resume` | 继续 |
| `POST` | `/chapters/jobs/{id}/cancel` | 取消 |
| `POST` | `/chapters/jobs/{id}/retry` | 重试阶段 |
| `POST` | `/chapters/jobs/{id}/retranslate-page` | 单页重译 |
| `GET` | `/chapters/jobs/{id}/download` | 下载 ZIP |
| `GET` | `/chapters/characters` | 角色列表 |
| `POST` | `/chapters/characters/{id}/confirm` | 确认角色 |

### 11.1 创建 OCR 任务

作品配置里的 `defaults` 会作为基础参数，`options` 只写需要覆盖的部分。
下面的写法与 CLI `prepare` 等价：

```powershell
$body = @{
  chapter_names = @()          # 空数组 = 当前作品全部章节
  stages = @("ocr")
  options = @{}                # 全部取作品配置默认值
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8001/chapters/jobs" `
  -ContentType "application/json" `
  -Body $body
```

需要临时调参时再显式覆盖，例如：

```powershell
$body = @{
  chapter_names = @("Chapter 70")
  stages = @("ocr")
  options = @{
    ocr_pages_per_batch = 4
    stage_config = @{
      ocr = @{ ignore_bubble = 0.4 }
    }
  }
} | ConvertTo-Json -Depth 8
```

### 11.2 创建 OCR 加 LaMa 任务

将 `stages` 改为：

```json
["ocr", "inpaint"]
```

### 11.3 查询任务

```powershell
Invoke-RestMethod "http://127.0.0.1:8001/chapters/jobs/<job-id>"
```

## 12. ZIP 导出

任务完成后打开：

```text
http://127.0.0.1:8001/chapters/jobs/<job-id>/download
```

ZIP 结构保持原目录：

```text
Chapter 1/001.jpg
Chapter 1/002.jpg
Chapter 2/001.jpg
Chapter 3/001.jpg
```

文件名和扩展名保持原图格式，不包含 `.pipeline` 中间目录。

## 13. 性能参考

以下数据来自 RTX 4060 Laptop 8GB，使用前三话共 31 页验收：

| 阶段 | 实测速度 |
| --- | ---: |
| OCR | 约 12.9 秒/页 |
| HQ 翻译 | 约 11.7 秒/页 |
| LaMa 擦除 | 约 4.3 秒/页 |
| 嵌字渲染 | 约 4.8 秒/页 |

单页全流程约 `33.7 秒`，不含模型首次加载和失败重试。连续跑多章节时，OCR 和 Qwen
模型会按语言或阶段复用，整体速度优于每章单独启动。

## 14. 常见问题

### OCR 很慢

长条漫检测是多切片处理，通常比普通单页慢。优先检查：

- `ignore_bubble` 是否为 `0.3`。
- 是否启用了不必要的混合 OCR。
- 是否启用了 MangaLens 气泡限制。
- 当前是否同时加载 Qwen，8GB 显存会互相挤占。

### 显存不足

模型管理器会按阶段加载和卸载模型。检查：

```powershell
docker exec manga-translator-model-manager curl -s -H "X-Internal-Token: manga-translator-internal" http://127.0.0.1:8090/status
```

如果 Qwen 和 OCR 同时驻留，先卸载 Qwen：

```powershell
docker exec manga-translator-model-manager curl -s -X POST -H "X-Internal-Token: manga-translator-internal" http://127.0.0.1:8090/models/unload
```

### Qwen 返回上下文超限

降低 HQ 批大小：

```text
hq_batch_size=2
hq_max_regions_per_request=60
```

不要只增加 `MT_QWEN_CONTEXT_SIZE`。8GB 显存同时增加上下文和图片批次很容易触发 OOM。

### Gemini 或 OpenAI 返回 503

这通常是上游临时压力，项目会自动重试。如果连续失败，检查：

- API Key 是否有效。
- API 服务是否可达。
- 是否配置了代理。
- 是否触发配额或频率限制。

### 容器看不到新代码

当前开发配置将宿主机 `manga_translator/` 以只读方式挂载到容器。修改 Python 代码后：

```powershell
docker restart manga-translator-gpu
```

修改 Compose、`.env` 或镜像内容后：

```powershell
docker compose -f packaging/docker-compose.yml up -d --no-build manga-translator-gpu
```

### 端口被占用

默认 Web 端口为：

```text
127.0.0.1:8001 -> 8000
```

如果 8001 被占用，修改 `packaging/docker-compose.yml` 中的端口映射后重建容器。

## 15. 数据备份

至少备份：

```text
config/series/                  # 作品配置（路径、语言规则、阶段参数）
packaging/data/pipeline/        # 各作品的任务数据库
packaging/data/config/
packaging/data/server/
packaging/data/models/
```

RAW 目录应单独保留原始副本。结果 ZIP 可以随时从章节流水线重新导出。

## 16. 当前边界

- 章节语言由作品配置决定；配置里没有覆盖到的章节会被记为扫描错误，不会被猜测。
- `acceptance` 要求所选章节属于同一源语言，跨语言批次请用 `prepare`。
- 一个容器同时只启用一部作品（由 `.env` 的 `MT_SERIES` 决定）；切换需要重建容器。
- 角色判断不属于当前默认流程。
- `prepare` 只输出结构化 JSON 和擦除图，不负责翻译或嵌字。
- 自动摘要和 Embedding 默认不作为核心交付前置条件。
- 在线 API 的可用性、费用、配额和内容规则由对应服务商决定。
- 使用 OCR、翻译、修复和发布前，需要自行确认素材授权和所在地法律要求。
