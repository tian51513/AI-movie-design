# 音乐风格库来源声明

- `SKILL.md` / `references/` / `templates/` 复制自 **MiniMax-AI/MiniMax-Music3**
  官方仓库 `music-caption-rewriter` skill，commit `91410fb657c007ae57c60df8240f5ece5be089c7`。
- 取自本地 T8 包（comfyui-minimax-h3-prompt-enhancer-T8）的官方冻结副本
  （provenance 见其 official_skills/SOURCE.json）；版权归 MiniMax-AI，
  随上游仓库许可使用。
- 内容：genre-router（18 族路由）+ references/index-*.md（族索引卡表）+
  templates/*.txt（1000 张完整结构化 caption 卡）。
- 本项目消费方式：engine/musicstyles.py 扫描索引 → 音乐库风格选择器 →
  选中卡作为 caption 骨架参考（suggest_caption）。
