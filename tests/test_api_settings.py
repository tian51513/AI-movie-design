# tests/test_api_settings.py
from fastapi.testclient import TestClient

from comic_studio.web.app import create_app

ROUTING_DEFAULTS = {   # asr_cleanup（2026-09-05 P10C）随后端默认同步
    "asr_cleanup": "local",
    "extract_assets": "local",
    "fix_appearance": "local",
    "split_storyboards": "online",
    "gen_video_prompt": "online",
    "optimize_prompt": "online",
    "gen_story": "online",
    "describe_shot": "local",
}


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "t.db", data_dir=tmp_path / "data", start_workers=False))


def test_get_returns_effective_defaults(tmp_path):
    with _client(tmp_path) as c:
        body = c.get("/api/settings").json()
        assert body["llm_providers"]["local"]["model"] == "qwen3:14b"
        assert body["llm_routing"] == ROUTING_DEFAULTS


def test_put_roundtrip_persists(tmp_path):
    payload = {
        "llm_providers": {
            "local": {"base_url": "http://localhost:11434/v1", "api_key": "ollama",
                      "model": "qwen3:32b"},
            "online": {"base_url": "https://api.example.com/v1", "api_key": "sk-x",
                       "model": "deepseek-chat"},
        },
        "llm_routing": {"extract_assets": "online"},
    }
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json=payload)
        assert resp.status_code == 200
        body = c.get("/api/settings").json()
        assert body["llm_providers"]["local"]["model"] == "qwen3:32b"
        assert body["llm_providers"]["online"]["base_url"] == "https://api.example.com/v1"
        assert body["llm_routing"]["extract_assets"] == "online"
        # 未提供的路由键保留默认（深合并语义）
        assert body["llm_routing"]["split_storyboards"] == "online"


def test_put_rejects_routing_to_unknown_provider(tmp_path):
    payload = {"llm_routing": {"extract_assets": "nonexistent"}}
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json=payload)
        assert resp.status_code == 422


def test_put_rejects_unknown_task_or_provider_keys(tmp_path):
    with _client(tmp_path) as c:
        assert c.put("/api/settings", json={"llm_routing": {"unknown_task": "local"}}).status_code == 422
        # 服务商键动态化（2026-09-17）：bad_name 合法可新增；冒号/大写仍拒
        assert c.put("/api/settings", json={"llm_providers": {
            "bad:name": {"base_url": "http://x", "api_key": "k", "model": "m"}}}).status_code == 422


def test_put_rejects_malformed_provider(tmp_path):
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"llm_providers": {"local": {"base_url": 123}}})
        assert resp.status_code == 422


def test_put_comfy_base_url(tmp_path):
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"comfy": {"base_url": "http://192.168.3.1:8188"}})
        assert resp.status_code == 200
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://192.168.3.1:8188"


def test_put_comfy_partial_update_keeps_base_url(tmp_path):
    """事故复盘（2026-09-01）：comfy 局部更新不得把未提供的键冲成默认——
    全量 model_dump 曾让 base_url 被覆盖成空串，36k 渲染任务以 AttributeError 批量失败。"""
    with _client(tmp_path) as c:
        assert c.put("/api/settings", json={"comfy": {"base_url": "http://127.0.0.1:8188"}}).status_code == 200
        resp = c.put("/api/settings", json={"comfy": {"mute_quiet_shots": True}})
        assert resp.status_code == 200
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://127.0.0.1:8188"


def test_put_comfy_empty_base_url_rejected(tmp_path):
    """base_url 显式传空 → 422（渲染/参考图/快车道全依赖它，误清空必须在门口拦下）。"""
    with _client(tmp_path) as c:
        assert c.put("/api/settings", json={"comfy": {"base_url": "http://127.0.0.1:8188"}}).status_code == 200
        resp = c.put("/api/settings", json={"comfy": {"base_url": ""}})
        assert resp.status_code == 422
        assert c.get("/api/settings").json()["comfy"]["base_url"] == "http://127.0.0.1:8188"


def test_put_template_map_roundtrip(tmp_path):
    """PUT template_map {t2i: x} 能 roundtrip 读回。"""
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"template_map": {"t2i": "my_custom"}})
        assert resp.status_code == 200
        body = c.get("/api/settings").json()
        assert body["template_map"]["t2i"] == "my_custom"
        # 其他键保留默认
        assert body["template_map"]["character_views"] == "character_views"


def test_put_template_map_rejects_unknown_key(tmp_path):
    """template_map 未知键返回 422。"""
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"template_map": {"bad_key": "x"}})
        assert resp.status_code == 422


def test_put_template_map_accepts_page_redraw(tmp_path):
    """page_redraw（动态漫整页重绘模板，2026-09-09 前端设置行）在白名单内，
    PUT 能 roundtrip；否则设置页「保存全部设置」会整单 422。"""
    with _client(tmp_path) as c:
        resp = c.put("/api/settings", json={"template_map": {"page_redraw": "zimage_i2i"}})
        assert resp.status_code == 200
        body = c.get("/api/settings").json()
        assert body["template_map"]["page_redraw"] == "zimage_i2i"
        # 默认值兜底（DEFAULT_SETTINGS 里已是 zimage_i2i）
        assert body["template_map"]["t2i"] == "zimage_t2i"


def test_providers_partial_put_preserves_unset(tmp_path):
    """M15（2026-09-05 审计）：llm_providers PUT 全量 model_dump——部分字段
    PUT 会把未提供键冲默认（09-01 comfy 事故同款）。exclude_unset 后局部
    PUT 只改提供的键。"""
    with _client(tmp_path) as c:
        r = c.put("/api/settings", json={"llm_providers": {
            "online": {"base_url": "http://a", "api_key": "k",
                       "model": "m1", "extra_body": None}}})
        assert r.status_code == 200, r.text
        r2 = c.put("/api/settings", json={"llm_providers": {
            "online": {"model": "m2"}}})
        assert r2.status_code == 200, r2.text
        prov = c.get("/api/settings").json()["llm_providers"]["online"]
        assert prov["model"] == "m2"
        assert prov["base_url"] == "http://a" and prov["api_key"] == "k"


def test_asr_engine_setting_roundtrip(tmp_path):
    """P10-D：asr 引擎设置往返（默认 faster_whisper → 切 comfy_qwen3），
    非法值 422。"""
    with _client(tmp_path) as c:
        assert c.get("/api/settings").json()["asr"]["engine"] == "faster_whisper"
        r = c.put("/api/settings", json={"asr": {"engine": "comfy_qwen3"}})
        assert r.status_code == 200
        assert c.get("/api/settings").json()["asr"]["engine"] == "comfy_qwen3"
        assert c.put("/api/settings", json={"asr": {"engine": "xx"}}).status_code == 422


def test_style_presets_api(tmp_path):
    """Krea2 风格库（2026-09-12）：GET /api/settings/style-presets 返回
    73 库两级结构，每风格带成品 prompt。"""
    with _client(tmp_path) as c:
        r = c.get("/api/settings/style-presets")
        assert r.status_code == 200
        libs = r.json()
        assert len(libs) >= 70
        first_lib = next(iter(libs.values()))
        assert first_lib and all("prompt" in s and s["prompt"]
                                 for s in first_lib[:3])




# ---------- 2026-09-17 服务商动态化：本地/线上均可配 N 个，默认各一 ----------

def test_put_accepts_new_dynamic_provider_keys(tmp_path):
    """local3/ollama2 等合法键（^[a-z][a-z0-9_]*$）可新增合并；冒号/大写/数字开头仍 422。"""
    with _client(tmp_path) as c:
        r = c.put("/api/settings", json={"llm_providers": {
            "local3": {"base_url": "http://127.0.0.1:8080/v1", "api_key": "k",
                       "model": "m1"}}})
        assert r.status_code == 200
        prov = c.get("/api/settings").json()["llm_providers"]
        assert prov["local3"]["base_url"] == "http://127.0.0.1:8080/v1"
        assert c.put("/api/settings", json={"llm_providers": {
            "lm_studio": {"base_url": "http://x", "model": "m"}}}).status_code == 200
        for bad in ("local:3", "Upper", "1bad", ""):
            assert c.put("/api/settings", json={"llm_providers": {
                bad: {"base_url": "http://x"}}}).status_code == 422, bad


def test_put_null_deletes_provider(tmp_path):
    """子字典 null = 删除连接；被路由引用时 422 拒绝（提示先改路由）。"""
    with _client(tmp_path) as c:
        c.put("/api/settings", json={"llm_providers": {
            "local3": {"base_url": "http://x", "model": "m1"}}})
        # 未引用 → 删成功
        assert c.put("/api/settings", json={"llm_providers": {"local3": None}}).status_code == 200
        assert "local3" not in c.get("/api/settings").json()["llm_providers"]
        # 有路由引用 → 422
        c.put("/api/settings", json={"llm_providers": {
            "local3": {"base_url": "http://x", "model": "m1"}}})
        c.put("/api/settings", json={"llm_routing": {"extract_assets": "local3"}})
        r = c.put("/api/settings", json={"llm_providers": {"local3": None}})
        assert r.status_code == 422 and "路由" in r.json()["detail"]


def test_default_providers_local_and_online_only(tmp_path):
    """默认服务商各一（local+online）；存量库的 local2 经深合并保留。"""
    with _client(tmp_path) as c:
        keys = set(c.get("/api/settings").json()["llm_providers"])
        assert keys == {"local", "online"}


def test_llm_test_accepts_dynamic_provider(tmp_path):
    """llm-test 的 provider 放宽为任意非空标签（动态连接名）。"""
    with _client(tmp_path) as c:
        r = c.post("/api/settings/llm-test", json={
            "provider": "local5", "base_url": "http://127.0.0.1:9/v1",
            "api_key": "", "model": "m"})
        assert r.status_code == 200 and r.json()["ok"] is False  # 服务不可达但不再 422


def test_delete_provider_and_reroute_in_same_put(tmp_path):
    """同一次保存里改路由 + 删连接（UI 的自然操作序）：护栏必须按提交后的
    生效路由判断，不能按库里的旧路由误拦（2026-09-17 Playwright 实测）。"""
    with _client(tmp_path) as c:
        c.put("/api/settings", json={"llm_providers": {
            "local3": {"base_url": "http://x", "model": "m1"}}})
        c.put("/api/settings", json={"llm_routing": {"extract_assets": "local3"}})
        r = c.put("/api/settings", json={
            "llm_providers": {"local3": None},
            "llm_routing": {"extract_assets": "local"}})   # 同单改走+删除
        assert r.status_code == 200, r.text
        prov = c.get("/api/settings").json()["llm_providers"]
        assert "local3" not in prov
        assert c.get("/api/settings").json()["llm_routing"]["extract_assets"] == "local"


def test_model_templates_expose_effective_models(tmp_path):
    """映射区模型可见性（2026-10-01 用户需求：不知道漫画页主模型是什么）：
    GET model_templates 每项带 models 槽位与当前生效值——覆盖
    （model_overrides）> api.json 默认，开关槽内置关=空值；离线计算不碰
    ComfyUI（/models/choices 才是在线枚举）。"""
    from comic_studio.engine.workflows import registry
    with _client(tmp_path) as c:
        body = c.get("/api/settings").json()
        krea = next(t for t in body["model_templates"]
                    if t["id"] == "comic_page_krea2")
        slots = {m["label"]: m for m in krea["models"]}
        wf = registry.load_manifest(
            registry.TEMPLATE_ROOT / "comic_page_krea2.yaml").api_json()
        # 默认值=api.json 节点现值；label_cn 带中文说明
        assert slots["unet"]["value"] == wf["90"]["inputs"]["unet_name"]
        assert slots["unet"]["label_cn"] == "主模型（Krea2）"
        assert slots["unet"]["overridden"] is False
        # 开关槽内置关（lora3~8 默认关）→ 生效值空=「（关闭）」
        assert slots["lora5"]["value"] == ""
        # 覆盖后生效值翻转 + 标记
        r = c.put("/api/settings", json={"model_overrides": {
            "comic_page_krea2": {"unet": "my_model.safetensors"}}})
        assert r.status_code == 200, r.text
        body = c.get("/api/settings").json()
        slots = {m["label"]: m for m in next(
            t for t in body["model_templates"]
            if t["id"] == "comic_page_krea2")["models"]}
        assert slots["unet"]["value"] == "my_model.safetensors"
        assert slots["unet"]["overridden"] is True


def test_effective_models_tolerates_broken_manifest(tmp_path):
    """容错护栏：api.json 读不出的模板（file 字段笔误等）→ 空清单，
    不把整个设置页 GET 炸黑（2026-10-01 audio_to_text.json 判例）。"""
    from comic_studio.engine.workflows.registry import ModelSlot
    from comic_studio.web.routes_settings import _effective_models

    class Broken:
        id = "broken"
        models = [ModelSlot(label="unet", node="1", field="unet_name",
                            cls="UNETLoader", label_cn="主模型")]

        def api_json(self):
            raise FileNotFoundError("nope")

    assert _effective_models(Broken(), {}) == []
