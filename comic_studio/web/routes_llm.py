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
        "你是角色设定师。把这段外貌描述改写为固定的行模板（标签逐字使用、每行一项、"
        "直接输出模板本身不解释）：\n性别：\n年龄：\n发色发型：\n瞳色：\n肤色：\n体型：\n服装：\n配饰："
        "（无则写 无）\n客观具体、不写性格心理、保留原文所有视觉信息；"
        "未成年角色只写中性描述，不写身体曲线/肌肤细节。"),
    "appearance:prop": (
        "你是道具设定师。把这段描述改写为可直接用于绘画参考的道具说明："
        "外观形状、材质质感、尺寸比例、颜色纹样、时代与文化风格，"
        "客观具体，不出现人物。直接输出改写后的描述。"),
    "appearance:scene": (
        "你是场景设定师。把这段描述改写为可直接用于绘画参考的场景说明："
        "空间结构、环境元素、光线氛围、色调、时代与文化风格，"
        "客观具体，不出现人物。直接输出改写后的描述。"),
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
    kind = body.get("kind") or "generic"
    # appearance 按资产类型分化（appearance / appearance:prop / appearance:scene）；
    # 未知 appearance:* 变体回退基础角色版
    system = _KIND_SYSTEMS.get(kind)
    if system is None and kind.startswith("appearance:"):
        system = _KIND_SYSTEMS["appearance"]
    if system is None:
        system = _KIND_SYSTEMS["generic"]
    client = client_for_task(request.app.state.db, "optimize_prompt")
    reply, _u = client.raw_chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": text}], temperature=0.4)
    # P7-A 审计：prompt/reply 落 llm_calls（排障回查）
    from ..engine.llm.provider import log_llm_call
    from ..engine.settings import get_setting as _gs
    _providers = _gs(request.app.state.db, "llm_providers")
    _route = _gs(request.app.state.db, "llm_routing").get("optimize_prompt", "?")
    log_llm_call(request.app.state.db, "optimize_prompt", _route,
                 _providers.get(_route, {}).get("model", "?"), _u,
                 prompt=f"[{kind}] {text}", reply=reply)
    return {"text": (reply or "").strip()}
