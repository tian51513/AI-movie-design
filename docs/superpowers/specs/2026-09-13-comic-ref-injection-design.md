# 漫画页角色参考图注入（二期）设计定稿

> 2026-09-13 brainstorming 定稿；一期（novel-to-comic T1~T7 + 气泡 + B1 停等 + 文本锚）已落地

## 决策表

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 模板路线 | **新模板 `zimage_page_ref`**：复用 v4 重绘的 Qwen-Image-Edit-2511 链，把「编辑原页」换成**人物场景化**（Edit 强项）；不做 t2i+IPAdapter（Z-Image/NextDiT 不支持 easy IPAdapter） |
| 2 | 单角色镜 | base 槽=角色主图（Edit 图条件）+ 指令「将此人置于{场景}，{动作}」——保人一致性最强 |
| 3 | 双角色镜 | base=第一角色主图 + IPAdapter PLUS 0.7 注入第二角色（v4 rembg+apply 链） |
| 4 | 无绑定 / >2 角色 | 退 `comic_page` 纯 t2i + 文字锚（Krea2 多人镜判例：槽不够硬塞搅浑） |
| 5 | 主图生成时机 | 确认资产后自动生成缺主图（`gen_ref stage="main"` 每角色 ~15s，现成管线） |
| 6 | 主图停等 | **停等检查**（重绘链判例）：主图齐 → 停等「检查满意后 ✓ 主图满意，继续出片」（迁移 39 `comic_refs_done`） |
| 7 | 存量项目 | 「🔄 全部重出」即生效（分流读现状：绑定+主图→注入），无迁移负担 |
| 8 | 可调参数 | IPAdapter weight / denoise 参数化注入（真机调优不改模板） |

## 链路（B1 扩展）

```
analyzed → 停等确认资产（B1 已有）→ 确认后：
  缺主图角色 → 入队 gen_ref(stage="main")（在飞 wait/失败守卫同批型）
  主图齐 + comic_refs_done=0 → 停等「请检查角色主图（可重生/上传替换），满意后点 ✓ 主图满意，继续出片」
  comic_refs_done=1 → assets_ready → 拆解 → 逐页分流：
    绑定角色 ≤2 且有 main.png → zimage_page_ref（base=主图[+IPAdapter 第二角色]）
    其余 → comic_page 纯 t2i（文字锚保留——图+文双锚）
```

无角色资产的项目：确认后直通 assets_ready（无主图可检）。

## 组件

- `templates/workflows/zimage_page_ref.{yaml,api.json}`：从 v4 改造——VAEEncode(原页) 槽语义变为「角色主图」；去重绘专用指令；requires ComfyUI-Easy-Use + comfyui-rembg（v4 同款，已装）
- `engine/comicgen.py`：`handle_gen_comic_page` 分流（绑定角色→参考模板+images 上传；模板缺失 warn 退纯 t2i）；`build_comic_prompt` 场景化指令变体（base=人物时「将此人置于…」措辞）
- `engine/autopilot.py`：analyzed comic 分支扩主图阶段（gen_ref 入队/停等检查/comic_refs_done 放行）
- 迁移 39 `projects.comic_refs_done`；`POST /api/projects/{id}/confirm-comic-refs`（同 confirm-comic-assets 模式）
- settings `template_map.comic_page_ref`（默认 zimage_page_ref，白名单+设置页可换）
- 前端：资产区「✓ 主图满意，继续出片」按钮（主图重生/上传已有入口）

## 测试

模板 manifest 注册（注入点/槽位/参数齐备）；handler 分流（comfy_mock 断言模板选择+上传列表）；>2 角色退纯 t2i；模板缺失 warn 退；autopilot 主图阶段三分支；confirm-comic-refs API；Playwright 停等按钮。

## 真机验证项（用户侧）

Edit-2511 人物场景化效果；双角色 IPAdapter 叠加质量；weight/denoise 手感（设置页模板参数）；主图规格（全身像利于场景化）。

## 后续（本 spec 之外）

开发完成后：盘点本机 ComfyUI 模型/节点/工作流，评估更快且一致性更好的漫画生成方案（角色场景一致性 + 连续性 + 耗时）——独立分析报告。
