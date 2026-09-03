"""start.bat 启动脚护栏（2026-09-02 事故）：重启服务不得误杀 ComfyUI。

事故：taskkill /f /im python.exe 按镜像名全域杀——ComfyUI（python 进程）
每次重启 comic_studio 都被连带杀掉。修复=只杀监听 8190 端口的进程。
bat 无单测框架，用静态断言防回归（机械校验文化，同 prompt heal 思路）。"""
from pathlib import Path

START_BAT = Path(__file__).resolve().parent.parent / "start.bat"


def test_bat_no_broad_image_kill():
    """禁止按镜像名全域杀进程（python.exe 会命中 ComfyUI/LM Studio 后端等）。"""
    text = START_BAT.read_text(encoding="utf-8", errors="replace").lower()
    assert "taskkill /f /im python.exe" not in text
    assert "taskkill /f /im uvicorn.exe" not in text


def test_bat_kills_by_port_8190_only():
    """旧服务清理必须按端口 8190 精确定位（Get-NetTCPConnection OwningProcess）。"""
    text = START_BAT.read_text(encoding="utf-8", errors="replace").lower()
    assert "localport 8190" in text
    assert "owningprocess" in text


def test_prod_bat_no_reload_dev_bat_has_reload():
    """start-prod 无 --reload（挂机长批次不被文件改动打断）；start.bat 保留（开发）。"""
    prod = (START_BAT.parent / "start-prod.bat").read_text(encoding="utf-8",
                                                           errors="replace").lower()
    assert "--reload" not in prod
    assert "localport 8190" in prod  # 端口清理与开发版一致
    assert "--reload" in START_BAT.read_text(encoding="utf-8", errors="replace").lower()
