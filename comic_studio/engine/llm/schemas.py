"""LLM 分析输出的契约（spec §9.1：输出强制 JSON schema 校验）。"""
from pydantic import BaseModel, Field, model_validator


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


class AssetMerge(BaseModel):
    """LLM 资产查重的单组合并指令（2026-09-20：keep 保留、drop 并入）。
    宽进（2026-09-20 真机判例：nsfwvision 输出 13 组里 5 组 drop=[] 曾把整单
    校验炸掉三次全丢）：drop 接受 str|[str]、空条目入模型前机械剔除——引擎层
    本就跳过清单外名称，形状容错优先于严格校验。"""
    kind: str = ""
    keep: str = ""
    drop: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _loosen(cls, data):
        if isinstance(data, dict):
            d = dict(data)
            drop = d.get("drop", [])
            if isinstance(drop, str):
                drop = [drop]
            if isinstance(drop, list):
                drop = [str(x).strip() for x in drop if str(x).strip()]
            else:
                drop = []
            d["drop"] = drop
            d["keep"] = str(d.get("keep") or "").strip()
            d["kind"] = str(d.get("kind") or "").strip()
            return d
        return data


class AssetDedup(BaseModel):
    merges: list[AssetMerge] = []

    @model_validator(mode="before")
    @classmethod
    def _drop_empty(cls, data):
        """drop/keep 为空的条目整条剔除（不让一颗老鼠屎毒死整单）。"""
        if isinstance(data, dict):
            ms = data.get("merges")
            if isinstance(ms, list):
                data = dict(data)
                data["merges"] = [m for m in ms
                                  if isinstance(m, dict)
                                  and str(m.get("keep") or "").strip()
                                  and (isinstance(m.get("drop"), (str, list))
                                       and (m.get("drop") if isinstance(m.get("drop"), str)
                                            else [x for x in (m.get("drop") or [])
                                                  if str(x).strip()]))]
        return data
