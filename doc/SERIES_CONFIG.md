# 作品配置（Series Configuration）

章节流水线不针对任何特定漫画。所有"作品相关"的设置都放在
`config/series/<slug>.yaml` 里；环境变量只负责**启用哪部作品**和部署级参数。

加一部新作品 = 新增一个 yaml 文件（+ 改一行 `.env`），不需要改代码或
`docker-compose.yml`。

---

## 1. 文件位置与启用方式

```text
config/series/
├── love-quest.yaml
└── wireless-onahole.yaml
```

容器内对应 `/app/config/series`（compose 把 repo 的 `config/series`
挂到那里，宿主机与容器共用同一份文件）。

启用哪部作品：

```text
.env
MT_SERIES="wireless-onahole"     # 对应 yaml 里的 slug 或文件名
```

或者不改 `.env`，用 CLI 的 `--series` 临时指定：

```powershell
docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare --series love-quest
```

选择优先级：

1. `MT_SERIES_CONFIG`：直接指向一个 yaml 文件路径（最高优先级）
2. `MT_SERIES`：slug，在 `MT_SERIES_DIR` 里查找；也可以直接给文件路径
3. 都不设时：`MT_SERIES_DIR` 里只有一个 yaml 就自动使用它；有多个则报错并列出可选 slug

`MT_SERIES_DIR` 默认是 `<项目目录>/config/series`，容器里即 `/app/config/series`。

---

## 2. 完整字段说明

```yaml
name: Wireless Onahole        # 必填。显示名，也用于 series_id（经 slug 化）
slug: wireless-onahole        # 可选。缺省由 name 推导；改它会导致历史数据另起一套

paths:
  raw:     "${MT_LIBRARY_ROOT}/Wireless Onahole Raw/RAW"
  results: "${MT_LIBRARY_ROOT}/Wireless Onahole Raw/results"
  database: "${MT_PIPELINE_ROOT}/wireless-onahole.db"   # 可选
  uploads:  ...                                          # 可选，缺省 <data_root>/uploads

inventory:
  expected_chapters: 0        # 0 = 关闭章节数校验
  expected_pages: 0           # 0 = 关闭页数校验
  chapter_pattern: null       # 可选正则，必须 fullmatch 目录名且在第 1 组捕获章节号

languages:                    # 必填。按顺序匹配，第一条命中的生效
  - chapters: "1-90"
    source: ko                # 语言代码，会写进 chapters/pages/regions 表
    ocr_model: paddleocr_korean
    ocr_hint: Korean          # 仅对支持语言提示的 OCR 模型（如 paddleocr_vl）生效
    translator_code: KOR      # 可选；缺省由 source 推断（见 §2.3）
    # 除上面 5 个保留键之外，任何键都是该章节区间的参数覆盖（见 §2.5）
    stage_config:
      ocr:
        text_threshold: 0.45
        box_threshold: 0.65
  - chapters: "*"             # 兜底规则，建议始终保留
    source: es
    ocr_model: paddleocr_latin
    ocr_hint: Spanish
    translator_code: ESP
    stage_config:             # 西语块单独一套，与韩文块互不影响
      ocr:
        text_threshold: 0.35
        ignore_bubble: 0.5
    mask_dilation: 15

translation:
  target: zh-Hans             # 写入页面 JSON 的 target_language
  target_code: CHS            # 传给翻译器的目标语言代码

defaults:                     # 跨语言基线，schema 与 /chapters/jobs 的 options 一致
  ocr_pages_per_batch: 8
  mask_dilation: 20
  models:
    inpaint: lama_large
  stage_config:
    ocr:
      detector: default
      detection_size: 2048
      ignore_bubble: 0.3
      ...
    inpaint:
      inpainting_size: 2048
```

### 2.1 `chapters` 选择器语法

| 写法 | 含义 |
| --- | --- |
| `"*"` | 全部章节 |
| `"1-90"` | 闭区间，含两端 |
| `"91-"` | 从 91 起，无上界 |
| `"-14"` | 到 14 为止 |
| `"114.5"` | 精确匹配单话 |
| `"1-14,24.5"` | 逗号分隔的任意组合 |

未命中任何规则的章节会被记为扫描错误（`errors`），不会被"猜"成某种语言。
所以请始终保留一条 `chapters: "*"` 的兜底规则。

### 2.2 路径与环境变量

`paths` 里的字符串支持 `${VAR}` / `$VAR` 展开。未定义的变量会原样保留，
不会静默变成空字符串——这样配错时能直接看出问题。

这样同一份 yaml 在宿主机和容器里都能用：

| 变量 | 容器内 | 宿主机 |
| --- | --- | --- |
| `MT_LIBRARY_ROOT` | `/data/library`（compose `environment`） | 需自行设置，如 `D:/Resources/Download/manga-dl` |
| `MT_PIPELINE_ROOT` | `/data/pipeline`（compose `environment`） | 需自行设置 |

相对路径按**yaml 文件所在目录**解析。

### 2.3 语言代码与翻译器代码

`source` 是写入数据库的规范语言代码（如 `ko`、`es`、`ja`、`en`）。
`translator_code` 是翻译器期望的代码（如 `KOR`、`ESP`、`JPN`），
不写时会用内置对照表从 `source` 推断；表里没有的语言请显式写上。

### 2.4 同一语言只用一套 OCR 模型

OCR 阶段按 `(source, ocr_model)` 分组，一组内只驻留一个模型。
同一个 `source` 的不同区间如果需要不同 OCR 模型，会被拆成两组分别跑
（会多一次模型装卸），但版本哈希与阶段配置仍然一致。

### 2.5 按语言（章节区间）覆盖参数

每条语言规则里，除 5 个保留键以外的键都会成为**该章节区间的参数覆盖**：

保留键（不当作参数）：`chapters`、`source`、`ocr_model`、`ocr_hint`、`translator_code`

覆盖是**深合并**的——只写要改的键即可，没写的沿用 `defaults`：

```yaml
defaults:
  mask_dilation: 20
  stage_config:
    ocr:
      text_threshold: 0.45
      box_threshold: 0.65
      ignore_bubble: 0.3
      detector: default

languages:
  - chapters: "91-"
    source: es
    ocr_model: paddleocr_latin
    stage_config:
      ocr:
        text_threshold: 0.35      # 只改这一项
    mask_dilation: 15             # 覆盖顶层项
```

该区间最终生效的参数 = `text_threshold 0.35` + `box_threshold 0.65` +
`ignore_bubble 0.3` + `detector default` + `mask_dilation 15`。

优先级（低 → 高）：

| 层级 | 载体 |
| --- | --- |
| 1 | 作品配置的 `defaults`（跨语言基线） |
| 2 | 命中的语言规则的参数覆盖 |
| 3 | 作业 `options` / CLI 参数 |

两条规则：

- **`ocr_model` 是每条规则内 OCR 模型的唯一来源**。解析时会同步写入
  `models.ocr`，因此模型与参数永远配套；作业级 `models.ocr` 仍然可以强制覆盖。
- **改任何参数都会让该章节的 OCR 阶段版本哈希变化**，重跑时自动失效重做，
  不需要手工清缓存。

> 建议：`defaults` 里始终保留一套完整可用的 OCR 参数作为基线。这样新加的
> 语言区间即使还没调参，也不会掉回 `config.json` 里那套通用默认值。

---

## 3. 优先级

| 层级 | 载体 | 优先级 |
| --- | --- | --- |
| 任务级 | `/chapters/jobs` 的 `options`、CLI 参数 | 最高 |
| 环境变量 | `MT_MANGA_RAW_DIR`、`MT_MANGA_RESULTS_DIR`、`MT_PIPELINE_DB`、`MT_PIPELINE_UPLOAD_DIR`、`MT_MANGA_DATA_ROOT` | 中 |
| 作品配置 | `config/series/<slug>.yaml` 的 `paths` / `defaults` | 低 |

`MT_MANGA_*` 这些旧变量保留下来作为**显式覆盖**，方便在一次运行里临时改路径；
正常情况下不需要设置。

---

## 4. 新增一部作品

1. 复制一份现成的 yaml（`config/series/love-quest.yaml` 是个好起点）：

   ```bash
   cp config/series/love-quest.yaml config/series/my-manga.yaml
   ```

2. 改 `name` / `slug` / `paths` / `languages`，按需要调 `defaults`。

3. 确认 RAW 目录就在 `${MT_LIBRARY_ROOT}` 下面（compose 挂的是整个漫画库，
   所以绝大多数情况下不需要动 compose）。

4. 用 CLI 或 WebUI 验证扫描结果：

   ```powershell
   # 只看扫描结果，不跑任务
   curl "http://127.0.0.1:8001/chapters/inventory?include_hashes=false"
   ```

   检查 `chapter_count`、`page_count`、`valid`、`errors`，以及各章
   `source_language` 是否符合预期。

5. 切换启用作品后重建容器：

   ```powershell
   # 修改 .env 里的 MT_SERIES
   docker compose -f packaging/docker-compose.yml up -d --no-build manga-translator-gpu
   ```

---

## 5. 相关环境变量速查

| 变量 | 作用 |
| --- | --- |
| `MT_SERIES` | 启用哪部作品（slug 或 yaml 路径） |
| `MT_SERIES_CONFIG` | 直接指定 yaml 路径，优先级最高 |
| `MT_SERIES_DIR` | 系列配置目录，默认 `<项目>/config/series` |
| `MT_LIBRARY_ROOT` | 漫画库根目录，供 yaml 里的 `${MT_LIBRARY_ROOT}` 使用 |
| `MT_PIPELINE_ROOT` | 任务数据库所在目录 |
| `MT_MANGA_DATA_ROOT` / `MT_MANGA_RAW_DIR` / `MT_MANGA_RESULTS_DIR` | 临时覆盖作品路径 |
| `MT_PIPELINE_DB` / `MT_PIPELINE_UPLOAD_DIR` | 临时覆盖数据库 / 上传目录 |
| `MT_EXPECTED_CHAPTERS` / `MT_EXPECTED_PAGES` | 临时覆盖校验值，`0` = 关闭 |

其余部署级变量（模型管理器地址、Qwen、翻译服务等）见
[使用手册 §4](USER_GUIDE_ZH.md#4-环境变量和-api)。

---

## 6. 哪些设置不在作品配置里

| 设置 | 位置 | 说明 |
| --- | --- | --- |
| 文本过滤列表 | `config/filter_list.json` | 全局，跨作品共用。命中区域在 OCR 阶段整行丢弃（不擦不译不渲染）。改完会自动让 OCR 阶段失效重跑，见[使用手册 §6.3](USER_GUIDE_ZH.md#63-ocr-参数)。**换作品后建议重新验证规则**，不同作品的 OCR 误认模式不同 |
| 全局 OCR / 检测 / 修复默认值 | `packaging/data/config/config.json` | 作品配置里显式写出的键会覆盖它，没写的键沿用 |
| API Key / 模型服务地址 | `.env`、`packaging/data/app.env` | 部署级 |
| 字体、排版、渲染器 | `packaging/data/config/config.json` | 与作品无关 |
| 角色、术语、翻译记忆 | `<作品>/pipeline.db` 或 `packaging/data/pipeline/*.db` | 按作品分开存储 |
