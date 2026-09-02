"""LLM 分析输出的契约（spec §9.1：输出强制 JSON schema 校验）。"""
from pydantic import BaseModel, Field


class CharacterAsset(BaseModel):
    name: str = Field(min_length=1)
    role: str = ""
    appearance: str = Field(min_length=1)  # 外貌固化描述：可视化为后续参考图生成服务
    tags: list[str] = []
    # 15 预设之一（音色自动匹配；非法值忽略，走性别×年龄基线兜底）
    suggested_voice: str = ""
    # 库内音色均不合适时的声线描述（2026-09-02 音色系统）：据此 VoiceDesign
    # 生成项目级音色并绑定。仅给有台词的说话角色——没台词不填（防空耗）。
    voice_description: str = ""


class SceneAsset(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = []


class PropAsset(BaseModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = []


class AssetsAnalysis(BaseModel):
    characters: list[CharacterAsset]
    scenes: list[SceneAsset]
    props: list[PropAsset]
