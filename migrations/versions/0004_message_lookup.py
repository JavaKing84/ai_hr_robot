"""messages 补一条 external_id 单列索引：修掉幂等判重全表扫

Revision ID: 0004_message_lookup
Revises: 0003_hardening
Create Date: 2026-11-14

**为什么 0003 之后还要再补一条**

0003 给 ``messages`` 加了部分唯一索引 ``ux_messages_channel_external``::

    UNIQUE (channel, external_id) WHERE external_id <> '' AND status IN ('received','sent','mocked')

它能挡住重复回调，但**查不动**。两条热路径都只拿 ``external_id`` 定位：

* ``MessageRepository.find_delivered()`` —— 每发一封面试通知都要先查一次幂等键；
* ``MessageRepository.seen_inbound()`` —— 每条入站消息（飞书回调 / IMAP 拉信）都要查一次。

查询里没有 ``status`` 条件，PostgreSQL 无法证明命中行落在部分索引里，
于是退化成 Seq Scan。在 6 万条 messages 的压测库上实测：单次 **7.5 ms**，
换成索引后 **0.05 ms**（Index Scan，150 倍）。这条路径是「每封信、每条消息各一次」，
量级一上来就会变成最贵的查询。

这里补一条**不带 status 谓词**的单列索引供查询用；唯一性仍由 0003 那条部分唯一索引
保证，两者职责分开，互不冲突。
"""
from __future__ import annotations

from alembic import op

revision = "0004_message_lookup"
down_revision = "0003_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_messages_external_id
        ON messages (external_id)
        WHERE external_id <> ''
        """
    )
    op.execute("ANALYZE messages")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_external_id")
