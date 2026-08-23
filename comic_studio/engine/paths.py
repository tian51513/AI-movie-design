"""跨环境路径可移植性（WSL ↔ Windows 原生共享同一 data/ 目录）。

DB 中一律存相对 data 根的 POSIX 风格路径（如 "projects/斗破/novel.txt"），
读取时与当前 data 根 join。历史遗留的绝对路径值原样放行（仅在创建它的
环境内有效）——本约定引入时尚无真实数据，不做迁移。
"""
from pathlib import Path, PurePath, PureWindowsPath


def rel_to_data(data_dir: Path | str, abs_path: Path | str) -> str:
    """绝对路径 → 相对 data 根的 POSIX 字符串（DB 存储格式）。"""
    rel = Path(abs_path).relative_to(Path(data_dir))
    return rel.as_posix()


def data_to_abs(data_dir: Path | str, stored: str) -> Path:
    """DB 存储值 → 当前环境绝对路径；绝对路径值（遗留数据）原样返回。

    Windows 盘符路径（E:\\...）在 Linux 上不是 PosixPath 绝对路径，
    需按 Windows 语义额外判定，避免被误拼到 data 根下。
    """
    if PurePath(stored).is_absolute() or PureWindowsPath(stored).is_absolute():
        return Path(stored)
    return Path(data_dir) / Path(stored)
