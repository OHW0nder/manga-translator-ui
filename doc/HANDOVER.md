# 会话交接文档 — Wireless Onahole 章节流水线

> 更新：2026-09-17。本文档用于跨会话交接，读完即可接手。
> 项目背景与本仓库约定见 `doc/USER_GUIDE_ZH.md`、`doc/SERIES_CONFIG.md`。

---

## 1. 一句话现状

** manga-translator-ui** 已从"写死 Love Quest"改造为**按作品可配置**的章节流水线；
`config/series/wireless-onahole.yaml` 配好了 Wireless Onahole（70–90 韩文 / 91+ 西语 /
74 例外为西语 / 101/103/107/112 为韩文特典）。91–119 已**全量完成并发布**（西语 +
4 个韩文特典章）；70–90 的 OCR + LaMa 擦除（540 页 / 2091 区域 / 全部发布）用的是旧参数，
**仍需重跑**以应用 §3.3/§3.4/§3.6 与新过滤。
代码已提交并推送到 fork 的 `feat/chapter-model-orchestration` 分支。

---

## 2. 目录与关键文件

| 位置 | 作用 |
| --- | --- |
| `config/series/wireless-onahole.yaml` | **本作全部配置**：路径、语言规则、每语言参数覆盖、分页拼接 |
| `config/series/love-quest.yaml` | Love Quest（沿用历史 `packaging/data/pipeline/pipeline.db`） |
| `config/filter_list.json` | 水印/署名过滤（**gitignore 故意排除**，规则抄录在 §5） |
| `packaging/data/config/filter_list.json` | 容器实际读取的那份（两份要同步改） |
| `packaging/data/config/config.json` | 容器全局配置；**作品配置没写的键会沿用这里的值** |
| `packaging/data/models/` | korean/latin OCR onnx、`lama_large_512px.ckpt`（已就位） |
| `packaging/docker-compose.yml` | 挂载整个漫画库 `${MT_LIBRARY_HOST}:/data/library` |
| `manga_translator/chapter_pipeline/` | 流水线实现；`prepare.py` 是 OCR+擦除 CLI |
| `doc/SERIES_CONFIG.md` | 作品配置 schema（§2.5 按语言覆盖参数、§2.6 分页拼接） |

漫画库：`D:\Resources\Download\manga-dl`（compose 挂到 `/data/library`）。
作品目录：`D:\Resources\Download\manga-dl\Wireless Onahole Raw\{RAW,results}`。
容器：`manga-translator-gpu`（WebUI `http://127.0.0.1:8001`，章节页 `/chapters`）。

---

## 3. 本次会话做的改动（已提交 `23d91ccb`，43 文件 +11746/−54）

### 3.1 按作品配置化（消除硬编码）

- 新增 `manga_translator/chapter_pipeline/series_config.py`：
  - `ChapterSelector`：章节区间语法 `"1-90"` / `"91-"` / `"-14"` / `"114.5"` / `"1-14,24.5"` / `"*"`
  - `LanguageRule` / `LanguageRuleSet`：章节→语言→OCR 模型/提示/翻译器代码
  - `SeriesConfig.options_for(number)`：`defaults` + 命中规则的参数覆盖，**深合并**
  - `${VAR}` 环境变量展开（未定义的变量原样保留，便于发现配置错误）
- `PipelineSettings` 持有 `SeriesConfig`；`MT_SERIES` / `MT_SERIES_CONFIG` / `MT_SERIES_DIR`
  选择作品；旧 `MT_MANGA_*` 降级为显式覆盖
- 移除的硬编码：`source_language_for_chapter` 的 `<=14`、Love Quest 路径与系列名、
  `34/567` 预期计数、`ocr_model_korean/spanish`、上传路径前缀 `{love-quest, love quest, raw}`
- `paths.py`：上传前缀改为按作品推导（作品名、slug、原稿目录名、`raw`）
- `stages.py`：`stage_config()` 签名加了 `settings`（传解析后的 options）
- 三层优先级：**作业 options > 语言规则覆盖 > defaults**
- **关键设计**：每章解析一次 options，同一份结果**同时**用于版本哈希与阶段执行

### 3.2 `prepare.py` 取代 `ocr_only.py`

```powershell
docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare `
  --stages ocr inpaint --chapters "Chapter 70" "Chapter 71" ...
```
- `--series` / `--input-root` / `--stages` / `--pages-per-batch` / `--mask-dilation` /
  `--inpainting-size` / `--report-path` / `--no-publish`
- 参数默认取作品配置；擦除完成后把成图发布到 `results/RAW/<Chapter>/`
- 发布失败不再让整个任务报错，改记入报告的 `publish_errors`

### 3.3 过滤列表纳入 OCR 版本哈希

`stages.py:ocr_filter_rules()` 读取 `config/filter_list.json` 并放进 OCR 阶段配置。
**改过滤列表会自动让所有章节 OCR 失效重跑**（这是有意的设计，见 §6 的代价）。

### 3.4 分页拼接（`ocr_boundary_overlap`）

韩式条漫按固定高度切片，两种漏检：
1. 气泡只剩半截轮廓 → 被 `ignore_bubble` 当"气泡外文字"过滤
2. 一行字的上下两半分落两页 → 单页内根本无法识别

实现：`runtime.py` 的 `stitch_boundary_image()`（拼接）+ `remap_stitched_payload()`
（坐标裁剪回本页）+ `_crop_mask_base64()`（蒙版裁剪）。
- 拼接 = 上页尾部 + 本页 + 下页头部，只在本章内相邻页之间
- 跨界区域**在相邻两页各留一份裁剪后的区域**，两侧各自擦掉一半
- 与邻页重叠 <3px 的碎块丢弃
- 该参数参与 OCR 版本哈希；`0` = 关闭
- 拼接图存为 JPEG q=95（存 PNG 会让 `_work/` 达 2MB/页，29 页实测 57.9MB）

**已验证**（2026-09-17）：

| 用例 | 修复前 | 修复后 |
| --- | --- | --- |
| Ch74 页 21/22（气泡跨界） | 022 页漏掉 `LA SEÑORA A LA SU-` | 022 页检出整句 `¿OYE, QUIERES LLEVAR EL BOLSO DE LA SEÑORA A LA SU- CURSAL 1?` |
| Ch81 页 46/47（**一行字**被切半） | 第 3/4 行 `하는데` 两页都漏检 | 待验证（重跑中） |

> 注意：跨界区域会在相邻两页各保留一份**同一段文字**（坐标各自裁剪）。
> OCR+擦除阶段这样是对的（两侧都能擦干净），但**翻译/渲染阶段需要去重或合并**，
> 否则同一句会被翻译并渲染两次。这是后续阶段要处理的已知问题。

---

## 4. 数据状态（截至本文档）

| 项 | 状态 |
| --- | --- |
| 70–90 OCR + 擦除 | 已跑完一轮（旧参数），**需重跑**以应用 §3.3/§3.4/§3.6 与新过滤（过滤改动已使旧缓存失效） |
| Chapter 74 / 81 重跑 | **已完成**（页 001 bug 修复后重跑，验证通过） |
| 91–119 全量（A: 93–100, B: 101–110, C: 111–119+114.5） | **已完成并发布**（三批共 ~473 页，全部四级验收通过；跨批次抽查 14/14 页 PASS） |
| OCR 阶段提速 | **已完成并验收**（长条跳过分格分析 + 扫描哈希缓存，Ch93 14 页 268s→172s，文本集逐一相同），详见 `doc/PERF_chapter_ocr_2026-09-18.md` |
| 1–69 章 | **未处理**，且语言未验证（74 章是西语，说明"1–90 全韩文"不成立） |
| 韩文特典章 101/103/107/112 | **已完成**：语言排查确认四者为韩文合集（各章 86-89% 页面在西语模型下 0-2 区域），yaml 已加韩文路由规则并重跑发布（250 页，韩文区域占 96-100%）；含两轮过滤词补充（짠툰 系变体、뉴토주소、MANGA18、.com 兜底） |

### 3.5 Ch74 重跑验证（西语模型 + 新过滤 + 拼接）

| 指标 | 旧（paddleocr_korean） | 新（paddleocr_latin + 拼接 + 新过滤） |
| --- | --- | --- |
| 语言 | ko（错误） | **es** ✓ |
| 区域数 | 169 | 171 |
| 拉丁/韩文 | 163/6 | 170/0（纯西语）✓ |
| 西语识别 | `TaMPLa3 CONEXION INALAMBRICA`、`DE QUE ESTAS`、`POR QU..` | `TEMPLa5C CONEXIÓN INALÁMBRICA`、`DE QUÉ ESTÁS`、`POR QUÉ...` ✓ |
| 过滤命中 | 4（staff 类） | **0 残留** ✓（staff 类被过滤） |
| 空文本区域 | — | 0 ✓ |
| 发布 | 29 | 29，`publish_errors: []` |

> 例外章 `chapters: "74"` 必须排在区间规则之前（规则是首个命中生效）。
> **1–69 章未验证语言**，Ch74 证明"1–90 全韩文"不成立，批量前必须先做语言抽检。

> Chapter 70 的 `.pipeline` 中间产物曾被误删（见 §7），已由重跑恢复，
> 恢复结果与删除前一致（73 页 / 124 区域）。

### 3.6 页 001 蒙版错位 bug（2026-09-17 发现并修复）

**现象**：凡开启拼接的章节，页 001 的气泡文字不被擦除，文字上方的图案反被 LaMa 涂花。
Ch74/81 重跑版与 91+ 试点 3 章全部中招；70–90 首轮（无拼接）不受影响；中段页正常。

**根因**：`stitch_boundary_image()` 给首页拼上下页头部 256px，但因无上邻页返回
`offset=0`；`remap_stitched_payload()` 与 `_restore_page_coordinates()` 都是
`offset <= 0` 直接返回 → 首页产物里的 `mask_raw`/`original_height` 停留在拼接高度
（页高 +256）。擦除阶段把多出 256px 的蒙版压缩回页面 → 整页错位。

**修复**：`offsets` 改存 `int | None`（None = 未拼接），`offset=0` 也走重映射；
`remap_stitched_payload()` 的短路条件改为 `offset < 0`。offset=0 时区域坐标不需平移，
越界的下页头部区域经钳制后被 <3px 碎块规则丢弃，蒙版裁回页框。

**验收**：5 章重跑后所有页 001 的 `mask_h == raw_h`；目测文字擦净、图案不再涂伤；
121 页 / 699 区域 / `publish_errors: []`。

**教训**：验收不能只看统计指标（当时报告全绿），必须目测成图——本 bug 是像素级
对比 + 蒙版叠加图定位的。另注意 `version_hash` 不含代码版本，**改流水线代码不会
自动失效旧产物**，必须手动删除受影响章节的 `.pipeline` 重跑。

### 4.1 实测速率（Chapter 70 / 73 页 / 169,733 px）

| 阶段 | 耗时 | 速率 |
| --- | ---: | --- |
| OCR（无拼接） | 268.5 s | 3.7 s/页，1.58 s/千像素 |
| 擦除（lama_large） | 98.1 s | 1.3 s/页，0.58 s/千像素 |

70–90 全量（540 页 / 2,769,800 px）：**约 1 小时**（无拼接实测 60m17s）。
开启 256px 拼接后 OCR 约再 +10%。

---

## 5. 水印过滤规则（当前值，两份文件都要同步）

```json
{
  "contains": ["ewtok", "staff", "뉴토", "웹툰왕국", "제공사", "가장 빠른", "가장 바른"],
  "exact": []
}
```

调参历史与依据：

| 规则 | 作用 | 说明 |
| --- | --- | --- |
| `newtoki` | NEWTOKI 站水印 | **已废弃**：OCR 变体多（`NEWTOKEJ6O`/`NEWTOKTGO`/`VEWTOKIJ6T`），含 `newtoki` 的很少 |
| `ewtok` | 同上 | 16 处变体全部命中，对 124 个对白区域零误伤 |
| `staff` | 汉化组署名页 | 24 处（`STAFF` / `LIMPIEZA STAFF` / `TRADUCCION STAFF` / `REDRAW STAFF`） |
| `뉴토` / `웹툰왕국` / `제공사` | 韩文水印行 | 覆盖 `뉴토끼`/`뉴토까` 等变体 |
| `가장 빠른` / `가장 바른` | 水印固定开头 | 兜底（OCR 会把 `웹툰` 认成 `원문`） |
| `paypal` / `temlex` | 页 001 的 PayPal 捐款链接 | 2026-09-17 加入。OCR 变体 `WPAPALMTEMLEXUSE`/`AWWPAYPALMETEMLEXOUIE` 都含 `TEMLEX` |
| `scanesp` / `submanhwa` / `manga18fx` | 西语汉化组站点水印 | 2026-09-17 加入（用户确认）。`TEMPLESCANESP.NET` 各变体稳定含 `SCANESP`；`discord.gg/submanhwa` 由 `submanhwa` 兜住。**注意：加入晚于 74/81/91/92/117 的重跑，这 5 章的站点水印仍被当对白擦除，用户决定不重跑**；全量批（93–119，含 4 个大章）将带新过滤跑 |
| `manga18` / `토주` / `짧툰` / `짭툰` / `짭투` / `툰.c` / `구글검색` / `.com` | 韩文特典章站点水印变体 | 2026-09-18 加入。짠툰.com 被 OCR 搅成 짧툰/짭툰/짭투/툰.com；뉴토주소(뉴토끼地址站) 稳定含 토주；MANGA18.CLUB/.C0M 由 manga18 兜住；.com 为 URL 后缀兜底（三轮迭代后 URL 残留清零） |

**过滤列表已定稿**，全量批可以开始。

**换作品必须重新验证规则**（不同站、不同 OCR 误认模式）。验证方法：把全部区域文本
跑一遍规则，统计命中数与对白误伤数（本次是 40 命中 / 0 误伤）。

**残留水印不要再指望 LaMa 擦干净**：浅灰字 + 非纯白背景会留下斑驳灰块，
加大 `mask_dilation` 只会把灰块摊大（实测 20/60/100 三档对比）。
正确做法就是过滤让它保持原样清晰。

---

## 6. 已知问题 / 待决策

1. **`filter_list.json` 改动会让全部章节 OCR 失效**（哈希包含过滤规则）。
   这是设计使然，但意味着重跑成本。Love Quest 的 OCR 缓存也因此失效过一次。
2. **Ch74 是西语**（`chapters: "74"` 单独规则）→ 说明 **1–90 全韩文的假设不成立**。
   1–69 章未验证，批量前建议做语言抽检（每章抽 2–3 页 OCR 判定）。
3. **页 026（Ch70）的 `네~♥` 气泡漏检**：`text_threshold 0.45` / `box_threshold 0.65`
   下检不到。降阈值到 0.35/0.55 能找回，但会把更多水印与拟声词捞进来。
   现在有分页拼接 + 过滤规则，副作用已小很多，但**未决策**。
4. **拟声词/SFX 保留策略**：当前阈值下气泡外 SFX（`찌카`/`바싹-`/`오따가자`）保留、
   气泡内被擦除。这是期望行为；若想也擦除 SFX 需降阈值。
5. **`config/filter_list.json` 被 gitignore 排除**（上游设计"用户自定义过滤列表"）。
   想让 fork 克隆即带规则，可加 `config/filter_list.example.json`（未决策）。
6. **拼接会放大 `_work/` 临时目录**：实现里拼接图存成 PNG。
   无拼接时 Ch70 的 `_work/` 是 14.2MB（73 页 JPEG 拷贝）；拼接后若改用 PNG
   可能到数百 MB/章。**待实测**，若过大应改存 JPEG（q=95）。
7. **拼接只在本章内相邻页之间**，不跨章（章末/章首不拼）。
8. **Ch112 页 030 手写体气泡漏检**（치..찢어내...♥）：手写风格完全躲过检测器（该页仅检出 1 个区域），与问题 3（Ch70 네~♥）同类。降阈值可找回但有副作用，未决策。

---

## 7. 教训（避免重蹈）

- **清理脚本必须校验 guard 变量**：曾因 shell 转义把 `active` 取空，
  导致把三套版本连同活跃版本一起删光。正确做法是用 Python 一次写完，
  或删除前断言 `active` 非空。
- 流水线靠 manifest 自愈：`manifest is None` 就重跑，所以删产物是可恢复的
  （但要重算时间）。
- 文档（如 `doc/USER_GUIDE_ZH.md` §14）描述的是另一部作品的情形，
  **引用文档结论前先对着代码验证**（例如"OCR 会和 Qwen 抢显存"对纯 OCR+擦除不成立）。

---

## 8. 常用命令

```powershell
# 切换作品后重建容器
docker compose -f packaging/docker-compose.yml up -d --no-build manga-translator-gpu

# OCR + 擦除（某几章）
docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare `
  --stages ocr inpaint --chapters "Chapter 91" "Chapter 92"

# Git Bash 下给 docker exec 传容器内路径要加 MSYS_NO_PATHCONV=1，
# 否则 /data/... 会被转义成 C:/Program Files/Git/data/...（report_path 曾被写坏）
MSYS_NO_PATHCONV=1 docker exec manga-translator-gpu python -m manga_translator.chapter_pipeline.prepare `
  --stages ocr inpaint --chapters "Chapter 93" --report-path /data/pipeline/reports/batch-a.json

# 查进度（页面） / API
#   http://127.0.0.1:8001/chapters
Invoke-RestMethod "http://127.0.0.1:8001/chapters/jobs"

# 验证过滤规则命中数与对白误伤数（先扫产物再统计）
# 产物目录: <results>/RAW/<Chapter>/.pipeline/ocr/<hash>/*.json

# 恢复上游删除的文件（本仓库 test/ 曾被整目录删过）
git checkout upstream/main -- test/ .gitattributes .editorconfig Unix-*.sh
```

## 9. git 状态

- `origin` = `https://github.com/OHW0nder/manga-translator-ui`（用户的 fork）
- `upstream` = `https://github.com/hgmzhn/manga-translator-ui`（上游）
- 工作分支 `feat/chapter-model-orchestration`（`main` 保持与上游一致）
- **认证**：`credential.helper=manager`，首次 push 会弹浏览器登录，token 存 GCM
- **不要 `git add -A` 之前不检查**：`.env`（含 GEMINI_API_KEY）与 `presets/`
  靠 `.gitignore` 排除；该文件曾被删，若再删必须先恢复

## 10. 批量处理流程

标准流程（试点→四级验收→过滤定稿→分批全量）已固化为长期文档：
**`doc/CHAPTER_PIPELINE.md` 的 "Batch OCR + Erase Workflow" 一节**。本文件只记录
当前进度，不复载流程。
