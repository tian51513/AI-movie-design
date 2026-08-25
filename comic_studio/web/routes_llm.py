# comic_studio/web/routes_llm.py
"""LLM 辅助接口：提示词优化（文本框 ✨ 弹窗，2026-08-25 需求）。"""
from fastapi import APIRouter, HTTPException, Request

from ..engine.llm.provider import client_for_task

router = APIRouter(prefix="/api/llm", tags=["llm"])

# kind → 系统提示（按输入框类型给不同的优化指引）
_KIND_SYSTEMS = {
    "shot_desc": (
        "你是漫剧分镜师。优化这段分镜画面描述：改写为具体、可视化、有镜头感的中文单段描述"
        "（谁、做什么、什么动作/表情、机位景别、光线氛围），保持原意不添加新角色，"
        "直接输出优化后的描述正文，不要任何解释或标题。"),
    "video_prompt": (
        "你是视频生成提示词工程师。润色这段视频提示词：提升画面描述的生动性与镜头语言质量，"
        "但必须保持原有结构、分段、标签与语言约定（如 <d>Chinese</d>、[Shot N] 标记）完全不变，"
        "不新增角色，长度变化不超过 ±30%。直接输出润色后的完整提示词。"),
    "appearance": (
        "你是角色设定师。把这段外貌描述改写为可直接用于绘画参考的固化外貌："
        "性别年龄、发色发型、瞳色、体型、标志性服装与配饰，客观具体不写性格心理，"
        "保留原文所有视觉信息。直接输出改写后的描述。"),
    "generic": (
        "优化这段中文文本：更通顺、具体、生动，保持原意与语言。直接输出优化结果，不要解释。"),
}


@router.post("/optimize")
def optimize(request: Request, body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(422, "text 为空")
    if len(text) > 20000:  # 防巨型请求打爆 LLM（安全扫描建议的合理部分）
        raise HTTPException(422, f"text 过长（{len(text)} 字符，上限 20000）")
    system = _KIND_SYSTEMS.get(body.get("kind") or "generic", _KIND_SYSTEMS["generic"])
    client = client_for_task(request.app.state.db, "optimize_prompt")
    reply, _u = client.raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": text}], temperature=0.4)
    return {"text": (reply or "").strip()}
