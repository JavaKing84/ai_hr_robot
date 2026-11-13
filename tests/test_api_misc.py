"""接口与仓库卫生测试：健康检查、重复投递、无前端文件、无前端依赖。"""

from __future__ import annotations

from pathlib import Path

from config.settings import BASE_DIR
from tests.conftest import resume_text

FRONTEND_SUFFIXES = {".html", ".htm", ".css", ".js", ".jsx", ".vue", ".tsx", ".scss"}
FRONTEND_PACKAGES = ("react", "vue", "tailwind", "@vitejs", "element-plus", "antd")


async def test_health_endpoint(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["components"]["feishu_mode"] == "mock"
    assert body["components"]["mail_mode"] == "mock"
    assert body["components"]["positions"] >= 4


async def test_error_envelope_contains_request_id(client) -> None:
    response = await client.get("/api/v1/recruitment/candidates/999999")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"]
    assert response.headers["X-Request-ID"] == body["error"]["request_id"]


async def test_duplicate_submission_keeps_first_record(client) -> None:
    payload = {"text": resume_text(), "filename": "张明.txt"}
    first = await client.post("/api/v1/recruitment/ingest/text", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["is_duplicate"] is False

    second = await client.post("/api/v1/recruitment/ingest/text", json=payload)
    assert second.status_code == 200
    assert second.json()["is_duplicate"] is True
    assert second.json()["candidate_id"] == first.json()["candidate_id"]

    candidate_id = first.json()["candidate_id"]
    events = (await client.get(f"/api/v1/recruitment/candidates/{candidate_id}/events")).json()
    assert any(event["action"] == "candidate.duplicate" for event in events)


async def test_ingest_rejects_tiny_text(client) -> None:
    response = await client.post("/api/v1/recruitment/ingest/text", json={"text": "太短"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_repository_has_no_frontend_files() -> None:
    offenders: list[Path] = []
    for path in BASE_DIR.rglob("*"):
        if any(part in {".venv", "var", ".git", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.is_file() and path.suffix.lower() in FRONTEND_SUFFIXES:
            offenders.append(path)
    assert offenders == [], f"仓库中不允许出现前端文件: {offenders}"


def test_dependencies_pin_apscheduler_v3() -> None:
    text = (BASE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert '"apscheduler>=3.11,<4"' in text.lower()


def test_dependencies_have_no_frontend_packages() -> None:
    text = (BASE_DIR / "pyproject.toml").read_text(encoding="utf-8").lower()
    for package in FRONTEND_PACKAGES:
        assert package not in text, f"不应引入前端依赖: {package}"
