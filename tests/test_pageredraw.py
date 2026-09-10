# tests/test_pageredraw.py
"""动态漫角色重绘（2026-09-09）：版本留档/整页重绘/批量编排。"""

from comic_studio.engine.db import Database

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _motion_project(tmp_path, n=3, redraw=1):
    from comic_studio.engine.comic import import_comic
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = import_comic(db, tmp_path / "data", "重绘剧", "9:16",
                       [(f"p{i}.png", PNG * i) for i in range(1, n + 1)],
                       redraw_characters=redraw)["id"]
    return db, pid


def test_kf_versions_natural_order(tmp_path):
    from comic_studio.engine.pageredraw import kf_versions, save_kf_version
    d = tmp_path / "shots/1"; d.mkdir(parents=True)
    (d / "kf_start_v1.png").write_bytes(PNG)
    assert kf_versions(d, "start") == ["v1"]
    (d / "kf_start_v2.png").write_bytes(PNG * 2)
    (d / "kf_start_v10.png").write_bytes(PNG * 3)   # 数字序防 v10<v2
    assert kf_versions(d, "start") == ["v1", "v2", "v10"]
    assert kf_versions(d, "end") == []


def test_activate_start_syncs_prev_end_and_clears_video(tmp_path):
    """切 start 版本 → 前镜尾帧联动同名版本 + 双镜 video_path 置空 + ledger 记录。"""
    import json as _json
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.shots import list_shots, update_shot
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.pageredraw import save_kf_version, activate_kf_version
    shots = list_shots(db, pid)
    s2 = shots[1]
    d2 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/2")
    src = tmp_path / "new.png"; src.write_bytes(PNG * 9)
    save_kf_version(d2, "start", src)               # → v2 + 活动拷贝已刷新
    # 预置两镜已渲染（模拟旧视频）
    for s in (shots[0], s2):
        update_shot(db, s["id"], {"status": "rendered",
                                  "video_path": f"projects/重绘剧/shots/{s['seq']}/video.mp4"})
    out = activate_kf_version(db, tmp_path / "data", s2["id"], "start", "v2")
    assert out["start"] == "v2"
    assert set(out["video_cleared"]) == {s2["id"], shots[0]["id"]}
    d1 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/1")
    assert (d1 / "kf_end_v2.png").read_bytes() == PNG * 9   # 前镜尾帧联动
    assert (d1 / "kf_end.png").read_bytes() == PNG * 9
    after = {s["id"]: s for s in list_shots(db, pid)}
    assert after[shots[0]["id"]]["video_path"] is None
    assert after[s2["id"]]["video_path"] is None
    led = _json.loads(after[s2["id"]]["ledger_json"])
    assert led["kf_active"]["start"] == "v2"


def test_activate_end_only_last_shot(tmp_path):
    """尾帧独立切版仅最后一镜（其余 422 由路由层拦，这里测引擎层 ValueError）。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.pageredraw import activate_kf_version
    import pytest
    with pytest.raises(ValueError):
        activate_kf_version(db, tmp_path / "data", list_shots(db, pid)[0]["id"],
                            "end", "v1")


def test_page_redraw_template_declares_denoise(tmp_path):
    """page_redraw_denoise 真生效的前置：zimage_i2i manifest 必须声明 denoise
    注入槽——filler 只注 manifest 声明过的参数，漏声明则 settings 覆盖被静默
    忽略成死旋钮（2026-09-09 评审修复的回归钉）。"""
    db = Database(tmp_path / "s.db"); db.migrate()
    from comic_studio.engine.workflows.registry import resolve_template
    point = resolve_template(db, "page_redraw").inject_params["denoise"]
    assert (point.node, point.field) == ("4", "denoise")


def test_build_page_redraw_prompt_style_and_clean_text(tmp_path):
    """F1（2026-09-10 真机四修）：i2i 提示词弃用视频提示词 description——
    旧版把六段脚手架截断塞给图像模型（真机根因③）。"""
    db, pid = _motion_project(tmp_path)
    from comic_studio.engine.shots import list_shots, update_shot
    from comic_studio.engine.pageredraw import build_page_redraw_prompt
    from comic_studio.engine.projects import get_project
    proj = get_project(db, pid)
    shot = list_shots(db, pid)[0]
    # 预置视频提示词脚手架当 description——重绘提示词不得携带
    update_shot(db, shot["id"], {
        "description": "subject_definitions:\n某某 是本镜画面中的人物\nsummary:镜头缓缓上移\n"
                       "retention_analysis:某某：fully_pre"})
    shot = list_shots(db, pid)[0]   # 重取：update 后旧行是陈旧的
    p = build_page_redraw_prompt(db, proj, shot)
    assert "subject_definitions" not in p          # 视频脚手架不进图像提示词
    assert "镜头" not in p.split("。")[0]           # 运镜描述不进
    assert "清除对白气泡内的" in p                  # 决策 9：清文字
    assert "按原页画风" in p                        # 画风空=原画风高清化
    # 转风格：改画风后出现画风段
    conn = db.connect()
    conn.execute("UPDATE projects SET style=?, style_vis=? WHERE id=?",
                 ("宫崎骏水彩画风", "水彩手绘质感", pid))
    conn.commit()
    p2 = build_page_redraw_prompt(db, get_project(db, pid), shot)
    assert "水彩手绘质感" in p2
    assert "subject_definitions" not in p2


def test_page_redraw_template_mechanics():
    """F2a（2026-09-10 真机四修）：双图槽（base 原页/char_ref 主图）+
    rembg→IP-Adapter 身份链 + turbo 正确采样机制（cfg=1 res_multistep——
    用户 _raw 实测定稿，旧 zimage_i2i 的 cfg=5+euler 是过度烹煮=根因④）。"""
    from pathlib import Path
    from comic_studio.engine.workflows import registry
    tmpl = registry.scan_templates(Path("templates/workflows"))["zimage_page_redraw"]
    assert [i["slot"] for i in tmpl.inject_images] == ["base", "char_ref"]
    assert "denoise" in tmpl.inject_params and "seed" in tmpl.inject_params
    wf = tmpl.api_json()
    classes = {n["class_type"] for n in wf.values()}
    assert "Image Rembg (Remove Background)" in classes
    assert "easy ipadapterApply" in classes
    ks = next(n for n in wf.values() if n["class_type"] == "KSampler")
    assert ks["inputs"]["cfg"] == 1
    assert ks["inputs"]["sampler_name"] == "res_multistep"
    assert ks["inputs"]["denoise"] == 0.55
    # 身份链接线：rembg 吃 char_ref 图（节点对位），ipadapter 吃 rembg 出图
    char_node = tmpl.inject_images[1]["node"]
    rembg_id = next(nid for nid, n in wf.items()
                    if n["class_type"] == "Image Rembg (Remove Background)")
    assert wf[rembg_id]["inputs"]["images"] == [char_node, 0]
    ipa = next(n for n in wf.values() if n["class_type"] == "easy ipadapterApply")
    assert ipa["inputs"]["image"] == [rembg_id, 0]


def test_redraw_page_injects_char_ref_slot(tmp_path):
    """RC5（2026-09-10 真机二轮）：char_ref/IP-Adapter 参考注入**停用**——单参考
    全局盖章把整页人物拉成参考脸（女性也变黄毛）；绑定角色+main.png 存在也
    只注 base，模板双槽声明保留待未来分人区域控制。"""
    db, pid = _motion_project(tmp_path, n=2)
    from types import SimpleNamespace as NS
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.shots import list_shots, update_shot
    from comic_studio.engine.pageredraw import redraw_page
    from comic_studio.engine.paths import data_to_abs
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    ids = persist_assets(db, tmp_path / "data", pid,
                         NS(characters=[NS(name="小红", appearance="性别：女", tags=[])],
                             scenes=[], props=[]))
    s1 = list_shots(db, pid)[0]
    import json as _json
    led = _json.loads(s1["ledger_json"])
    led["assets"] = {"characters": ids, "scenes": [], "props": []}
    update_shot(db, s1["id"], {"ledger_json": _json.dumps(led, ensure_ascii=False)})
    a = list_project_assets(db, pid)[0]
    d = data_to_abs(tmp_path / "data", a["library_dir"]); d.mkdir(parents=True, exist_ok=True)
    (d / "main.png").write_bytes(PNG * 3)
    with comfy_server("ok") as m:
        redraw_page(db, tmp_path / "data", s1["id"], ComfyClient(m.base_url))
    names = [str(u) for u in m.uploads]
    assert any("base" in n for n in names), names
    assert not any("char_ref" in n for n in names), names   # 参考脸不注入


def test_redraw_page_produces_v2_and_clears_video(tmp_path):
    """整页重绘：comfy_mock 出图 → start v2 + 前镜（若有）尾帧联动 + 视频置空。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.shots import list_shots, update_shot
    from comic_studio.engine.pageredraw import redraw_page, kf_versions
    from comic_studio.engine.paths import data_to_abs
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    shots = list_shots(db, pid)
    s2 = shots[1]
    update_shot(db, s2["id"], {"status": "rendered",
                               "video_path": "projects/重绘剧/shots/2/video.mp4"})
    with comfy_server("ok") as m:
        out = redraw_page(db, tmp_path / "data", s2["id"], ComfyClient(m.base_url))
    assert out.name == "kf_start_v2.png"
    d2 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/2")
    assert kf_versions(d2, "start") == ["v1", "v2"]
    after = {s["id"]: s for s in list_shots(db, pid)}
    assert after[s2["id"]]["video_path"] is None            # 决策 12
    assert after[shots[0]["id"]]["video_path"] is None      # 前镜尾帧联动连带
    d1 = data_to_abs(tmp_path / "data", f"projects/重绘剧/shots/1")
    assert (d1 / "kf_end_v2.png").exists()
    # 底图=原页（不是活动 kf）：上传清单里应有 page_002.png
    # （comfy_mock 记录上传名，见 _make_handler；此处以产物存在为弱断言）


def test_enqueue_batch_redraw_marks_and_idempotent(tmp_path):
    """批量：标记 pending + 入队 + redraw_done=1；重发只补仍 pending 的镜。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.pageredraw import enqueue_batch_redraw
    from comic_studio.engine.projects import get_project
    from comic_studio.engine.shots import list_shots, update_shot
    import json as _json
    # settings 配置 comfy 地址（门禁）
    from comic_studio.engine.settings import set_setting
    set_setting(db, "comfy", {"base_url": "http://x:8188"})
    assert enqueue_batch_redraw(db, tmp_path / "data", pid) == 3
    assert get_project(db, pid)["redraw_done"] == 1
    # ruling 1：engine/jobs.py 无 list_jobs——直查 SQL
    jobs = db.connect().execute(
        "SELECT * FROM jobs WHERE project_id=? AND type='redraw_kf'",
        (pid,)).fetchall()
    assert len(jobs) == 3
    for s in list_shots(db, pid):
        assert _json.loads(s["ledger_json"]).get("pending_redraw") is True
    # 模拟镜 2 已完成（清标记）→ 重发只入队 2 个（1/3 仍带标记不重复入队）
    s2 = list_shots(db, pid)[1]
    led = _json.loads(s2["ledger_json"]); led.pop("pending_redraw")
    update_shot(db, s2["id"], {"ledger_json": _json.dumps(led, ensure_ascii=False)})
    assert enqueue_batch_redraw(db, tmp_path / "data", pid) == 2
    jobs = db.connect().execute(
        "SELECT * FROM jobs WHERE project_id=? AND type='redraw_kf'",
        (pid,)).fetchall()
    assert len(jobs) == 5   # 3 + 2：已标记的镜不重复入队


def test_enqueue_batch_redraw_zero_pending_refull(tmp_path):
    """终审统一零-pending 规则：整轮完成（标记全清）后再发批量=全新整批——
    重新标记+入队全部非禁用镜（不论 redraw_done）。旧实现返 {"enqueued":0}
    死路，卡死 purge→重提取→重批量 与 换画风→整批重做 两条恢复流。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.pageredraw import enqueue_batch_redraw
    from comic_studio.engine.settings import set_setting
    set_setting(db, "comfy", {"base_url": "http://x:8188"})
    assert enqueue_batch_redraw(db, tmp_path / "data", pid) == 3
    # 模拟整轮完成：handler 成功语义=清全部标记
    import json as _json
    from comic_studio.engine.shots import list_shots, update_shot
    for s in list_shots(db, pid):
        led = _json.loads(s["ledger_json"]); led.pop("pending_redraw", None)
        update_shot(db, s["id"],
                    {"ledger_json": _json.dumps(led, ensure_ascii=False)})
    # 重发 → 3（旧实现 0）
    assert enqueue_batch_redraw(db, tmp_path / "data", pid) == 3
    jobs = db.connect().execute(
        "SELECT COUNT(*) c FROM jobs WHERE project_id=? AND type='redraw_kf'",
        (pid,)).fetchone()["c"]
    assert jobs == 6
    for s in list_shots(db, pid):
        assert _json.loads(s["ledger_json"]).get("pending_redraw") is True


def test_handle_redraw_kf_clears_pending(tmp_path):
    db, pid = _motion_project(tmp_path, n=2)
    from comic_studio.engine.pageredraw import enqueue_batch_redraw, handle_redraw_kf
    from comic_studio.engine.settings import set_setting
    set_setting(db, "comfy", {"base_url": "http://x:8188"})
    enqueue_batch_redraw(db, tmp_path / "data", pid)
    from comic_studio.engine.shots import list_shots
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    job = db.connect().execute(
        "SELECT * FROM jobs WHERE project_id=? AND type='redraw_kf' ORDER BY id LIMIT 1",
        (pid,)).fetchone()
    with comfy_server("ok") as m:
        handle_redraw_kf(db, tmp_path / "data", job, ComfyClient(m.base_url))
    sid = job["shot_id"]
    s = {x["id"]: x for x in list_shots(db, pid)}[sid]
    import json
    assert "pending_redraw" not in json.loads(s["ledger_json"])


def test_redraw_page_bootstraps_missing_pages(tmp_path):
    """旧项目（PATCH 后开重绘，无 pages/）：从活动 kf_start 拷贝建源页再重绘。"""
    import shutil
    db, pid = _motion_project(tmp_path)
    from comic_studio.engine.paths import data_to_abs
    from comic_studio.engine.projects import get_project
    slug = get_project(db, pid)["slug"]
    shutil.rmtree(data_to_abs(tmp_path / "data", f"projects/{slug}/pages"))
    from comic_studio.engine.shots import list_shots
    from comic_studio.engine.pageredraw import redraw_page
    from tests.comfy_mock import comfy_server
    from comic_studio.engine.comfy.client import ComfyClient
    with comfy_server("ok") as m:
        redraw_page(db, tmp_path / "data", list_shots(db, pid)[0]["id"],
                    ComfyClient(m.base_url))
    assert (data_to_abs(tmp_path / "data", f"projects/{slug}/pages/page_001.png")).exists()


def test_batch_redraw_shares_one_seed(tmp_path):
    """F3（2026-09-10 真机四修）：同批共用一个 seed——真机根因②两镜风格漂移
    （shots.seed 全 NULL → 每镜随机）。单镜重生仍随机（routes 侧）。"""
    db, pid = _motion_project(tmp_path, n=3)
    from comic_studio.engine.pageredraw import enqueue_batch_redraw
    from comic_studio.engine.settings import set_setting
    set_setting(db, "comfy", {"base_url": "http://x:8188"})
    enqueue_batch_redraw(db, tmp_path / "data", pid)
    import json as _json
    rows = db.connect().execute(
        "SELECT payload_json FROM jobs WHERE project_id=? AND type='redraw_kf'",
        (pid,)).fetchall()
    seeds = {_json.loads(r["payload_json"]).get("seed") for r in rows}
    assert len(rows) == 3 and len(seeds) == 1 and None not in seeds
    # 补漏复用在飞 seed：模拟镜 2 完成（清标记）→ 重发只补它，seed 与整批一致
    from comic_studio.engine.shots import list_shots, update_shot
    s2 = list_shots(db, pid)[1]
    led2 = _json.loads(s2["ledger_json"]); led2.pop("pending_redraw")
    update_shot(db, s2["id"], {"ledger_json": _json.dumps(led2, ensure_ascii=False)})
    # 把镜 1/3 的 job 置 done（模拟跑完），镜 2 重发后应复用同批 seed
    db.connect().execute("UPDATE jobs SET status='done' WHERE project_id=?", (pid,))
    db.connect().commit()
    enqueue_batch_redraw(db, tmp_path / "data", pid)   # 零 pending=全新整批（新 seed 合法）
    row = db.connect().execute(
        "SELECT payload_json FROM jobs WHERE project_id=? AND type='redraw_kf' "
        "AND status='pending' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    new_seed = _json.loads(row["payload_json"])["seed"]
    # 在飞场景：留一个 pending（补漏路径）→ 复用其 seed
    s3 = list_shots(db, pid)[2]
    led3 = _json.loads(s3["ledger_json"]); led3["pending_redraw"] = True
    update_shot(db, s3["id"], {"ledger_json": _json.dumps(led3, ensure_ascii=False)})
    enqueue_batch_redraw(db, tmp_path / "data", pid)
    rows2 = db.connect().execute(
        "SELECT payload_json FROM jobs WHERE project_id=? AND type='redraw_kf' "
        "AND status='pending'", (pid,)).fetchall()
    for r in rows2:
        assert _json.loads(r["payload_json"])["seed"] == new_seed, "补漏须复用在飞 seed"


def test_batch_redraw_prebinds_named_characters(tmp_path):
    """F2b：批量入口补跑 auto_bind——存量项目「强制重读」统一命名后点批量即绑上
    （真机根因①：读图 VLM 与提取 VLM 各起各名，绑定全空=身份桥断）。"""
    from types import SimpleNamespace as NS
    db, pid = _motion_project(tmp_path, n=2)
    from comic_studio.engine.assets import persist_assets, list_project_assets
    from comic_studio.engine.pageredraw import enqueue_batch_redraw
    from comic_studio.engine.settings import set_setting
    from comic_studio.engine.shots import list_shots, update_shot
    set_setting(db, "comfy", {"base_url": "http://x:8188"})
    ids = persist_assets(db, tmp_path / "data", pid,
                         NS(characters=[NS(name="小红", appearance="性别：女", tags=[])],
                             scenes=[], props=[]))
    s1 = list_shots(db, pid)[0]
    update_shot(db, s1["id"], {"description": "小红 在院子里推门。"})
    enqueue_batch_redraw(db, tmp_path / "data", pid)
    led = __import__("json").loads(list_shots(db, pid)[0]["ledger_json"])
    assert led["assets"]["characters"] == list(ids)


def test_bind_redraw_characters_variant_matching(tmp_path):
    """F2b+（2026-09-10 真机加修）：变体归一补绑——前缀（黑发女性↔黑发女子）
    + 发色近义（白发→金发，黑白页浅金发被读成白发，用户确认同人）；
    不动通用 auto_bind（小说/漫改语义不变）。"""
    from types import SimpleNamespace as NS
    db, pid = _motion_project(tmp_path, n=4)
    from comic_studio.engine.assets import persist_assets
    from comic_studio.engine.pageredraw import bind_redraw_characters
    from comic_studio.engine.shots import list_shots, update_shot
    ids = persist_assets(db, tmp_path / "data", pid, NS(characters=[
        NS(name="黑发女性", appearance="性别：女", tags=[]),
        NS(name="金发女性", appearance="性别：女", tags=[]),
        NS(name="小红", appearance="性别：女", tags=[]),
    ], scenes=[], props=[]))
    hid, jid, xid = ids
    descs = ["黑发女子 在窗边梳头。",          # 前缀变体（去末字）
             "白发女子 对镜自拍。",            # 发色近义 → 金发
             "小红 和 小明 走过。",            # 2字名只精确匹配（不绑 小明——无此资产）
             "金发女性 与 男性角色 交谈。"]     # 全名精确
    for s, d in zip(list_shots(db, pid), descs):
        update_shot(db, s["id"], {"description": d})
    n = bind_redraw_characters(db, pid)
    assert n >= 3
    led = [__import__("json").loads(s["ledger_json"])["assets"]["characters"]
           for s in list_shots(db, pid)]
    assert led[0] == [hid]        # 黑发女子 → 黑发女性
    assert led[1] == [jid]        # 白发女子 → 金发女性
    assert led[2] == [xid]        # 小红 精确；小明无资产不绑
    assert led[3] == [jid]        # 金发女性 精确（男性角色无资产不绑）
    # 无交叉误绑：黑发desc不绑金发资产（led[0] 不含 jid）已在上面断言
