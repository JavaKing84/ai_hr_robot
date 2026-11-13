"""数据库加固：jsonb + timestamptz + 消息幂等唯一索引 + 索引按真实查询重排

Revision ID: 0003_hardening
Revises: 0002_resume_meta
Create Date: 2026-11-14

四件事，逐条对应「待优化清单」的 P1：

1. **JSON → JSONB**：``json`` 存的是文本，既不能建 GIN 也不能用 ``@>`` / ``?`` 运算符。
   转成 ``jsonb`` 后，按技能/分数明细检索才有索引可用（见第 4 条的 GIN）。
2. **timestamp → timestamptz**：原来时间列没有时区，库里只是一串「墙上时间」。
   历史值按 **UTC** 解释（应用层一直存 naive UTC），转换后跨时区不会算错。
3. **messages 幂等唯一索引**：``(channel, external_id)`` 部分唯一，只约束
   ``status IN ('received','sent','mocked')`` 的成功行，因此既挡住重复回调，
   又不破坏「失败重投留 failed 行 / 人工确认闸门留 pending_approval 行」的既有设计。
   建索引前先清理历史重复行（保留 id 最小的那条）。
4. **索引重排**：删掉被复合索引覆盖的单列索引（纯写放大），补上真实查询用得到的
   复合索引、部分索引与 GIN 索引。

降级脚本会把结构还原到 0002（数据不会回到 json，类型转换不可逆，这一点在 downgrade 里注明）。
"""
from __future__ import annotations

from alembic import op

revision = "0003_hardening"
down_revision = "0002_resume_meta"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------- 第 1 步：jsonb
#: (表, 列, JSON 默认值表达式) —— 有默认值的列需要一并改成 jsonb 字面量
_JSON_COLUMNS: list[tuple[str, str, str]] = [
    ("positions", "keywords", "'[]'::jsonb"),
    ("positions", "screening", "'{}'::jsonb"),
    ("candidates", "skills", "'[]'::jsonb"),
    ("candidates", "project_highlights", "'[]'::jsonb"),
    ("candidates", "structured", "'{}'::jsonb"),
    ("candidates", "score_detail", "'{}'::jsonb"),
    ("candidates", "hard_failed", "'[]'::jsonb"),
    ("messages", "parsed", "'{}'::jsonb"),
    ("messages", "raw", "'{}'::jsonb"),
    ("email_threads", "message_ids", "'[]'::jsonb"),
    ("email_threads", "meta", "'{}'::jsonb"),
    ("events", "detail", "'{}'::jsonb"),
]

# ------------------------------------------------------------ 第 2 步：timestamptz
_TIME_COLUMNS: list[tuple[str, str]] = [
    ("positions", "created_at"),
    ("positions", "updated_at"),
    ("supervisors", "created_at"),
    ("supervisors", "updated_at"),
    ("candidates", "created_at"),
    ("candidates", "updated_at"),
    ("candidates", "first_seen_at"),
    ("candidates", "last_seen_at"),
    ("candidates", "interview_time"),
    ("messages", "created_at"),
    ("messages", "updated_at"),
    ("messages", "sent_at"),
    ("messages", "received_at"),
    ("email_threads", "created_at"),
    ("email_threads", "updated_at"),
    ("email_threads", "last_message_at"),
    ("events", "created_at"),
    ("events", "updated_at"),
    ("events", "occurred_at"),
]


def upgrade() -> None:
    # 0) 0002 加的三列是 ``NOT NULL`` 却没有数据库默认值：ORM 插入没问题（Python 默认值会
    #    一起发过去），但裸 SQL / 数据搬迁 / 未来新增采集脚本一旦漏写这三列就直接报错。
    #    这里补上 server_default，让「不带这一列」也能落库。
    op.execute("ALTER TABLE candidates ALTER COLUMN resume_sha256 SET DEFAULT ''")
    op.execute("ALTER TABLE candidates ALTER COLUMN resume_size_bytes SET DEFAULT '0'")
    op.execute("ALTER TABLE candidates ALTER COLUMN parse_engine SET DEFAULT ''")

    # 1) json -> jsonb（列上若有 DEFAULT，先摘掉再挂 jsonb 默认值，避免 'json' 默认值残留）
    for table, column, default in _JSON_COLUMNS:
        op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE jsonb USING "{column}"::jsonb'
        )
        op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default}')

    # 2) timestamp -> timestamptz（历史值是 naive UTC，用 AT TIME ZONE 'UTC' 补上时区）
    for table, column in _TIME_COLUMNS:
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
            f'TYPE timestamptz USING "{column}" AT TIME ZONE \'UTC\''
        )

    # 3) 幂等唯一索引：先清历史重复行（同一 channel + external_id 的成功记录只留最早一条）
    op.execute(
        """
        DELETE FROM messages a
        USING messages b
        WHERE a.id > b.id
          AND a.channel = b.channel
          AND a.external_id = b.external_id
          AND a.external_id <> ''
          AND a.status IN ('received', 'sent', 'mocked')
          AND b.status IN ('received', 'sent', 'mocked')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_messages_channel_external
        ON messages (channel, external_id)
        WHERE external_id <> '' AND status IN ('received', 'sent', 'mocked')
        """
    )

    # 4) 索引重排：先删被复合索引覆盖 / 从未参与查询的单列索引
    for name in (
        "ix_candidates_position_id",
        "ix_candidates_status",
        "ix_candidates_position_code",
        "ix_candidates_cycle_id",
        "ix_messages_channel",
        "ix_messages_direction",
        "ix_messages_external_id",
        "ix_messages_position_id",
        "ix_messages_supervisor_id",
        "ix_events_action",
        "ix_events_object_id",
        "ix_email_threads_kind",
        "ix_email_threads_position_id",
        "ix_email_threads_candidate_id",
        "ix_positions_supervisor_open_id",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")

    # 4.1 candidates
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_status_position_id "
        "ON candidates (status, position_code, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_position_status_score "
        "ON candidates (position_id, status, score)"
    )
    # 按轮次取候选人：``WHERE cycle_id = ? ORDER BY id`` 走这条复合索引，省掉排序
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_cycle_id ON candidates (cycle_id, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_interview_time "
        "ON candidates (interview_time) WHERE interview_time IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_candidates_skills_gin "
        "ON candidates USING gin (skills jsonb_path_ops)"
    )

    # 4.2 messages
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_channel_direction_id "
        "ON messages (channel, direction, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_retry ON messages (channel, direction, status) "
        "WHERE status IN ('failed', 'pending_approval')"
    )

    # 4.3 events
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_object ON events (object_type, object_id, id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_events_action_id ON events (action, id)")

    # 4.4 email_threads
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_threads_candidate_kind "
        "ON email_threads (candidate_id, kind)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_threads_message_ids_gin "
        "ON email_threads USING gin (message_ids jsonb_path_ops)"
    )

    # 4.5 positions
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_positions_supervisor_active "
        "ON positions (supervisor_open_id, active) WHERE active IS TRUE"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_positions_department ON positions (department)")

    # 让统计信息立刻准确（刚大批量改过类型）
    op.execute("ANALYZE")


def downgrade() -> None:
    """结构回退到 0002。

    注意：jsonb → json、timestamptz → timestamp 都是**有损**回退
    （jsonb 会丢掉键顺序与重复键，timestamptz 会丢掉时区偏移），
    只在确有需要时使用；正常升级不需要执行本函数。
    """
    op.execute("DROP INDEX IF EXISTS ux_messages_channel_external")
    for name in (
        "ix_candidates_status_position_id",
        "ix_candidates_position_status_score",
        "ix_candidates_cycle_id",
        "ix_candidates_interview_time",
        "ix_candidates_skills_gin",
        "ix_messages_channel_direction_id",
        "ix_messages_retry",
        "ix_events_object",
        "ix_events_action_id",
        "ix_email_threads_candidate_kind",
        "ix_email_threads_message_ids_gin",
        "ix_positions_supervisor_active",
        "ix_positions_department",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")

    for table, column, _ in _JSON_COLUMNS:
        op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE json USING "{column}"::json'
        )
    for table, column in _TIME_COLUMNS:
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
            f"TYPE timestamp USING \"{column}\" AT TIME ZONE 'UTC'"
        )

    op.execute("CREATE INDEX ix_candidates_position_id ON candidates (position_id)")
    op.execute("CREATE INDEX ix_candidates_status ON candidates (status)")
    op.execute("CREATE INDEX ix_candidates_position_code ON candidates (position_code)")
    op.execute("CREATE INDEX ix_candidates_cycle_id ON candidates (cycle_id)")
    op.execute("CREATE INDEX ix_messages_channel ON messages (channel)")
    op.execute("CREATE INDEX ix_messages_direction ON messages (direction)")
    op.execute("CREATE INDEX ix_messages_external_id ON messages (external_id)")
    op.execute("CREATE INDEX ix_events_action ON events (action)")
    op.execute("CREATE INDEX ix_events_object_id ON events (object_id)")
    op.execute("CREATE INDEX ix_positions_supervisor_open_id ON positions (supervisor_open_id)")
