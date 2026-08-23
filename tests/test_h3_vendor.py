import subprocess
import sys
from pathlib import Path

from comic_studio.engine.prompts import H3_DIR


def test_vendored_files_present():
    for rel in ["SKILL.md", "references/capability-map.md", "references/official-rules.md",
                "references/prompt-framework.md", "scripts/validate_h3_prompt.py"]:
        assert (H3_DIR / rel).exists(), rel
    assert "复核" in (H3_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_validator_runs():
    r = subprocess.run([sys.executable, str(H3_DIR / "scripts/validate_h3_prompt.py"),
                        "--help"], capture_output=True, timeout=15)
    assert r.returncode == 0
