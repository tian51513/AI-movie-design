# 小说转漫画（novel-to-comic）设计定稿

> 2026-09-12 /grill-me 定稿，所有分支已决策
> **状态：已实现**（2026-09-12~13，计划 docs/superpowers/plans/2026-09-12-novel-to-comic.md T1~T7 全完成，e131208→a031e2a；二期：气泡渲染已落地 2026-09-13（Pillow 后处理+样式三参数）；预留多格分镜/角色参考图注入）

## 已定决策

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 项目关系 | **独立项目类型**（第五种，与小说/主题/有声/动态漫并列） |
| 2 | 页面格式 | **一期单格插画**，多格分镜二期 |
| 3 | 对白呈现 | **参数化三选一**：无对白 / 气泡 / 底部字幕条 |
| 4 | 导出格式 | **三种全要**：在线翻页 + PDF + 长图 |
| 5 | 自动页数 | **字数基线 + LLM 微调**（±20%） |
| 6 | 角色一致性 | **可选开关**（默认开=走资产链+参考图；关=跳过资产直接逐页） |
| 7 | 阶段流转 | **复用前半 + 新后半**：created→analyze→assets→storyboard→**comic_ready** |
| 8 | 分镜拆解 | **复用 split_storyboards + 模式分支**（system prompt 按 comic 模式输出场景+人物+对白，不写运镜/声音） |
| 9 | 前端入口 | **创建弹窗第五 tab「📖 漫画」** |
| 10 | 图片 job | **新写 gen_comic_page**（逐页 job，镜像 gen_shot 模式） |
| 11 | 总原则 | **能复用就复用** |

## 创建参数

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 画风 | Krea2 库预览 / 自定义 | 动漫 | 复用现有 73 库+预览面板 |
| 页数 | int | 0=自动 | 0 时按字数基线+LLM 微调 |
| 尺寸 | select | 1024×1536 竖版 | 方形/竖版/横版/SD竖版 |
| 质量档 | select | 标准 | 快速 8 步 / 标准 12 步 / 高质量 20 步 |
| 模板 | select | Krea2 | Krea2（10-15s/页）/ Z-Image turbo（5-8s/页） |
| 对白 | select | 气泡 | 无 / 气泡 / 底部 |
| 角色一致性 | bool | true | 开=资产链+参考图；关=纯文字描述 |

## 管线

```
文本（小说/主题/有声）
  → analyze（复用：角色/场景/道具提取）
  → [可选] gen_refs（角色一致性开时：参考图生成）
  → split_storyboards（复用+模式分支：每镜=一页，输出场景+人物+对白）
  → gen_comic_page（新 job：逐页 t2i + 对白后处理）
  → comic_ready（全部页面完成）
  → 导出（在线浏览 / PDF / 长图）
```

## 数据库变更

- `projects.comic_mode` 新值 `'comic_output'`
- 新列：`dialogue_mode`（TEXT: none/bubble/footer）、`target_pages`（INT, 0=自动）、`image_size`（TEXT）、`quality_tier`（TEXT）
- `shots.workflow_type` 新值 `'comic'`
- 页面文件存 `projects/<slug>/pages/page_NNN.png`（复用现有 pages 约定）

## 新写模块

| 模块 | 文件 | 职责 |
|---|---|---|
| `gen_comic_page` job | `engine/comicgen.py` | 逐页生成：提示词组装+模板提交+对白后处理+落盘 |
| 对白后处理 | `engine/comicgen.py` | 气泡绘制（Pillow）或底部字幕条 |
| PDF 导出 | `engine/comicexport.py` | Pillow 拼接 PDF |
| 长图导出 | `engine/comicexport.py` | ffmpeg tile 纵向拼接 |
| 漫画拆解 prompt | `engine/llm/storyboard.py` | comic 模式 system prompt 分支 |
| autopilot 分支 | `engine/autopilot.py` | comic_output 流程（storyboard_ready→gen pages→comic_ready） |
| 前端 | `frontend/` | 第五 tab + 漫画浏览页 + 导出按钮 |

## 依赖引入

- `Pillow`（气泡绘制+PDF 导出）→ `pyproject.toml [comic]` extra

## 工作量估算

约 3-4 天（含测试）
