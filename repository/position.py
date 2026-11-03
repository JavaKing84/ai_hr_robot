"""职位 + 主管的数据访问：从 YAML 配置同步运行期快照。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model.position import Position
from model.supervisor import Supervisor
from repository.base import BaseRepository
from schema.config import CompanyConfig


class PositionRepository(BaseRepository[Position]):
    model = Position

    async def get_by_code(self, code: str) -> Position | None:
        result = await self.session.execute(select(Position).where(Position.code == code))
        return result.scalar_one_or_none()

    async def list_active(self) -> list[Position]:
        result = await self.session.execute(
            select(Position).where(Position.active.is_(True)).order_by(Position.id)
        )
        return list(result.scalars().all())

    async def list_by_open_ids(self, open_ids: list[str]) -> list[Position]:
        if not open_ids:
            return []
        result = await self.session.execute(
            select(Position).where(Position.supervisor_open_id.in_(open_ids), Position.active.is_(True))
        )
        return list(result.scalars().all())

    async def sync_from_config(self, config: CompanyConfig) -> dict[str, int]:
        """把 YAML 的职位与主管写进数据库（upsert），返回变更统计。

        每轮任务开始时调用一次 —— 这就是「HR 改了配置，下一轮生效」的实现点。
        """
        created = updated = supervisors_created = supervisors_updated = 0
        deactivated = 0
        seen_codes: set[str] = set()

        for department in config.departments:
            supervisor_cfg = department.supervisor
            existing_supervisor = await self.session.execute(
                select(Supervisor).where(Supervisor.feishu_open_id == supervisor_cfg.feishu_open_id)
            )
            supervisor = existing_supervisor.scalar_one_or_none()
            if supervisor is None:
                supervisor = Supervisor(
                    name=supervisor_cfg.name,
                    title=supervisor_cfg.title,
                    department=department.name,
                    feishu_open_id=supervisor_cfg.feishu_open_id,
                    email=supervisor_cfg.email,
                )
                await self.add(supervisor)
                supervisors_created += 1
            else:
                supervisor.name = supervisor_cfg.name
                supervisor.title = supervisor_cfg.title
                supervisor.department = department.name
                supervisor.email = supervisor_cfg.email
                supervisors_updated += 1

            for position_cfg in department.positions:
                seen_codes.add(position_cfg.code)
                position = await self.get_by_code(position_cfg.code)
                payload = {
                    "title": position_cfg.title,
                    "department": department.name,
                    "headcount": position_cfg.headcount,
                    "description": position_cfg.description,
                    "keywords": position_cfg.keywords,
                    "threshold": position_cfg.screening.threshold,
                    "rule_version": position_cfg.screening.rule_version,
                    "screening": position_cfg.screening.model_dump(mode="json"),
                    "supervisor_name": supervisor_cfg.name,
                    "supervisor_title": supervisor_cfg.title,
                    "supervisor_open_id": supervisor_cfg.feishu_open_id,
                    "supervisor_email": supervisor_cfg.email,
                    "active": True,
                }
                if position is None:
                    position = Position(code=position_cfg.code, **payload)
                    await self.add(position)
                    created += 1
                else:
                    for key, value in payload.items():
                        setattr(position, key, value)
                    updated += 1

        # 配置中已删除的职位：置为 inactive，不物理删除（保留历史简历关联）
        result = await self.session.execute(select(Position).where(Position.active.is_(True)))
        for position in result.scalars().all():
            if position.code not in seen_codes:
                position.active = False
                deactivated += 1

        await self.session.flush()
        return {
            "positions_created": created,
            "positions_updated": updated,
            "positions_deactivated": deactivated,
            "supervisors_created": supervisors_created,
            "supervisors_updated": supervisors_updated,
        }


class SupervisorRepository(BaseRepository[Supervisor]):
    model = Supervisor

    async def get_by_open_id(self, open_id: str) -> Supervisor | None:
        result = await self.session.execute(
            select(Supervisor).where(Supervisor.feishu_open_id == open_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Supervisor | None:
        if not email:
            return None
        result = await self.session.execute(
            select(Supervisor).where(Supervisor.email.ilike(email.strip().lower()))
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Supervisor]:
        result = await self.session.execute(select(Supervisor).order_by(Supervisor.id))
        return list(result.scalars().all())


def build_repositories(session: AsyncSession) -> tuple[PositionRepository, SupervisorRepository]:
    return PositionRepository(session), SupervisorRepository(session)
