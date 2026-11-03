"""Repository 基类：只做单聚合根的数据访问，禁止在这里调用任何外部 API。"""

from __future__ import annotations

from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, obj_id: int) -> T | None:
        return await self.session.get(self.model, obj_id)

    async def add(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def add_all(self, objs: Sequence[T]) -> Sequence[T]:
        self.session.add_all(list(objs))
        await self.session.flush()
        return objs

    async def count(self, stmt: Select[Any]) -> int:
        result = await self.session.execute(select(func.count()).select_from(stmt.subquery()))
        return int(result.scalar_one())
