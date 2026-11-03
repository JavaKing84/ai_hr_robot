"""提示词包：筛选模板（screening.md）+ 各类抽取提示（extraction.py）。"""

from pathlib import Path

SCREENING_TEMPLATE_PATH = Path(__file__).with_name("screening.md")


def load_screening_template() -> str:
    """读取 HR 可编辑的筛选模板原文（用于 /health 展示或审计，不参与运行逻辑）。"""
    return SCREENING_TEMPLATE_PATH.read_text(encoding="utf-8")
