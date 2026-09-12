# comic_studio/engine/comicexport.py
"""漫画成书导出（小说转漫画 Task 5，2026-09-12）：pages/page_NNN.png → PDF / 长图。

PDF 走 Pillow（可选依赖，`pdf` extra——同 asr/footer 约定：缺失显式报错给
安装指引，不静默）；长图走 ffmpeg vstack（复用 merge.ffmpeg_bin，
imageio-ffmpeg 自带，无需额外安装）。输出固定落 projects/<slug>/output/，
同名覆盖（导出是同一份成品的重生成，非 epNNN 那类归档产物）。

页清单以磁盘 pages/ 为准、按页号升序；有分镜行的项目再按分镜表过滤
（disabled=1 与已删镜的残页不进导出——与「无效镜不进合成」纪律同口径）。
"""
import re
import subprocess
from pathlib import Path

from .logbus import emit as emit_log
from .paths import data_to_abs
from .projects import get_project

_PAGE_RE = re.compile(r"^page_(\d+)\.png$")


def comic_pages(db, data_dir: Path, project_id: int) -> list[Path]:
    """按页号升序列出可导出漫画页（pages/ 缺失 → 空清单）。"""
    proj = get_project(db, project_id)
    if proj is None:
        raise ValueError(f"项目不存在（{project_id}）")
    pages_dir = data_to_abs(data_dir, f"projects/{proj['slug']}/pages")
    if not pages_dir.is_dir():
        return []
    found = sorted((int(m.group(1)), p) for p in pages_dir.iterdir()
                   if (m := _PAGE_RE.match(p.name)))
    rows = db.connect().execute(
        "SELECT seq, disabled FROM shots WHERE project_id=?",
        (project_id,)).fetchall()
    if rows:  # 有分镜：过滤 disabled/已删 seq 的残页；无分镜行（手工放页）→ 全量
        alive = {r["seq"] for r in rows if not r["disabled"]}
        found = [(s, p) for s, p in found if s in alive]
    return [p for _, p in found]


def _output_dir(data_dir: Path, slug: str) -> Path:
    out = data_to_abs(data_dir, f"projects/{slug}/output")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _project_pages(db, data_dir: Path, project_id: int):
    """项目行 + 页清单；无页显式 ValueError（路由 422 语义）。

    页数检查在 Pillow import 之前——无页报错不依赖可选依赖。"""
    pages = comic_pages(db, data_dir, project_id)
    if not pages:
        raise ValueError("无已生成漫画页（pages/ 为空）——请先完成逐页生成再导出")
    return get_project(db, project_id), pages


def export_comic_pdf(db, data_dir: Path, project_id: int) -> Path:
    """pages → output/comic.pdf（Pillow 多页合成，可选依赖）。

    单页损坏跳过并 warn（透明不中断）；全部损坏才报错。"""
    proj, pages = _project_pages(db, data_dir, project_id)
    try:
        from PIL import Image
    except ImportError:
        emit_log(db, "comic", "warn",
                 "PDF 导出需要 Pillow，当前环境未安装", project_id=project_id)
        raise ValueError(
            "PDF 导出需要 Pillow：请安装（WSL：.venv/bin/pip install '.[pdf]'；"
            "Windows：.venv-win/Scripts/pip.exe install '.[pdf]'）") from None
    imgs = []
    for p in pages:
        try:
            imgs.append(Image.open(p).convert("RGB"))
        except Exception:
            emit_log(db, "comic", "warn",
                     f"漫画页读取失败，导出已跳过：{p.name}", project_id=project_id)
    if not imgs:
        raise ValueError("漫画页全部无法读取（图片损坏？）")
    out = _output_dir(data_dir, proj["slug"]) / "comic.pdf"
    imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:],
                 resolution=150.0)
    emit_log(db, "comic", "info",
             f"PDF 导出完成：{len(imgs)}/{len(pages)} 页 → output/comic.pdf",
             project_id=project_id)
    return out


def export_comic_strip(db, data_dir: Path, project_id: int) -> Path:
    """pages → output/comic_strip.png（ffmpeg vstack 竖排长图）。

    各页须同宽（同一项目同一尺寸预设保证）；宽不一致时 ffmpeg 报错，
    stderr 尾段入异常消息供排障（encoding='utf-8', errors='replace'——GBK 判例）。"""
    proj, pages = _project_pages(db, data_dir, project_id)
    out = _output_dir(data_dir, proj["slug"]) / "comic_strip.png"
    from .merge import ffmpeg_bin
    args = [ffmpeg_bin(), "-y"]
    for p in pages:
        args += ["-i", str(p)]
    n = len(pages)
    if n == 1:
        args += ["-frames:v", "1", str(out)]
    else:
        # format=rgba 统一像素格式（t2i RGB 与 footer 后处理 RGBA 混存也能拼）；
        # vstack 只要求同宽，页高不同亦可
        chains = ";".join(f"[{i}:v]format=rgba[s{i}]" for i in range(n))
        sinks = "".join(f"[s{i}]" for i in range(n))
        args += ["-filter_complex", f"{chains};{sinks}vstack=inputs={n}[v]",
                 "-map", "[v]", "-frames:v", "1", str(out)]
    try:
        subprocess.run(args, check=True, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=600)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip()[-400:]
        raise RuntimeError(f"长图拼接失败（ffmpeg）：{tail}") from None
    emit_log(db, "comic", "info",
             f"长图导出完成：{n} 页 → output/comic_strip.png",
             project_id=project_id)
    return out
