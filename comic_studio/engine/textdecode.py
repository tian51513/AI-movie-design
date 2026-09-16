"""上传文本编码自动检测（2026-09-16 用户需求：GBK 等非 UTF-8 文件直传）。

检测链（首成即返）：
1. BOM 识别——UTF-8/UTF-16/UTF-32 按 BOM 解（codec 自剥 BOM；Windows 记事本
   「ANSI/Unicode」存档的常态）。UTF-32 LE BOM 以 UTF-16 LE BOM 为前缀，
   检测表必须 UTF-32 在前。
2. UTF-8 严格试解——存量 UTF-8 文件行为不变。
3. GB18030 试解——GBK/GB2312 的官方超集，一个覆盖三个。

不猜的：无 BOM UTF-16（Windows 存 UTF-16 必带 BOM，罕见）；Big5/Shift-JIS
等（GB18030 解出的是乱码而非报错，猜错比报错糟）——统一报错引导手动转 UTF-8。
"""
import codecs

# 顺序敏感：UTF-32 在 UTF-16 前（BOM 前缀包含关系）
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_ERROR_MSG = "无法识别文件编码（支持 UTF-8/GBK/GB18030/UTF-16），请转换后重新上传"


def decode_text_bytes(data: bytes) -> str:
    """字节流 → 文本。全候选失败抛 ValueError（消息含支持清单，路由层转 422）。"""
    candidates: list[str] = []
    for bom, codec in _BOMS:
        if data.startswith(bom):
            candidates.append(codec)
            break  # BOM 即声明，不再堆无 BOM 候选
    candidates += ["utf-8", "gb18030"]
    for codec in candidates:
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue  # BOM 撞错（如截断的 UTF-16）也继续往后试
    raise ValueError(_ERROR_MSG)
