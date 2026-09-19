# 工作流模板目录

每个模板 = 一个 YAML manifest + 一个 API 格式工作流 JSON。

## 添加真实模板（用户操作）

1. 启动 ComfyUI Desktop，打开目标工作流（如 小枫-文生图工作流.json）
2. 右上齿轮开启「开发者模式」→ 菜单 Workflow → **Export (API)**（中文界面：工作流 → 导出 API）
3. 保存为 `templates/workflows/<模板id>.api.json`
4. 复制一份 manifest（参考 demo_t2i.yaml），改：
   - id/type/name/file
   - inject.prompt：正向提示词节点的编号和字段（在导出的 JSON 里找 CLIPTextEncode 的 key）
   - inject.params.seed：KSampler 的编号
   - outputs[0]：SaveImage 节点编号
5. 设置页「工作流模板映射」确认 t2i 指向新模板 id（默认映射为 t2i_ref——manifest 的 id 可以就叫 t2i_ref，或任意 id 后在设置页改映射）

模板验收标准（spec §6.2）：注入一句测试提示词能出片。

## 已注册模板库（2026-08-23 由用户导出自动转换）

| 模板 id | 类型 | 来源 | 状态 |
|---|---|---|---|
| zimage_t2i | t2i | z-image文生图 | **主力（已映射 t2i）** |
| zimage_i2i | i2i | 小枫-Z-image[文+图] | 注册未映射（P3 以图重生精化，denoise 0.75） |
| t2i_ref | t2i | 小枫-文生图（majicmix） | 备选——设置页可切回 |
| character_views | character_views | ▶▷MiniMaxH3辅助四视图（2026-09-16 刷新：多 LoRA 栈） | **主力（已映射）**——gen_ref 角色主图派生四视图 |
| h3_ref2va | ref2va | 多参全能生视频（2026-09-18 新链） | **主力（已映射）** |
| h3_fl2v | fl2v | 首尾帧生视频（2026-09-18 新链） | **主力（已映射）** |
| h3_t2v | t2v | 文生视频（2026-09-18 新链） | 可选（已映射） |
| h3_i2v | i2v | 图生视频（首帧）（2026-09-18 新链） | fl2v 缺尾帧降级目标 |
| h3_spectrum_fl2v / _i2v / _t2v / _ref2va | fl2v/i2v/t2v/ref2va | BulletTime/SpectrumSpeed 统一骨架（2026-09-18） | Spectrum 加速道备选——设置页映射切换；RTX VSR 超分开关（comfy.rtx_vsr_enabled，默认关） |
| music3 | music | MiniMaxMusic3 音乐生成（2026-09-19） | 全局音乐库——musiclib 消费；caption/lyrics/max_duration/seed 四注入 |

**H3 视频模板 2026-09-18 换链**：ComfyUI 更新后旧链（MiniMaxLowVRAMAttention/
TESpeedMiniMaxH3/PathchSageAttentionKJ）失效——四个分镜模板按用户修复验证的
「加速版 (new)」更新（`UNET→ModelAttentionBackend→SigmaShift→LazyH3LoraStack
(8槽开关)→MiniMaxChunkFeedForward→H3SLAAttention`，fused turbo8 底模 4 步）；
导演台手动移植同款注意力/FFN 链（不走 Spectrum 链——ChunkFF 长序列分块是
导演台批式长视频的显存安全需要）。Spectrum 加速道默认 BulletTime 组合
（fused 底模），换 SpectrumSpeed 组合在设置页切 unet/clip/vae_video 三槽。

所有入库模板的注入点正提示词已清空（管线运行时注入）；负面提示词与模型配置保留原样。
`_raw/` 中另有 Director 系列（segments_json 接口）、Impact V6 分段循环、提示词增强、多姿势图——不符合 v1 manifest 模型，留作 v2/P3 候选。

## manifest 字段

| 字段 | 说明 |
|---|---|
| id / type / name | 模板标识 / 类型（t2i 等）/ 显示名 |
| file | API JSON 文件名（同目录） |
| prompt_format | 提示词模板，占位 {kind_label} {name} {detail} |
| inject.prompt | {node, field}：文本注入点 |
| inject.params | 参数注入点映射（seed/width/height…） |
| inject.images | 可选图片槽位 [{node, field, slot}] |
| switch_links | 开关接线 `{param键: {node/field/"on"/"off"/add_nodes}}`：params 键真→注入 add_nodes 节点并改接 on 链；假→off 直连（旁路分支默认不存在——API 格式孤立节点也会执行）。注意 yaml 里 `"on"`/`"off"` 必须带引号（YAML 1.1 裸 on/off 解析成布尔键） |
| outputs[].filename_prefix | 输出前缀，占位 {project} {asset} |
| requires | 依赖的自定义节点名列表（信息性） |
