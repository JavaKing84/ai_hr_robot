"""邮件线程重建：只保留 JWZ 线程算法里的最小部分——头部解析 + 树构建。

参考 ``emersion/python-emailthreads``（MIT，低 star，按 SKILL §4.10 仅作算法参考）
与 Sentry 的 ``GroupEmailThread`` 设计：持久化原始 ``Message-ID``，靠
``In-Reply-To`` / ``References`` 把回复挂回原线程。这里不引入任何第三方运行时依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

_MESSAGE_ID_RE = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")


@dataclass(frozen=True)
class MessageThreadHeaders:
    """线程判定所需的三个头部字段。"""

    message_id: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()

    @classmethod
    def from_raw(
        cls,
        message_id: str | None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> "MessageThreadHeaders | None":
        normalized = normalize_message_id(message_id)
        if not normalized:
            return None
        return cls(
            message_id=normalized,
            in_reply_to=normalize_message_id(in_reply_to),
            references=tuple(parse_reference_header(references)),
        )


def normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("<>").strip()
    return value or None


def parse_reference_header(value: str | None) -> list[str]:
    """从 ``References`` / ``In-Reply-To`` 头部取出全部 Message-ID（保持原顺序）。"""
    if not value:
        return []
    ids = [normalize_message_id(match) for match in _MESSAGE_ID_RE.findall(value)]
    cleaned = [i for i in ids if i]
    if cleaned:
        return cleaned
    # 容错：有些客户端不加尖括号
    return [part for part in (normalize_message_id(p) for p in value.split()) if part]


def _parent_of(headers: MessageThreadHeaders, known_ids: Mapping[str, object]) -> str | None:
    if headers.in_reply_to and headers.in_reply_to in known_ids:
        return headers.in_reply_to
    for reference in reversed(headers.references):
        if reference in known_ids:
            return reference
    return None


def root_of(headers_by_id: Mapping[str, MessageThreadHeaders | None], start: str) -> str:
    """沿父链上溯到根，带环路保护（恶意/损坏头部不至于死循环）。"""
    known = {k for k, v in headers_by_id.items() if v is not None}
    seen: set[str] = set()
    current = start
    while current not in seen:
        seen.add(current)
        headers = headers_by_id.get(current)
        if headers is None:
            break
        parent = _parent_of(headers, known)
        if parent is None or parent == current:
            break
        current = parent
    return current


def build_threads(messages: Sequence[MessageThreadHeaders]) -> dict[str, list[str]]:
    """把一批邮件按线程分组：``{root_message_id: [message_id, ...]}``。"""
    by_id: dict[str, MessageThreadHeaders | None] = {m.message_id: m for m in messages}
    threads: dict[str, list[str]] = {}
    for message in messages:
        root = root_of(by_id, message.message_id)
        threads.setdefault(root, []).append(message.message_id)
    return threads


@dataclass
class ThreadNode:
    message_id: str
    children: list["ThreadNode"] = field(default_factory=list)


def build_tree(messages: Sequence[MessageThreadHeaders]) -> list[ThreadNode]:
    """构建线程树（同一根下按引用关系挂子节点），供需要层级展示的调用方使用。"""
    by_id: dict[str, MessageThreadHeaders | None] = {m.message_id: m for m in messages}
    nodes = {m.message_id: ThreadNode(m.message_id) for m in messages}
    roots: list[ThreadNode] = []
    for message in messages:
        parent = _parent_of(message, by_id)
        if parent and parent in nodes:
            nodes[parent].children.append(nodes[message.message_id])
        else:
            roots.append(nodes[message.message_id])
    return roots


def flatten_in_order(messages: Iterable[MessageThreadHeaders]) -> list[str]:
    """按出现顺序展开（调用方通常已按收信时间排序）。"""
    return [m.message_id for m in messages]
