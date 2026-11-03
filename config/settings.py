"""应用配置：全部来自环境变量 / `.env`，不含任何硬编码凭证。

`config/positions.yaml` 承载业务配置（部门 → 职位 → 主管、筛选规则），
本文件只负责路径、外部系统模式（mock/live）与运行参数。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema.config import CompanyConfig

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 运行
    app_name: str = "ai-hr-robot"
    environment: str = "dev"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = "Asia/Shanghai"

    # 数据库
    database_url: str = "sqlite+aiosqlite:///./var/hr_robot.db"
    db_echo: bool = False

    # 路径
    config_path: Path = CONFIG_DIR / "positions.yaml"
    templates_dir: Path = CONFIG_DIR / "templates"
    var_dir: Path = BASE_DIR / "var"

    # 飞书
    feishu_mode: Literal["mock", "live"] = "mock"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # LLM
    llm_mode: Literal["auto", "live", "off"] = "auto"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=45.0, gt=0)

    # 邮件
    mail_mode: Literal["mock", "live", "off"] = "mock"
    hr_mailbox: str = "hr@example.com"
    hr_sender_name: str = "星野科技 HR"
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_start_tls: bool = False
    smtp_user: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_user: str = ""
    imap_password: str = ""
    imap_inbox: str = "INBOX"
    imap_poll_limit: int = 30

    # 调度
    scheduler_enabled: bool = True
    schedule_cron: str = ""
    inbound_poll_seconds: int = 300

    # ---------- 派生路径 ----------
    @property
    def resumes_dir(self) -> Path:
        return self.var_dir / "resumes"

    @property
    def packages_dir(self) -> Path:
        return self.var_dir / "packages"

    @property
    def mail_outbox_dir(self) -> Path:
        return self.var_dir / "outbox" / "mail"

    @property
    def mock_inbox_dir(self) -> Path:
        return self.var_dir / "mock_inbox"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def llm_enabled(self) -> bool:
        if self.llm_mode == "off":
            return False
        if self.llm_mode == "live":
            return True
        return bool(self.openai_api_key)

    @property
    def mail_live(self) -> bool:
        return self.mail_mode == "live"

    @property
    def feishu_live(self) -> bool:
        return self.feishu_mode == "live"

    def ensure_dirs(self) -> None:
        for path in (
            self.var_dir,
            self.resumes_dir,
            self.packages_dir,
            self.mail_outbox_dir,
            self.mock_inbox_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def load_company_config(self) -> CompanyConfig:
        """读取业务配置；文件缺失或格式错误直接抛出，避免“静默跑空轮”。"""
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        return CompanyConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：改动环境变量后强制重新读取。"""
    get_settings.cache_clear()
