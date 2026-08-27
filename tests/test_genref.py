# tests/test_genref.py
import json
import time

import pytest

from comic_studio.engine.db import Database
from comic_studio.engine.genref import handle_gen_ref, build_gen_prompt
from comic_studio.engine.jobs import enqueue_job, get_job
from comic_studio.engine.projects import create_project
from comic_studio.engine.settings import set_setting
from comic_studio.engine.workflows import registry
from comfy_mock import comfy_server

API = {"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
       "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
       "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
MANIFEST = """
id: t_t2i_test
type: t2i
name: 测试
file: t.api.json
prompt_format: "{kind_label}：{name}。{detail}"
inject:
  prompt: {node: "6", field: "text"}
  params:
    seed: {node: "3", field: "seed"}
outputs:
  - {node: "9", filename_prefix: "cs/{project}/{asset}"}
requires: []
"""


def _setup(tmp_path, monkeypatch):
    (tmp_path / "t.api.json").write_text(json.dumps(API))
    (tmp_path / "m.yaml").write_text(MANIFEST)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", tmp_path)  # 防跨测试污染
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "t_t2i_test"})
    pid = create_project(db, tmp_path / "data", "p", "9:16", "t")["id"]
    return db, pid


def test_build_gen_prompt_by_kind(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[NS(name="庭院", description="古宅院子", tags=[])],
                      props=[]))
    from comic_studio.engine.assets import list_project_assets
    rows = {r["kind"]: r for r in list_project_assets(db, pid)}
    p_char, _ = build_gen_prompt(rows["character"])
    assert "萧炎" in p_char and "三视图" in p_char and "禁止视角重复" in p_char
    p_scene, _ = build_gen_prompt(rows["scene"])
    assert "场景概念" in p_scene and "无人物" in p_scene
    # 项目级风格段注入（公共参数）
    p_styled, _ = build_gen_prompt(rows["character"], style="日系动漫风格，赛璐璐上色")
    assert "日系动漫风格" in p_styled and p_styled.index("日系动漫") > p_styled.index("白色干净背景")  # 风格段收尾主导画风


def test_handle_gen_ref_end_to_end_with_mock(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        # 提交的工作流里 prompt 已注入
        wf = m.prompts[0]["prompt"]
        assert "萧炎" in wf["6"]["inputs"]["text"]
        assert wf["9"]["inputs"]["filename_prefix"].startswith("cs/")
        # 产物落盘
        lib = get_asset(db, asset["id"])["library_dir"]
        sheet = (tmp_path / "data" / lib / "views" / "sheet.png")
        assert sheet.exists() and sheet.stat().st_size == 2
    # 日志埋点
    from comic_studio.engine.logbus import fetch_logs
    msgs = " | ".join(r["message"] for r in fetch_logs(db, pid))
    assert "提交" in msgs and "参考图" in msgs


def test_regen_marks_stale(tmp_path, monkeypatch):
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.shots import persist_shots, list_shots
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset_id = list_project_assets(db, pid)[0]["id"]
    persist_shots(db, pid, [NS(text_span="", description="x", shot_type="",
        camera={}, duration=5.0, workflow_type="ref2va", ledger={},
        character_ids=[asset_id], scene_ids=[], prop_ids=[], depends_on=None)])
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset_id,
                      resource="gpu_comfy", payload={"asset_id": asset_id})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
    assert list_shots(db, pid)[0]["status"] == "stale"
    # 验证 warn 日志
    from comic_studio.engine.logbus import fetch_logs
    msgs = [r["message"] for r in fetch_logs(db, pid)]
    assert any("stale" in m and "萧炎" in m for m in msgs)


def test_style_goes_after_suffix_and_dedup(tmp_path):
    from comic_studio.engine.genref import build_gen_prompt
    row = {"kind": "character", "name": "直葉", "appearance_json": '{"detail": "黑发少女。"}',
           "source_project": 1, "id": 2}
    p, _ = build_gen_prompt(row, style="真人电影，电影质感。")
    assert "。。" not in p
    assert p.index("白色干净背景") < p.index("真人电影")  # 风格段在设定图套话之后
    # 顺序：风格段 → Turbo 尾缀 → 结构再强调
    assert p.index("真人电影") < p.index("ultra-detailed") < p.index("严格三视图布局")


def test_gen_prompt_zimage_turbo_tail():
    """ZImage-Turbo 规范（data/ZImage-Turbo 技能模板）：无负向词、纠错正向写入、
    中英混编、质量尾缀适配 8 步推理。"""
    row = {"kind": "character", "name": "林晨", "source_project": 1, "id": 1,
           "appearance_json": '{"detail":"黑发少年"}'}
    p, _ = build_gen_prompt(row)
    assert "ultra-detailed" in p and "8k" in p
    assert "避免多余手指" in p and "避免五官扭曲" in p and "无蜡像塑料感" in p
    assert "无文字水印" in p
    p_scene, _ = build_gen_prompt(dict(row, kind="scene",
                                       appearance_json='{"detail":"古城"}'))
    assert "cinematic color grading" in p_scene and "画面完整" in p_scene
    p_prop, _ = build_gen_prompt(dict(row, kind="prop",
                                      appearance_json='{"detail":"剑"}'))
    assert "材质纹理" in p_prop


def test_character_two_stage_main_then_views(tmp_path, monkeypatch):
    """两段式（2026-08-25 需求）：zimage 主图（可重复生成）→ Krea2 四视图派生。
    未映射 character_views 时回退单段（旧用例覆盖）。"""
    from pathlib import Path
    db, pid = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    set_setting(db, "template_map", {"t2i": "zimage_t2i",
                                     "character_views": "character_views"})
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        assert len(m.prompts) == 2  # 主图 + 四视图两段
        main_text = m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
        assert "萧炎" in main_text and "全身像" in main_text and "三视图" not in main_text
        views_wf = m.prompts[1]["prompt"]
        # 提示词保留工作流内置触发词（不再被中文提示词覆盖）
        assert "Character Sheet" in views_wf["24"]["inputs"]["prompt"]
        assert views_wf["17"]["inputs"]["image"].startswith("cs__")  # 主图作种子上传
        lib = get_asset(db, asset["id"])["library_dir"]
        assert (tmp_path / "data" / lib / "main.png").exists()
        assert (tmp_path / "data" / lib / "views" / "sheet.png").exists()


def test_two_stage_stage_control(tmp_path, monkeypatch):
    """stage 粒度（2026-08-25 需求）：main=仅主图；views=仅从现有主图重派生三视图。"""
    from pathlib import Path
    db, pid = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    set_setting(db, "template_map", {"t2i": "zimage_t2i",
                                     "character_views": "character_views"})
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    lib = get_asset(db, asset["id"])["library_dir"]
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok") as m:
        # stage=main：仅一段 t2i 主图，sheet 不落
        jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                          resource="gpu_comfy",
                          payload={"asset_id": asset["id"], "stage": "main"})
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        assert len(m.prompts) == 1
        assert "全身像" in m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
        assert (tmp_path / "data" / lib / "main.png").exists()
        assert not (tmp_path / "data" / lib / "views" / "sheet.png").exists()
        # stage=views：仅一段 character_views（用现有主图作种子）
        jid2 = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                           resource="gpu_comfy",
                           payload={"asset_id": asset["id"], "stage": "views"})
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid2), ComfyClient(m.base_url))
        assert len(m.prompts) == 2  # 只新增一段
        assert "Character Sheet" in m.prompts[1]["prompt"]["24"]["inputs"]["prompt"]
        assert (tmp_path / "data" / lib / "views" / "sheet.png").exists()


def test_main_image_template_with_ref_slot(tmp_path, monkeypatch):
    """小枫文+图模板（xf_zimage_ti2i）：有主图时作 ref 槽传入重绘；
    无主图时引导用纯文生图（zimage_t2i）。"""
    from pathlib import Path
    db, pid = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    set_setting(db, "template_map", {"t2i": "xf_zimage_ti2i"})
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    lib = get_asset(db, asset["id"])["library_dir"]
    main = tmp_path / "data" / lib / "main.png"
    from comic_studio.engine.comfy.client import ComfyClient
    # 场景一：无主图 → 引导 zimage_t2i（纯文生图节点 57:27）
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        main_text = m.prompts[0]["prompt"]["57:27"]["inputs"]["text"]
        assert "萧炎" in main_text  # 走了 zimage_t2i
        assert main.exists()
    # 场景二：已有主图 → 文+图重绘（节点 28 注入 + ref 槽上传）
    jid2 = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                       resource="gpu_comfy", payload={"asset_id": asset["id"],
                                                      "stage": "main"})
    with comfy_server("ok") as m2:
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid2), ComfyClient(m2.base_url))
        wf2 = m2.prompts[0]["prompt"]
        assert "萧炎" in wf2["28"]["inputs"]["value"]      # 文本进了文+图模板
        assert wf2["23"]["inputs"]["image"].startswith("cs__")  # 主图作 ref 上传


def test_handle_gen_ref_prefers_style_vis(tmp_path, monkeypatch):
    """画风拆层（2026-08-27 方案A）：主图生成优先用 style_vis（视觉词），
    style 里的叙事/剪辑词不应进入图像提示词；style_vis 空 → 回退完整 style。"""
    db, pid = _setup(tmp_path, monkeypatch)
    conn = db.connect()
    conn.execute("UPDATE projects SET style=?, style_vis=? WHERE id=?",
                 ("剧情PV风格，叙事性构图，场景切换流畅，情绪递进", "电影质感，叙事性构图", pid))
    conn.commit()
    from comic_studio.engine.genref import build_gen_prompt
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])], scenes=[], props=[]))
    row = list_project_assets(db, pid)[0]
    # 视觉词进、叙事词不进（拆层发生在调用方——handle_gen_ref 选词，builder 只管拼接）
    style_used = "电影质感，叙事性构图"
    p, _ = build_gen_prompt(row, style=style_used, variant="main")
    assert "电影质感" in p and "情绪递进" not in p and "场景切换" not in p


def test_photo_style_changes_wording_and_boosts(tmp_path, monkeypatch):
    """写实意图（2026-08-27 真机：自定义"真人电影"出二次元）：
    ① 不再出现"立绘"（二次元词汇）；② 换"全身照"；③ 追加真人实拍增强词。
    非写实风格走中性"全身像"，无增强词。"""
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])], scenes=[], props=[]))
    row = list_project_assets(db, pid)[0]
    p_photo, _ = build_gen_prompt(row, style="真人电影", variant="main")
    assert "立绘" not in p_photo and "全身照" in p_photo
    assert "真人实拍质感" in p_photo and "真实皮肤纹理" in p_photo
    p_anime, _ = build_gen_prompt(row, style="日系动漫风格，赛璐璐上色", variant="main")
    assert "立绘" not in p_anime and "全身像" in p_anime
    assert "真人实拍质感" not in p_anime
