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


def test_condense_appearance_drops_none_and_strengthens_gender():
    """行模板→自然语言（2026-08-27 真机：majicmix 下男性角色变女性）：
    「无」值行不进提示词（对 CLIP 是噪声）；性别转强词（含英文锚，CLIP 对
    male/man token 敏感）；年龄/发型/服装并成自然短句。自由文本原样保留。"""
    from comic_studio.engine.genref import condense_appearance
    detail = ("性别：男\n年龄：17\n发色发型：黑色短发\n瞳色：无\n肤色：无\n"
              "体型：无\n服装：校服衬衫，黑色长裤\n配饰：无")
    out = condense_appearance(detail)
    assert "无" not in out and "瞳色" not in out and "配饰" not in out
    assert "男性" in out and "17岁" in out and "黑色短发" in out
    assert "校服衬衫" in out and "man" in out.lower()
    # 女性分支
    out_f = condense_appearance(detail.replace("性别：男", "性别：女"))
    assert "女性" in out_f and "woman" in out_f.lower()
    # 自由文本（非行模板）原样返回
    assert condense_appearance("黑发少年，穿校服") == "黑发少年，穿校服"


def test_tags_en_prompt_for_sd_clip(tmp_path, monkeypatch):
    """模板级提示词方言（2026-08-27 真机：t2i_ref 是 SD 系 CLIP 读不懂中文，
    中文提示词=噪声 → 输出连人都不是）。tags_en 模板走英文标签流组装。"""
    from comic_studio.engine.genref import build_gen_prompt_tags_en
    row = {"kind": "character", "name": "主角", "source_project": 1, "id": 1,
           "appearance_json": json.dumps({"detail": "性别：男\n年龄：17\n发色发型：黑色短发\n"
                                        "瞳色：无\n服装：校服衬衫，黑色长裤\n配饰：无"},
                                        ensure_ascii=False)}
    p, _ = build_gen_prompt_tags_en(row, style="真人电影质感，写实摄影", variant="main")
    assert "1boy" in p and "black" in p and "short hair" in p
    assert "17" in p and "full body" in p and "white background" in p
    assert "photorealistic" in p and "35mm" in p and "8k" in p
    assert "避免畸形" not in p and "无" not in p  # 中文纠错尾缀/空值行不进 tags_en
    # 女性分支 + 非写实风格原样附加
    row_f = dict(row, appearance_json=json.dumps(
        {"detail": "性别：女\n发色发型：金色长发\n服装：白裙\n瞳色：无"}, ensure_ascii=False))
    p_f, _ = build_gen_prompt_tags_en(row_f, style="日系动漫风格")
    assert "1girl" in p_f and "blonde" in p_f and "long hair" in p_f
    assert "photorealistic" not in p_f and "日系动漫风格" in p_f


def test_manifest_prompt_style_parsed(tmp_path):
    """registry 解析 prompt_style（默认 natural_zh；tags_en 声明生效）。"""
    from comic_studio.engine.workflows.registry import load_manifest
    (tmp_path / "a.api.json").write_text("{}")
    (tmp_path / "a.yaml").write_text(
        "id: t_a\ntype: t2i\nname: a\nfile: a.api.json\nprompt_format: x\n"
        "inject: {prompt: {node: '1', field: text}}\noutputs: [{node: '2', filename_prefix: cs}]\n")
    (tmp_path / "b.yaml").write_text(
        "id: t_b\ntype: t2i\nname: b\nfile: a.api.json\nprompt_format: x\n"
        "prompt_style: tags_en\n"
        "inject: {prompt: {node: '1', field: text}}\noutputs: [{node: '2', filename_prefix: cs}]\n")
    assert load_manifest(tmp_path / "a.yaml").prompt_style == "natural_zh"
    assert load_manifest(tmp_path / "b.yaml").prompt_style == "tags_en"


def test_handle_gen_ref_dispatches_by_prompt_style(tmp_path, monkeypatch):
    """tags_en 模板的主图提交文本是英文标签流。"""
    db, pid = _setup(tmp_path, monkeypatch)
    set_setting(db, "template_map", {"t2i": "t_t2i_test"})
    # 重写 manifest 加 tags_en
    (tmp_path / "m.yaml").write_text(MANIFEST + "\nprompt_style: tags_en\n")
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets, get_asset
    from comic_studio.engine.jobs import get_job
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="性别：男\n发色发型：黑色短发\n服装：黑西装\n瞳色：无", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        text = m.prompts[0]["prompt"]["6"]["inputs"]["text"]
        assert "1boy" in text and "black" in text and "萧炎" not in text.split(",")[0]


def test_gen_ref_attaches_audit_snapshot(tmp_path, monkeypatch):
    """P7-A：参考图任务提交时落审计快照（实际注入的提示词+工作流）。"""
    db, pid = _setup(tmp_path, monkeypatch)
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.jobs import enqueue_job, get_job
    from types import SimpleNamespace as NS
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="性别：男\n发色发型：黑色短发\n瞳色：无", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                      resource="gpu_comfy", payload={"asset_id": asset["id"]})
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
    import json as _json
    snap = _json.loads(get_job(db, jid)["snapshot_json"])
    assert "萧炎" in snap["prompt"] and snap["template"] == "t_t2i_test"
    assert snap["workflow"]["6"]["inputs"]["text"] == snap["prompt"]


def test_prop_scene_forbid_persons_double():
    """2026-08-28 真机：道具重试数次仍含人物（旧 suffix 零禁令）、场景多次后
    勉强达标——强禁令双重强调（suffix+尾缀），道具再叠加「产品静物摄影」框架
    压制人物先验。"""
    from comic_studio.engine.genref import build_gen_prompt
    row = {"kind": "prop", "name": "眼镜", "source_project": 1, "id": 3,
           "appearance_json": '{"detail":"金丝圆框眼镜"}'}
    p = build_gen_prompt(row)[0]
    assert "产品静物摄影" in p
    assert p.count("无人物") + p.count("禁止出现任何人物") >= 2
    s = build_gen_prompt(dict(row, kind="scene",
                              appearance_json='{"detail":"学校保健室"}'))[0]
    assert s.count("无人物") + s.count("禁止出现任何人物") >= 2


def test_condense_appearance_keeps_labels_for_body_fields():
    """剩余字段保留「标签：值」（2026-09-13 真机判例：体型调高挑后主图仍偏胖
    ——裸值「前凸后翘，身材高挑，大胸」夹在肤色后对模型是弱约束噪声，中文流
    标签即语义锚）。性别/年龄/发型/服装仍并自然短句。"""
    from comic_studio.engine.genref import condense_appearance
    detail = ("性别：女\n年龄：36\n发色发型：黑色短发\n瞳色：深褐色\n"
              "肤色：白皙\n体型：身材高挑，纤瘦\n服装：白裙\n配饰：无")
    out = condense_appearance(detail)
    assert "体型：身材高挑，纤瘦" in out      # 标签保留=语义锚
    assert "瞳色：深褐色" in out and "肤色：白皙" in out
    assert "36岁" in out and "女性" in out    # 自然短句部分不变
    assert "配饰" not in out                  # 无值行仍丢


def test_redesign_ignores_old_main_reference(tmp_path, monkeypatch):
    """🎨 重设计（2026-09-20 用户判例：换画风后重生主图每次都差不多——带图槽
    模板把旧 main.png 作 char 参考重绘，旧图锚死构图与人脸）。redesign=1 →
    有旧主图也强制走纯文生图（zimage_t2i），旧图不上传。"""
    from pathlib import Path
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.paths import data_to_abs
    db, pid = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", Path("templates/workflows"))
    set_setting(db, "template_map", {"t2i": "comic_page_krea2"})  # 带图槽模板
    persist_assets(db, tmp_path / "data", pid,
                   NS(characters=[NS(name="萧炎", appearance="黑发少年", tags=[])],
                      scenes=[], props=[]))
    asset = list_project_assets(db, pid)[0]
    main = data_to_abs(tmp_path / "data", asset["library_dir"]) / "main.png"
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_bytes(b"\x89PNG\r\n\x1a\nold")  # 旧主图在场
    with comfy_server("ok") as m:
        from comic_studio.engine.comfy.client import ComfyClient
        # 不带 redesign：旧主图作参考上传（Krea2 工作台链）
        jid = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                          resource="gpu_comfy",
                          payload={"asset_id": asset["id"], "stage": "main"})
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid), ComfyClient(m.base_url))
        wf = m.prompts[0]["prompt"]
        assert "2001" in wf  # comic_page_krea2 Generate 节点签名
        assert any(f"cs__p{pid}" in u for u in m.uploads)
        # redesign：走 zimage_t2i 纯文生图，旧图不再上传
        m.uploads.clear(); m.prompts.clear()
        jid2 = enqueue_job(db, "gen_ref", project_id=pid, asset_id=asset["id"],
                           resource="gpu_comfy",
                           payload={"asset_id": asset["id"], "stage": "main",
                                    "redesign": True})
        handle_gen_ref(db, tmp_path / "data", get_job(db, jid2), ComfyClient(m.base_url))
        wf2 = m.prompts[0]["prompt"]
        assert "57:27" in wf2 and "萧炎" in wf2["57:27"]["inputs"]["text"]  # zimage_t2i
        assert not any(f"cs__p{pid}" in u for u in m.uploads)


def test_heal_image_prompt():
    """机械 heal（官方 validate_prompt.py 移植，仿 heal_h3_prompt 判例）：
    换行折叠单段、官方禁词剥离（中英）、引号不平衡判废（None=调用方回落）、
    代码围栏剥离。纯函数零 LLM。"""
    from comic_studio.engine.genref import heal_image_prompt
    # 换行折叠为单段（官方规范：单段连续无换行）
    assert heal_image_prompt("第一段。\n第二段。\r\n第三段。") == "第一段。 第二段。 第三段。"
    # 禁词剥离：英文边界匹配 + 中文相邻可命中（画质4K细腻）+ 中文禁词
    out = heal_image_prompt("a masterpiece photo in 8K, 画质4K细腻，旷世杰作")
    for bad in ("masterpiece", "8K", "4K", "杰作"):
        assert bad not in out
    assert "photo" in out and "画质" in out and "细腻" in out
    # 引号不平衡 → None（扩写输出判废，调用方回落原文）
    assert heal_image_prompt('他说"你好') is None
    # 引号平衡不受影响
    assert heal_image_prompt('招牌"开业大酬宾"醒目') == '招牌"开业大酬宾"醒目'
    # 代码围栏剥离（小模型常见毛病，gen.py _strip_code_blocks 同款）
    assert heal_image_prompt("```\n提示词正文\n```") == "提示词正文"
    # 多余空白收敛
    assert heal_image_prompt("词  词   词") == "词 词 词"


def test_expand_image_prompt_modes(tmp_path, monkeypatch):
    """官方扩写规范接入：mode 路由三套系统词（t2i-zh 默认 / t2i-en 随设置
    comfy.prompt_expand_lang / edit 恒中文不受语言旋钮影响）。FakeClient
    捕获 system 词断言路由正确。"""
    import pathlib, tempfile
    from comic_studio.engine.genref import (
        expand_image_prompt, EXPAND_T2I_ZH, EXPAND_T2I_EN, EXPAND_EDIT_ZH)
    tmp = pathlib.Path(tempfile.mkdtemp())
    db = Database(tmp / "s.db"); db.migrate()
    orig = "角色：羽川翼。黑长直女性。画面：教室窗边"

    class Cap:
        model = "x"
        last_system = None
        def raw_chat(self, messages, *a, **k):
            Cap.last_system = messages[0]["content"]
            return orig + "。柔光从左上洒落，整体构图沉稳平衡", None
    monkeypatch.setattr("comic_studio.engine.llm.provider.client_for_task",
                        lambda db, t: Cap())
    # t2i 默认中文（用户定调：效果一致优先中文）
    assert expand_image_prompt(db, orig, mode="t2i") != orig
    assert Cap.last_system == EXPAND_T2I_ZH
    # t2i + 语言旋钮 en → 英文系统词
    set_setting(db, "comfy", {"prompt_expand_lang": "en"})
    expand_image_prompt(db, orig, mode="t2i")
    assert Cap.last_system == EXPAND_T2I_EN
    # edit 恒中文（官方规则：正文随用户语言），语言旋钮不影响
    expand_image_prompt(db, orig, mode="edit")
    assert Cap.last_system == EXPAND_EDIT_ZH
    # 非法语言值当 zh 处理
    set_setting(db, "comfy", {"prompt_expand_lang": "jp"})
    expand_image_prompt(db, orig, mode="t2i")
    assert Cap.last_system == EXPAND_T2I_ZH


def test_expand_image_prompt_fallback_and_flag():
    """提示词扩写层（2026-09-22 判例：Qwen-Image 2.1 短提示出不了好效果）：
    LLM 失败/输出过短回落原文（扩写是增强不是门槛）；manifest prompt_expand
    旗只在 qwen21 族模板声明。"""
    from comic_studio.engine.genref import expand_image_prompt
    from comic_studio.engine.workflows import registry
    import pathlib, tempfile, pytest
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "t.api.json").write_text(json.dumps(API))
    (tmp / "m.yaml").write_text(MANIFEST)
    db = Database(tmp / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "t_t2i_test"})
    orig = "角色：羽川翼。黑长直女性。角色主图：单人物全身像"
    # LLM 不可达 → 回落原文不炸
    class Dead:
        model = "x"
        def raw_chat(self, *a, **k):
            raise RuntimeError("down")
    import comic_studio.engine.genref as G
    from comic_studio.engine.llm.provider import LLMError
    monkey = __import__("pytest").MonkeyPatch()
    monkey.setattr("comic_studio.engine.llm.provider.client_for_task", lambda db, t: Dead())
    assert expand_image_prompt(db, orig) == orig
    # 输出过短（<0.6×）= 丢内容 → 回落
    class Short:
        model = "x"
        def raw_chat(self, *a, **k):
            return "太短", None
    monkey.setattr("comic_studio.engine.llm.provider.client_for_task", lambda db, t: Short())
    assert expand_image_prompt(db, orig) == orig
    # 正常扩写通过
    class Good:
        model = "x"
        def raw_chat(self, messages, *a, **k):
            return orig + "。柔和的顶光从左上方洒落，发丝呈现细腻的高光层次", None
    monkey.setattr("comic_studio.engine.llm.provider.client_for_task", lambda db, t: Good())
    out = expand_image_prompt(db, orig)
    assert "顶光" in out and "羽川翼" in out
    monkey.undo()
    regs = registry.scan_templates(registry.TEMPLATE_ROOT)
    assert regs["qwen21_t2i"].prompt_expand == "t2i"
    assert regs["qwen21_edit"].prompt_expand == "edit"
    assert regs["zimage_t2i"].prompt_expand == ""
