# PERF: 章节流水线 OCR 阶段提速（2026-09-18）

> 本文记录一次针对 chapter pipeline OCR 阶段的性能优化：如何发现瓶颈、改了什么、
> 怎么测试和验收。供日后与上游合并时理解这几处改动的来龙去脉。
> 关联：`doc/CHAPTER_PIPELINE.md`（Batch OCR + Erase Workflow）、`doc/HANDOVER.md`。

## 0. 背景与症状

Wireless Onahole 91+ 为 720×10000 左右的西语长条（韩文章仅 720×2500）。
实测同一流水线：韩文章 OCR ≈ 3.7 s/页，长条章 ≈ 19 s/页——**超出像素面积比例
（4 倍面积应 ≈ 15 s/页）尚可解释，但整批任务的墙钟中模型计算只占少数**，
于是立项做一次系统性的瓶颈定位。

## 1. 发现过程（分层探针 → cProfile）

`version_hash` 不含代码版本，且流水线日志只有模型调用进度条，没有耗时分解，
所以采用"逐层计时 + 最终 cProfile 定案"的方法：

1. **第一层（模型调用）**：monkey-patch `DefaultDetector._infer`、
   `ModelPaddleOCR._infer`、`MangaLensBubbleDetector.detect`，单章 14 页跑一遍。
   结果：模型合计仅 ~57 s / 墙钟 268 s（21%）→ 瓶颈在模型之外。
2. **第二层（蒙版/拼接）**：补包 `mask_refinement.dispatch`、
   `_complete_mask_with_det_rearrange`、`_complete_mask_core`、`refine_mask`、
   `stitch_boundary_image`、`remap_stitched_payload`。
   结果：蒙版精修 21 s、拼接 6 s → 仍有 ~200 s 无归属。
3. **第三层（stage 级）**：包 `MangaTranslator._run_detection / _run_ocr /
   _run_textline_merge / _run_mask_refinement / translate_batch`。
   结果：`_run_textline_merge` 单独 47 s（>识别本身的 46 s）；
   `translate_batch` 之外还有 ~113 s。
4. **定案（cProfile）**：`python -m cProfile` 全程采样，按 cumulative 排序，
   两个真凶浮出：
   - `get_panels_from_array`（kumikolib 分格分析，由 `_run_textline_merge` →
     `sort_regions` 调用）：**128 s（46%）**，其中 `union_all` 内
     `Segment.__eq__` 被调用 **1.7 亿次**（~50 s 纯自耗时间）。
   - `scan_inventory`：每次任务启动对全库 4112 页**重算 sha256**，两次调用共
     **~87 s 固定成本**（Windows bind-mount IO 放大了读盘代价）。

方法论备注：
- py-spy 因容器缺 `SYS_PTRACE` 不可用，cProfile 是最终定案工具。
- monkey-patch 容易踩"导入时绑定"的坑（`from x import y` 后再改 `x.y` 不生效），
  patch 必须打在实际查找名字的命名空间上。

## 2. 修改内容

### 2.1 极端长宽比跳过分格分析（`utils/textblock.py`）

`sort_regions()` 在 `force_simple_sort` 检查之后新增通用门控：图像
`height/width >= PANEL_SORT_MAX_ASPECT_RATIO`（常数 3.0）时直接走
`_simple_sort`。

- **为什么安全**：webtoon 长条是单列内容，分格信息对阅读顺序没有增益，
  自上而下简单排序即正确语义；普通漫画页（长宽比 < 3）路径完全不变。
- **为什么是代码内常数而不是系列配置**：长宽比是图像自身的属性，不是作品偏好，
  对任何作品都成立（符合本项目"作品差异走配置、普适行为走代码"的划分）。
- 原有的 60 s 线程池超时兜底保留不变（对超大普通页仍生效）。

### 2.2 inventory 扫描哈希缓存（`chapter_pipeline/{models,inventory,storage,pipeline}.py`）

- `models.PageInventory` 增加 `source_mtime_ns` 字段（默认 0）。
- `scan_inventory(..., hash_cache=None)`：`hash_cache` 为
  `relative_path -> (size_bytes, mtime_ns, sha256)` 映射。size 与 mtime_ns
  都匹配时复用缓存的 sha256，否则重算并写回缓存。
- `storage.py`：
  - `pages` 表新增 `source_mtime_ns INTEGER NOT NULL DEFAULT 0`（新库直接建，
    旧库由 `_migrate_schema()` 的 `PRAGMA table_info` 检查 + `ALTER TABLE`
    原地迁移，无 schema version 机制）；
  - `upsert_inventory` 写入 mtime；
  - 新增 `page_hash_cache(series_slug, series_name)` 供扫描前取缓存。
- `pipeline.scan()`：`include_hashes=True` 时从 DB 取缓存传给扫描。
  DB 里已有全库 4112 页记录，但旧记录 mtime=0（不能证明文件未变），
  因此**第一次扫描仍会全量哈希并回填 mtime，之后每次任务启动扫描降到秒级**。

**正确性论证**：缓存键含 size + mtime_ns，文件内容变化必然引起 mtime 变化
（写入即更新 mtime）；`sha256` 语义与之前完全一致，只是计算被跳过。
哈希进入 OCR 的 `input_hash` → 版本失效链路不受影响。

## 3. 测试与验收

单章 Chapter 93（14 页西语长条）做前后对照，共 5 轮：

| 轮次 | 内容 | 墙钟 |
| --- | --- | --- |
| 基线（探针版，多次） | 优化前 OCR-only --no-publish | 268–294 s |
| cProfile 定案 | 同上 + 采样开销 | 365 s（仅供参考排序） |
| 优化后 #1（冷缓存） | 首次扫描全量哈希回填 mtime | **218 s** |
| 优化后 #2（热缓存） | 扫描全命中缓存 | **172 s** |

验收判据与结果：

1. **质量等价**：区域数 157 = 157；区域文本多重集逐一相同（`sorted(texts)`
   完全一致）。蒙版精修代码未动，擦除不受影响。
2. **行为变化（有意）**：webtoon 长条的区域顺序从"分格序"变为"自上而下简单序"，
   即阅读顺序，属预期改善；普通漫画页顺序不变。
3. **计数**：`publish_errors: []`，14/14 页完成。
4. **迁移**：旧库启动即自动补列，无需手工操作。

## 4. 预期收益（91+ 全量批 93–119，约 530 页）

- 每章省 ~9 s 分格分析 → 530 页省 ~80 min 中的一部分（多章并行时按墙钟折算 ~15–25 min）；
- 每次任务启动省 ~80 s 扫描（冷启动首跑除外）；
- 对后续 70–90 重跑、以及任何 webtoon 作品同样生效。

## 5. 合并注意事项（给未来翻 git 历史的人）

- `textblock.py` 的改动在 `sort_regions` 内，上游若重写过该函数（尤其分格排序
  相关），需人工核对门控是否仍成立；
- `storage.py` 的 `_migrate_schema` 是本仓库私有机制（上游无 schema version），
  上游若改 `pages` 表结构，注意保留该迁移；
- `inventory/pipeline` 的 `hash_cache` 参数为新增可选参数，向后兼容；
- 报告撰写时的基准数据来自 Wireless Onahole 库（`D:\Resources\Download\manga-dl`），
  容器 `manga-translator-gpu`，GPU 为 CUDA 设备。
