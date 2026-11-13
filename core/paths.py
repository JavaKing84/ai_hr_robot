"""路径解析：数据库里存的是仓库相对路径，读盘时统一在这里还原成绝对路径。"""

from __future__ import annotations

from pathlib import Path

from config.settings import BASE_DIR


def resolve_resume_path(raw: str | None) -> Path | None:
    """简历路径 → 可直接打开的文件路径；为空返回 None。

    兼容三种历史写法：
    1. 仓库相对路径 ``var/resumes/xxx.pdf``（当前写法）；
    2. 旧版本绝对路径 ``D:\\Desktop\\hr-ai-robot1-develop\\var\\resumes\\xxx.pdf``
       ——项目搬过家，这个绝对路径必然失效，**但它指向的文件名仍然有效**，
       所以按文件名回落到当前仓库的 ``var/resumes/``（2026-11-14 补：原来对绝对
       路径直接 return，导致「文件其实在、页面却显示原件丢失」）；
    3. 仅文件名 ``xxx.pdf``。
    """
    text = (raw or "").strip()
    if not text:
        return None

    path = Path(text)
    direct = path if path.is_absolute() else BASE_DIR / path
    if direct.exists():
        return direct

    # 回落到仓库内的 var/resumes/<文件名>（原件被搬过家 / 只存了文件名）
    fallback = BASE_DIR / "var" / "resumes" / path.name
    if fallback.exists():
        return fallback

    return direct


__all__ = ["resolve_resume_path"]
