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

    # 数据库（开发/生产默认 PostgreSQL；测试由 tests/conftest.py 注入 SQLite 覆盖）
    database_url: str = "postgresql+asyncpg://hr:hr@localhost:5432/hr_robot"
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
    # 群机器人（自定义机器人）webhook：2026-11-13 起主管/HR 的推送统一走群，
    # 不再逐个 open_id 单聊私信。留空则自动回落到 open_id 私信（旧行为）。
    feishu_webhook_url: str = ""
    # 群机器人的 chat_id（oc_xxx）：自定义机器人 webhook 不能上传文件，
    # 简历压缩包要用应用身份发到同一个群，必须知道 chat_id。留空则只发文本+本地路径。
    feishu_chat_id: str = ""
    # group = 全部出站消息进群；user = 保持旧的 open_id 私信
    feishu_push_target: Literal["group", "user"] = "group"
    # 群消息 @ 谁（open_id 列表，逗号分隔）；留空则 @所有人
    feishu_mention_open_ids: str = ""

    # 面试通知确认闸门（测试期间默认开启）：
    # True = 只挂起、不发送，必须由 HR 人工确认后才投递；
    # False = 主管给出名单后直接发送（生产环境确认过再用）。
    interview_notice_require_confirm: bool = True

    # 面试通知确认请求发往哪个飞书用户（open_id）。
    # 留空 = 发给触发这次安排的岗位主管；填了则统一发给该 HR 本人。
    hr_feishu_open_id: str = ""

    # 语义分影子模式（校准期默认开启）：
    # True  = 照常调用大模型打分并落库留痕，但「通过与否」仍按规则分判定；
    # False = 融合分（规则分与语义分加权）真正决定通过。
    # 为什么要影子：阈值 55/60 是按纯规则分布定的，语义分一上来就参与判定，
    # 通过名单会整体平移，无法判断是「改好了」还是「改坏了」。
    semantic_shadow: bool = True

    # LLM
    llm_mode: Literal["auto", "live", "off"] = "auto"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_fallback_models: str = ""
    llm_timeout_seconds: float = Field(default=45.0, gt=0)
    #: 多供应商链（JSON 数组）。每一项形如
    #: ``{"name": "学院-qwen3.8-27b", "model": "openai/qwen3.8-27b",
    #:    "api_base": "http://202.204.64.234:11434/v1", "api_key": "ideal@2026"}``
    #: 顺序即降级顺序；留空则退回单供应商（LLM_MODEL + OPENAI_BASE_URL + OPENAI_API_KEY）。
    llm_providers: str = ""
    #: 是否让大模型做「深度解析」（分维度打分 + 亮点/风险/面试追问点）
    llm_deep_analysis: bool = True

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
        # auto：只要配了「多供应商链」或单供应商 key 就启用
        return bool(self.llm_provider_chain or self.openai_api_key)

    @property
    def llm_provider_chain(self) -> list[dict[str, str]]:
        """解析 ``LLM_PROVIDERS``（JSON 数组）为降级顺序列表。

        每项统一成 ``{"name", "model", "api_base", "api_key"}``：
        - ``model`` 没写供应商前缀时补 ``openai/``（三个学院/DeepSeek 端点都是
          OpenAI 兼容的 ``/v1`` 接口，litellm 用 ``openai/`` 前缀 + api_base 即可路由）；
        - 解析失败不抛异常、只返回空列表——配置写坏时静默退回规则抽取，绝不让
          整轮任务因为一个 JSON 写错而崩掉。
        """
        import json

        raw = (self.llm_providers or "").strip()
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []

        chain: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or "").strip()
            if not model:
                continue
            if "/" not in model:
                model = f"openai/{model}"
            api_base = str(item.get("api_base") or item.get("api_url") or "").strip()
            # 允许直接贴 /chat/completions 结尾的完整地址，这里统一裁成 base
            for suffix in ("/chat/completions", "/completions"):
                if api_base.endswith(suffix):
                    api_base = api_base[: -len(suffix)]
            api_key = str(item.get("api_key") or "").strip()
            key = (model, api_base)
            if key in seen:
                continue
            seen.add(key)
            chain.append(
                {
                    "name": str(item.get("name") or model),
                    "model": model,
                    "api_base": api_base,
                    "api_key": api_key,
                }
            )
        return chain

    @property
    def llm_fallback_model_list(self) -> list[str]:
        """``LLM_FALLBACK_MODELS`` 逗号分隔的降级候选模型列表（已去空、去重）。"""
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def feishu_group_push(self) -> bool:
        """出站是否走群机器人 webhook。"""
        return bool(self.feishu_webhook_url) and self.feishu_push_target == "group"

    @property
    def feishu_mention_list(self) -> list[str]:
        return [x.strip() for x in (self.feishu_mention_open_ids or "").split(",") if x.strip()]

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
