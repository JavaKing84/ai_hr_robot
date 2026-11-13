"""初始 schema：6 张业务表 + 外键 + 唯一索引

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-13

建表顺序遵循外键依赖：positions / supervisors（被引用方）先建，
candidates 次之，messages / email_threads 引用前三者，events 最后。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- positions ----
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("department", sa.String(64), nullable=False, server_default=""),
        sa.Column("headcount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("keywords", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="60.0"),
        sa.Column("rule_version", sa.String(32), nullable=False, server_default="v1"),
        sa.Column("screening", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("supervisor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("supervisor_title", sa.String(32), nullable=False, server_default=""),
        sa.Column("supervisor_open_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("supervisor_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_positions_code", "positions", ["code"], unique=True)
    op.create_index("ix_positions_supervisor_open_id", "positions", ["supervisor_open_id"])

    # ---- supervisors ----
    op.create_table(
        "supervisors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("title", sa.String(32), nullable=False, server_default=""),
        sa.Column("department", sa.String(64), nullable=False, server_default=""),
        sa.Column("feishu_open_id", sa.String(128), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_supervisors_department", "supervisors", ["department"])
    op.create_index("ix_supervisors_feishu_open_id", "supervisors", ["feishu_open_id"], unique=True)

    # ---- candidates ----
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, server_default=""),
        sa.Column("email", sa.String(255), nullable=False, server_default=""),
        sa.Column("phone", sa.String(32), nullable=False, server_default=""),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("position_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("department", sa.String(64), nullable=False, server_default=""),
        sa.Column("education_level", sa.String(16), nullable=False, server_default=""),
        sa.Column("school", sa.String(128), nullable=False, server_default=""),
        sa.Column("major", sa.String(128), nullable=False, server_default=""),
        sa.Column("years_experience", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("skills", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("project_highlights", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("structured", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resume_path", sa.String(512), nullable=False, server_default=""),
        sa.Column("resume_filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("resume_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(32), nullable=False, server_default="imap"),
        sa.Column("score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("score_detail", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("hard_failed", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="received"),
        sa.Column("is_duplicate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duplicate_of_id", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("interview_time", sa.DateTime(), nullable=True),
        sa.Column("interview_location", sa.String(255), nullable=False, server_default=""),
        sa.Column("cycle_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_candidates_name", "candidates", ["name"])
    op.create_index("ix_candidates_email", "candidates", ["email"])
    op.create_index("ix_candidates_phone", "candidates", ["phone"])
    op.create_index("ix_candidates_position_id", "candidates", ["position_id"])
    op.create_index("ix_candidates_position_code", "candidates", ["position_code"])
    op.create_index("ix_candidates_status", "candidates", ["status"])
    op.create_index("ix_candidates_cycle_id", "candidates", ["cycle_id"])

    # ---- messages ----
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("sender", sa.String(255), nullable=False, server_default=""),
        sa.Column("recipient", sa.String(255), nullable=False, server_default=""),
        sa.Column("subject", sa.String(512), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("supervisor_id", sa.Integer(), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("thread_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("parsed", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="sent"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supervisor_id"], ["supervisors.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_messages_channel", "messages", ["channel"])
    op.create_index("ix_messages_direction", "messages", ["direction"])
    op.create_index("ix_messages_candidate_id", "messages", ["candidate_id"])
    op.create_index("ix_messages_position_id", "messages", ["position_id"])
    op.create_index("ix_messages_supervisor_id", "messages", ["supervisor_id"])
    op.create_index("ix_messages_external_id", "messages", ["external_id"])
    op.create_index("ix_messages_thread_key", "messages", ["thread_key"])

    # ---- email_threads ----
    op.create_table(
        "email_threads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="interview_notice"),
        sa.Column("root_message_id", sa.String(255), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("message_id_index", sa.String(1024), nullable=False, server_default=""),
        sa.Column("subject", sa.String(512), nullable=False, server_default=""),
        sa.Column("reply_address", sa.String(255), nullable=False, server_default=""),
        sa.Column("counterpart_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("supervisor_id", sa.Integer(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["position_id"], ["positions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["supervisor_id"], ["supervisors.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_email_threads_token", "email_threads", ["token"], unique=True)
    op.create_index("ix_email_threads_kind", "email_threads", ["kind"])
    op.create_index("ix_email_threads_root_message_id", "email_threads", ["root_message_id"])
    op.create_index("ix_email_threads_counterpart_email", "email_threads", ["counterpart_email"])
    op.create_index("ix_email_threads_candidate_id", "email_threads", ["candidate_id"])
    op.create_index("ix_email_threads_position_id", "email_threads", ["position_id"])
    op.create_index("ix_email_threads_supervisor_id", "email_threads", ["supervisor_id"])

    # ---- events ----
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False, server_default="robot"),
        sa.Column("object_type", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("object_id", sa.BigInteger(), nullable=True),
        sa.Column("from_state", sa.String(32), nullable=False, server_default=""),
        sa.Column("to_state", sa.String(32), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_events_action", "events", ["action"])
    op.create_index("ix_events_object_id", "events", ["object_id"])
    op.create_index("ix_events_occurred_at", "events", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("email_threads")
    op.drop_table("messages")
    op.drop_table("candidates")
    op.drop_table("supervisors")
    op.drop_table("positions")
