# tests/test_workflow_registry.py
import textwrap

import pytest

from comic_studio.engine.workflows.registry import (
    ManifestError, load_manifest, resolve_template, scan_templates)

MANIFEST = textwrap.dedent("""
    id: t_test
    type: t2i
    name: 测试文生图
    file: t_test.api.json
    prompt_format: "{kind_label}设定图：{name}。{detail}"
    inject:
      prompt: {node: "6", field: "text"}
      params:
        seed: {node: "3", field: "seed"}
    outputs:
      - {node: "9", filename_prefix: "cs/{project}/{asset}"}
    requires: []
""")


def _write(root):
    (root / "t_test.api.json").write_text('{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}')
    (root / "t_test.yaml").write_text(MANIFEST)


def test_load_and_scan(tmp_path):
    _write(tmp_path)
    t = load_manifest(tmp_path / "t_test.yaml")
    assert t.id == "t_test" and t.type == "t2i"
    assert t.inject_prompt == ("6", "text") or (t.inject_prompt.node, t.inject_prompt.field) == ("6", "text")
    reg = scan_templates(tmp_path)
    assert set(reg) == {"t_test"}


def test_duplicate_id_rejected(tmp_path):
    _write(tmp_path)
    (tmp_path / "dup.yaml").write_text(MANIFEST)
    with pytest.raises(ManifestError):
        scan_templates(tmp_path)


def test_prompt_expand_mode_parsing(tmp_path):
    """prompt_expand 模式化（Qwen-Image 2.1 官方扩写规范接入）：str 模式
    （t2i=官方 8 步观察者流 / edit=官方属性解耦流）；bool true 兼容映射
    t2i；缺省/False 关闭；非法值防 typo 报 ManifestError。"""
    def _load_with(val):
        extra = "" if val is None else f"prompt_expand: {val}\n"
        (tmp_path / "pe.api.json").write_text('{}')
        (tmp_path / "pe.yaml").write_text(MANIFEST.replace(
            "requires: []", f"requires: []\n{extra}").replace("t_test", "pe_x"))
        return load_manifest(tmp_path / "pe.yaml")
    assert _load_with(None).prompt_expand == ""
    assert _load_with("false").prompt_expand == ""
    assert _load_with("true").prompt_expand == "t2i"      # 旧布尔真值兼容
    assert _load_with("t2i").prompt_expand == "t2i"
    assert _load_with("edit").prompt_expand == "edit"
    with pytest.raises(ManifestError):
        _load_with("chat")   # 非法模式防 typo


def test_resolve_via_settings(tmp_path, monkeypatch):
    from comic_studio.engine.db import Database
    from comic_studio.engine.workflows import registry
    from comic_studio.engine.settings import set_setting
    _write(tmp_path)
    monkeypatch.setattr(registry, "TEMPLATE_ROOT", tmp_path)
    db = Database(tmp_path / "s.db"); db.migrate()
    set_setting(db, "template_map", {"t2i": "t_test"})
    t = resolve_template(db, "t2i")
    assert t.id == "t_test"
    set_setting(db, "template_map", {"t2i": "missing_id"})
    with pytest.raises(ManifestError):
        resolve_template(db, "t2i")


def test_qwen21_lora_stack_wiring():
    """qwen21 双模板 8 LoRA 槽（2026-10-01 用户需求：同 character_views 可配）：
    manifest 声明 lora1..8 开关槽；api.json 里 LazyKreaLoraStack 串在
    UNETLoader 之后、采样器/模型缓存之前；槽默认全 none（不加 LoRA=原味）。"""
    from comic_studio.engine.workflows import registry
    regs = registry.scan_templates(registry.TEMPLATE_ROOT)
    for tid, unet, consumer in (("qwen21_t2i", "468", "474"),
                                ("qwen21_edit", "479", "486")):
        t = regs[tid]
        loras = [s for s in t.models if s.label.startswith("lora")]
        assert len(loras) == 8, tid
        assert all(s.switch_field for s in loras), tid   # 开关语义（（关闭）选项）
        assert "ComfyUI_Lazybuxuexi" in t.requires, tid
        wf = t.api_json()
        nid = next(k for k, v in wf.items()
                   if v["class_type"] == "LazyKreaLoraStack")
        stack = wf[nid]["inputs"]
        assert stack["模型"] == [unet, 0], tid
        assert wf[consumer]["inputs"]["model"] == [nid, 0], tid
        assert all(stack[f"LoRA_{i}"] == "none" for i in range(1, 9)), tid
