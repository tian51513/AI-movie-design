# tests/test_paths.py
from pathlib import Path

from comic_studio.engine.paths import data_to_abs, rel_to_data


def test_rel_to_data_strips_data_root_as_posix(tmp_path):
    abs_path = tmp_path / "projects" / "斗破" / "novel.txt"
    assert rel_to_data(tmp_path, abs_path) == "projects/斗破/novel.txt"


def test_rel_to_data_accepts_string_input(tmp_path):
    assert rel_to_data(str(tmp_path), str(tmp_path / "library" / "characters" / "3")) == \
        "library/characters/3"


def test_roundtrip(tmp_path):
    rel = "projects/x/novel.txt"
    assert rel_to_data(tmp_path, data_to_abs(tmp_path, rel)) == rel


def test_data_to_abs_resolves_under_data_root(tmp_path):
    assert data_to_abs(tmp_path, "library/characters/3") == tmp_path / "library" / "characters" / "3"


def test_data_to_abs_passthrough_legacy_absolute(tmp_path):
    legacy = str(tmp_path / "projects" / "old" / "novel.txt")
    assert data_to_abs(tmp_path, legacy) == Path(legacy)


def test_data_to_abs_passthrough_windows_absolute():
    assert data_to_abs("/some/data", r"E:\data\projects\x\novel.txt") == Path(r"E:\data\projects\x\novel.txt")
