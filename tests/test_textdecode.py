# tests/test_textdecode.py — 上传文本编码自动检测（2026-09-16 GBK 直传需求）
import pytest

from comic_studio.engine.textdecode import decode_text_bytes


def test_utf8_passthrough():
    assert decode_text_bytes("中文正文".encode("utf-8")) == "中文正文"


def test_empty_bytes():
    assert decode_text_bytes(b"") == ""


def test_utf8_bom_stripped():
    """带 BOM 的 UTF-8（Windows 记事本常见）解码后不应残留 ﻿。"""
    out = decode_text_bytes("开头".encode("utf-8-sig"))
    assert out == "开头" and not out.startswith("﻿")


def test_gbk_decoded():
    assert decode_text_bytes("中文GBK文件".encode("gbk")) == "中文GBK文件"


def test_gb2312_decoded():
    assert decode_text_bytes("简体旧编码".encode("gb2312")) == "简体旧编码"


def test_gb18030_four_byte_decoded():
    """GB18030 四字节区（GBK 不含）也应解出。"""
    assert decode_text_bytes("𠂊".encode("gb18030")) == "𠂊"


def test_utf16_le_bom_decoded():
    """UTF-16 带 BOM（Windows「Unicode」存档）按 BOM 解。"""
    assert decode_text_bytes("中文正文".encode("utf-16")) == "中文正文"


def test_utf32_bom_not_misread_as_utf16():
    """UTF-32 LE BOM 以 UTF-16 LE BOM 为前缀——检测顺序必须 UTF-32 在前，
    否则被 utf-16 吃掉 BOM 后剩奇数字节报错。"""
    assert decode_text_bytes("中".encode("utf-32")) == "中"


def test_binary_rejected():
    with pytest.raises(ValueError) as ei:
        decode_text_bytes(b"\x80\x81\x82\xff")
    assert "UTF-8" in str(ei.value)  # 报错文案列支持清单


def test_truncated_utf16_bom_rejected():
    """UTF-16 BOM 后字节截断（真坏文件）→ 全链失败报 ValueError。"""
    with pytest.raises(ValueError):
        decode_text_bytes(b"\xff\xfe\xd6")
