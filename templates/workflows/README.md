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

模板验收标准（spec §6.2）：注入一句测试提示词能出片。（spec §6.2）：注入一句测试提示词能出片。

## manifest 字段

| 字段 | 说明 |
|---|---|
| id / type / name | 模板标识 / 类型（t2i 等）/ 显示名 |
| file | API JSON 文件名（同目录） |
| prompt_format | 提示词模板，占位 {kind_label} {name} {detail} |
| inject.prompt | {node, field}：文本注入点 |
| inject.params | 参数注入点映射（seed/width/height…） |
| inject.images | 可选图片槽位 [{node, field, slot}] |
| outputs[].filename_prefix | 输出前缀，占位 {project} {asset} |
| requires | 依赖的自定义节点名列表（信息性） |
