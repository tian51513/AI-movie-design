# comic_studio/engine/settings.py
"""settings 表读写。默认值即产品默认行为（spec §2/§8.6/§9.1）。"""
import copy
import json

from .db import Database

DEFAULT_SETTINGS = {
    "workers": 1,
    # 说话人净化追加黑名单（2026-09-07 优化#6）：逗号分隔词，VLM 又造新
    # 脚手架短语时设置页自添免发版
    "speaker_blacklist": "",
    # 类型→模板映射（spec §6.3）；t2v 可选，默认 None
    "template_map": {
        "character_views": "character_views",
        "t2i": "zimage_t2i",
        "keyframe": "xf_zimage_ti2i",  # 关键帧图生图  # 主力：Z-Image Turbo（majicmix 版 t2i_ref 保留可切换）
        "ref2va": "h3_ref2va",
        "fl2v": "h3_fl2v",
        "t2v": "h3_t2v",
        "director": "h3_director",  # P7-D 整段快车道（从视频展示工作流抽离的专属模板）
        "asr": "asr_qwen3",  # P10-D ComfyUI Qwen3-ASR（audio_to_text）
        # 动态漫整页重绘（2026-09-09 建；2026-09-10 真机四修 F2a 换双槽
        # IP-Adapter 模板——单槽 zimage_i2i 主图进不了工作流=角色漂移根因①）
        "page_redraw": "zimage_page_redraw",
        # 小说转漫画逐页 t2i（2026-09-12）：comicgen.gen_comic_page 消费
        "comic_page": "comic_page",
        # 二期参考注入（2026-09-13）：绑定角色镜走人物场景化模板
        "comic_page_ref": "zimage_page_ref",
        # Krea2 快道（2026-09-13 用户实测 20-30s/页·风格原生贴合）
        "comic_page_krea2": "comic_page_krea2",
    },
    "llm_providers": {
        "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama",
                  "model": "qwen3:14b"},
        "online": {"base_url": "", "api_key": "", "model": ""},
    },
    # 任务路由（spec §9.1：轻活本地、重活线上）
    # 服务商动态化（2026-09-17）：本地/线上均可配 N 个连接（键 ^[a-z][a-z0-9_]*$，
    # UI 自动命名 local3/online2…），默认各一；存量库的 local2 经深合并保留
    "asr": {"engine": "faster_whisper", "chunk_seconds": 300},
    "llm_routing": {
        "extract_assets": "local",
        "fix_appearance": "local",
        "split_storyboards": "online",
        "gen_video_prompt": "online",
        "optimize_prompt": "online",
        "gen_story": "online",
        "describe_shot": "local",
        "asr_cleanup": "local",
    },
    "comfy": {"base_url": "http://127.0.0.1:8188", "min_free_vram_gb": 8,
              "merge_xfade": False, "merge_grade": False,  # C6 段间交叉淡化/统一调色（2026-09-01）
              "director_batch_frames": 512,
              "page_ref_denoise": 1.0,  # v1.3 空白画布纯生成（2026-09-13 底图锁死判例后改 1.0）
              "page_ref_lightning": 0.0,  # v1.4 默认关（4步蒸馏画面糊·真机判例）；1.0=草稿加速档
              "page_ref_cfg": 3.5,  # v1.4 质量基线（v4 重绘同款锐利配置；Lightning 开时建议 2.5,
              # 导演台性能开关（2026-08-28 需求）：段间清显存+重预热每镜多几十秒，
              # 12GB 专跑默认关；OOM 时打开。源帧对比图导出默认关。
              "director_clear_vram": False, "director_export_source": False,
              # P7-H 批间首帧接力（2026-08-29）：上批末帧作下批首段起始画面，
              # 介于批内 latent 连贯与硬切之间；失败自动退化为硬切
              "director_batch_relay": True,
              # P7-J 整片混音：TTS 配音（有台词镜）+ SRT 烧录；失败退化纯画面
              "director_mix": True,
              # 无台词镜静音（2026-08-30 杂音封堵）：H3 原声不进成片，代价是丢自然环境声
              "mute_quiet_shots": False,
              # 整页重绘幅度（2026-09-11 v3：线稿 ControlNet 锁结构后=1.0 全幅
              # 生成，构图由 CN strength 保证；调风格化程度改此值意义已变）
              "page_redraw_denoise": 1.0,
              # v7 H3 抽帧道短视频时长（2026-09-12 用户需求：只抽首帧，越短越省）
              # ——1~4 可调；H3 对超短视频的行为以真机为准（不稳就回调 4）
              "page_redraw_h3_duration": 2,
              # H3 SLA 注意力（2026-09-10 用户自定义节点 H3SLAAttention）：五模板
              # LoRA 后最后一环；默认开（0.9=本机验证值/64=音频安全块，节点出厂默认）
              "h3_sla_enabled": True,
              "h3_sla_sparsity": 0.9,
              "h3_sla_block_size": "64"},
    # 工作流模板级参数（2026-09-14 用户：模型切换区各模板各自设）：
    # {模板 id: {steps: N}}——steps 0/缺省=模板内置；作用于无专属步数控制的
    # 路径（genref 主图/参考图、漫画页 Krea2 快道），项目质量档控制的路径不覆盖
    "template_params": {},
    # 工作流模型槽位覆盖（计划5B 任务6）：{模板 id: {label: 文件名}}
    "model_overrides": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：dict 递归合并，标量/列表以 override 为准。"""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def get_setting(db: Database, key: str):
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    row = db.connect().execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return copy.deepcopy(DEFAULT_SETTINGS[key])
    stored = json.loads(row["value_json"])
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, dict) and isinstance(stored, dict):
        return _deep_merge(default, stored)
    return stored


def set_setting(db: Database, key: str, value) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO settings (key, value_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
        (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()
    # 立即 checkpoint 进主库——防 WAL 被清时丢数据（2026-08-29 真机：
    # force_recover 删 WAL → 用户刚保存的设置全丢）
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass


def ensure_comfy_configured(db: Database) -> None:
    """ComfyUI 依赖型任务入队前门禁（2026-09-01 事故复盘：base_url 被存成
    空串 → worker 拿 None client → 36k 任务 AttributeError 批量失败）。"""
    base = str((get_setting(db, "comfy") or {}).get("base_url") or "").strip()
    if not base:
        raise ValueError("ComfyUI 未配置地址（设置页 → 工作流 → ComfyUI base_url 为空）")
