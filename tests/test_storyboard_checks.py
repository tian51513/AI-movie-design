# tests/test_storyboard_checks.py
"""P7-G 第二批机械校验器（借鉴 XiaoLuo/短剧厂 2026-08-28）：只告警不拦截。"""
import json
from types import SimpleNamespace as NS

from comic_studio.engine.db import Database
from comic_studio.engine.projects import create_project
from comic_studio.engine.shots import persist_shots


def _shot(dur=5.0, scene="", jing="中景", dialogue=None):
    ledger = {"assets": {"scenes": [scene] if scene else []}}
    if dialogue:
        ledger["dialogue"] = dialogue
    return NS(text_span="", description="x", shot_type="", camera={},
              duration=dur, workflow_type="ref2va", ledger=ledger,
              character_ids=[], scene_ids=[], prop_ids=[], depends_on=None)


def _mk(tmp_path, shots, target=0.0, name="审计剧"):
    db = Database(tmp_path / "s.db"); db.migrate()
    pid = create_project(db, tmp_path / "data", name, "9:16", "t",
                         target_duration=target)["id"]
    persist_shots(db, pid, shots)
    return db, pid


def _set_camera(db, pid, seq, jing):
    conn = db.connect()
    row = conn.execute("SELECT id, camera_json FROM shots WHERE project_id=? AND seq=?",
                       (pid, seq)).fetchone()
    conn.execute("UPDATE shots SET camera_json=? WHERE id=?",
                 (json.dumps({"景别": jing}, ensure_ascii=False), row["id"]))
    conn.commit()


def test_audit_duration_conservation(tmp_path):
    from comic_studio.engine.storyboard_checks import audit_storyboard
    db, pid = _mk(tmp_path, [_shot(5) for _ in range(8)], target=60)  # 40s vs 60s
    warns = audit_storyboard(db, pid)
    assert any("时长守恒" in w and "40" in w for w in warns)
    # 达标不告警
    db2, pid2 = _mk(tmp_path, [_shot(5) for _ in range(12)], target=60, name="达标剧")
    assert not any("时长守恒" in w for w in audit_storyboard(db2, pid2))


def test_audit_visual_gear_shift(tmp_path):
    """换挡检测：>30s 无景别/场景大变化 → 告警（防 AI 画面单调）。"""
    from comic_studio.engine.storyboard_checks import audit_storyboard
    db, pid = _mk(tmp_path, [_shot(5, scene="S1") for _ in range(10)])  # 50s 同场景
    for i in range(1, 11):
        _set_camera(db, pid, i, "中景")
    warns = audit_storyboard(db, pid)
    assert any("换挡" in w for w in warns)
    # 中途切一次景别 → 无告警
    _set_camera(db, pid, 6, "特写")
    assert not any("换挡" in w for w in audit_storyboard(db, pid))


def test_audit_long_dialogue_line(tmp_path):
    from comic_studio.engine.storyboard_checks import audit_storyboard
    long_line = "这句话真的非常非常长长到自然语速在五秒镜头里根本说不完还需要更多时间才能说完"
    db, pid = _mk(tmp_path, [_shot(5, dialogue=[{"speaker": "甲", "line": long_line}])])
    warns = audit_storyboard(db, pid)
    assert any("台词超 25 字" in w and "镜 1" in w for w in warns)


def test_sensitive_replacement_fixer():
    """敏感词替换转译库（借鉴短剧厂：机械修复而非拦截）。"""
    from comic_studio.engine.textfix import apply_sensitive_replacements
    text, n = apply_sensitive_replacements("他说要杀了他，背后是黑社会势力。")
    assert n >= 2 and "杀了" not in text and "黑社会" not in text
    assert "废了" in text and "灰色势力" in text
    t2, n2 = apply_sensitive_replacements("干净的文本")
    assert t2 == "干净的文本" and n2 == 0


def test_audit_scale_pool_monotony(tmp_path):
    """B：景别池单一检测——全程无全景/远景 → 告警（人物撑满画面的根因可自查）。"""
    from comic_studio.engine.storyboard_checks import audit_storyboard
    db, pid = _mk(tmp_path, [_shot(5, scene="S1") for _ in range(6)], name="单调剧")
    for i in range(1, 7):
        _set_camera(db, pid, i, "中景" if i % 2 else "近景")
    warns = audit_storyboard(db, pid)
    assert any("景别池单一" in w for w in warns)
    _set_camera(db, pid, 3, "全景")  # 出现一个环境镜 → 不再告警
    assert not any("景别池单一" in w for w in audit_storyboard(db, pid))


def test_split_system_scale_rhythm_rule():
    """A：拆解规则 3 改景别节奏——每 3~5 镜至少 1 个全景/远景环境镜。"""
    from comic_studio.engine.llm.storyboard import SPLIT_SYSTEM
    assert "每 3~5 镜至少" in SPLIT_SYSTEM and "全景或远景" in SPLIT_SYSTEM
    assert "优先中景/近景" not in SPLIT_SYSTEM  # 旧保护条款已替换
