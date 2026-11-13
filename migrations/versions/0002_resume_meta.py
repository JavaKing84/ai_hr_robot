"""candidates 增加简历文件元信息：内容指纹 / 字节数 / 解析引擎

Revision ID: 0002_resume_meta
Revises: 0001_initial
Create Date: 2026-11-14

resume_path 同时改为仓库相对路径（var/resumes/xxx），历史绝对路径由
数据修复脚本回填，见 project-intel 的记录。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_resume_meta"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidates", sa.Column("resume_sha256", sa.String(64), nullable=False, server_default=""))
    op.add_column("candidates", sa.Column("resume_size_bytes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("candidates", sa.Column("parse_engine", sa.String(32), nullable=False, server_default=""))
    op.create_index("ix_candidates_resume_sha256", "candidates", ["resume_sha256"])


def downgrade() -> None:
    op.drop_index("ix_candidates_resume_sha256", table_name="candidates")
    op.drop_column("candidates", "parse_engine")
    op.drop_column("candidates", "resume_size_bytes")
    op.drop_column("candidates", "resume_sha256")
