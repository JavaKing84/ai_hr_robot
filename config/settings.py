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

from core.logging import get_logger
from schema.config import CompanyConfig, PositionConfig

logger = get_logger(__name__)

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
    # 简历预览链接的对外地址：飞书里点「看简历」时用它拼 URL。
    # 本机跑就保持默认；部署后改成能被 HR 访问到的域名/IP。
    public_base_url: str = "http://127.0.0.1:8000"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = "Asia/Shanghai"

    # 数据库：全链路只支持 PostgreSQL（开发/测试/生产同一套方言）
    database_url: str = "postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot"
    db_echo: bool = False
    #: 启动时自动执行 alembic upgrade head（默认开）。关掉它就必须自己先迁移。
    auto_migrate: bool = True

    # ---------- 内部接口鉴权 ----------
    #: 运维接口令牌。配置后 /api/v1/* 必须带 X-API-Token 或 Authorization: Bearer。
    #: 留空 = 不拦截（仅本机开发可以这样），启动日志与 /health 会显示 auth=disabled。
    api_token: str = ""

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
    #: 回调来源校验策略：auto = 有 encrypt_key 就验签名、有 token 就验 token、都没有则
    #: 非 dev 环境直接拒收；strict = 两者必须至少配一个；off = 不校验（仅本地联调）。
    feishu_verify_mode: Literal["auto", "strict", "off"] = "auto"
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

    # 面试通知确认闸门：True=挂起等 HR 确认后发；False=主管给名单即发。
    # 默认 False（2026-11-14 改）：主管在群里已经点明「谁 + 什么时间」，
    # 再回一条「请确认全发 / 确认1,3 / 取消」是让他把同一件事说第二遍。
    # 防误发靠的是邮件通道本身——幂等 Message-ID、mock 模式、失败不推进状态。
    # 需要人工复核的场景（例如批量导入后自动约面）再显式打开。
    interview_notice_require_confirm: bool = False

    # 确认请求发给谁；留空则发给该岗位主管，填了统一发给这个 HR。
    hr_feishu_open_id: str = ""

    # 语义分影子模式：照常打分并留痕，但通过与否仍按规则分判定，避免名单整体平移。
    semantic_shadow: bool = True

    # LLM
    llm_mode: Literal["auto", "live", "off"] = "auto"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_fallback_models: str = ""
    llm_timeout_seconds: float = Field(default=45.0, gt=0)
    #: 多供应商链（JSON 数组），顺序即降级顺序；留空退回单供应商。
    #: 每项：{"name": "...", "model": "openai/...", "api_base": "http://host:port/v1", "api_key": "..."}
    llm_providers: str = ""
    #: 是否让大模型做深度解析（分维度打分 + 亮点/风险/追问点）
    llm_deep_analysis: bool = True
    #: 读招聘需求截图用的多模态模型（如 qwen2.5-vl / gpt-4o）；留空则图片只保存不识别
    llm_vision_model: str = ""

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
    #: 主动拉群消息的间隔（秒）。0 = 关闭。
    #: 为什么需要：webhook 要飞书服务器能回连到本机，内网/开发机收不到，
    #: 主管在群里回话系统就成了聋子。主动拉取不依赖入站网络。
    feishu_poll_seconds: int = 10
    #: 拉群消息时是否只处理 @ 了机器人的消息。单群专用场景可关。
    feishu_require_mention: bool = False
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
            entry: dict[str, str] = {
                "name": str(item.get("name") or model),
                "model": model,
                "api_base": api_base,
                "api_key": api_key,
            }
            # 思考型模型会把推理写进 reasoning，max_tokens 偏小时 content 为 null、JSON 抽取失败。
            # 配 "disable_thinking": true 可让 HTTP 直连路径关掉思考。
            if str(item.get("disable_thinking") or "").strip().lower() in {"1", "true", "yes", "on"}:
                entry["disable_thinking"] = "true"
            chain.append(entry)
        return chain

    @property
    def llm_fallback_model_list(self) -> list[str]:
        """``LLM_FALLBACK_MODELS`` 逗号分隔的降级候选模型列表（已去空、去重）。"""
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def feishu_group_push(self) -> bool:
        """出站是否发到群里。

        2026-11-13 修正：旧实现只认 ``FEISHU_WEBHOOK_URL``，导致「只配了
        ``FEISHU_CHAT_ID`` + 应用凭证」的部署被判成私聊，群机器人与 Agent 问答
        全部静默走 open_id 分支（而 open_id 往往是群 chat_id，发不出去）。
        现在两条通道任有一条即可进群：有 chat_id 用应用身份发，
        只有 webhook 就用自定义机器人发。
        """
        if self.feishu_push_target != "group":
            return False
        return bool((self.feishu_webhook_url or "").strip() or (self.feishu_chat_id or "").strip())

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
        """读取业务配置；文件缺失或格式错误直接抛出，避免“静默跑空轮”。

        ``config/positions.d/*.yaml`` 是主管发 JD 后由机器人自动生成的岗位，
        这里合并进主配置——加岗位不用改 ``positions.yaml``。
        """
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        config = CompanyConfig.model_validate(raw)
        _merge_draft_positions(config, self.config_path.parent / "positions.d")
        return config


def _merge_draft_positions(config: CompanyConfig, draft_dir: Path) -> None:
    """把 positions.d 下的岗位文件并入配置；**同 code 的草稿覆盖主配置**。

    为什么草稿优先：``positions.d`` 里的文件是 HR 后来在飞书里说的要求，
    比手写的 ``positions.yaml`` 新，理应盖掉旧的那份。

    2026-11-14 之前这里写的是「同 code 以主配置为准，直接 continue」，后果是：
    HR 说「后端岗改成五年经验」→ 模型抽出 ``backend_senior``（撞上主配置的 code）
    → 草稿被丢掉 → 但 ``JobIntakeService.apply`` 已经写盘、``position.intaken``
    事件也记了、回复还说「已录入」。**配置一个字没变，人却被告诉改好了**，
    这是最难查的一种失败。现在改成覆盖。

    覆盖时要先从**所有**部门摘掉同 code 的旧条目再挂到新部门：
    否则 HR 把一个岗位从技术部调到产品部，会出现两个部门各有一份同 code 的岗位。
    """
    if not draft_dir.is_dir():
        return
    by_name = {d.name: d for d in config.departments}
    default_department = config.departments[0] if config.departments else None
    for path in sorted(draft_dir.glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            position = PositionConfig.model_validate(payload)
        except Exception:  # noqa: BLE001 - 单个文件坏掉不影响主配置加载
            logger.warning("config.draft_position_invalid", extra={"path": str(path)})
            continue

        department = by_name.get(position.department or "") or default_department
        if department is None:
            continue
        position.department = department.name

        for other in config.departments:
            kept = [p for p in other.positions if p.code != position.code]
            if len(kept) != len(other.positions):
                # 重新赋值而不是原地 remove：pydantic 模型列表改了才稳妥，
                # 也避免遍历时为「当前部门」和「旧部门」是同一个对象而漏摘。
                other.positions = kept
        department.positions.append(position)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：改动环境变量后强制重新读取。"""
    get_settings.cache_clear()
