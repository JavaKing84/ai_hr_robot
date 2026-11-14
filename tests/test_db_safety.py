"""数据库安全护栏的单元测试。

这些用例保护的不是业务功能，而是「测试/验收脚本绝不会清空生产库」这条底线：
`assert_disposable_database` 拒绝任何不以 ``_test`` / ``_e2e`` 结尾的库名，
`prefer_local_test_target` 保证本机可用时不往共享服务器上写。

护栏失效的后果是不可逆的（真实候选人数据被 TRUNCATE），所以它自己必须有测试。
"""

from __future__ import annotations

import pytest

from core.db import (
    DEFAULT_LOCAL_PG_DSN,
    DISPOSABLE_SUFFIXES,
    assert_disposable_database,
    derive_test_database_url,
    is_local_host,
    prefer_local_test_target,
)

PROD_URL = "postgresql+asyncpg://admin:pw@10.191.44.66:15432/hr_ai_robot"
REMOTE_URL = "postgresql+asyncpg://admin:pw@10.191.44.66:15432/hr_ai_robot_test"


# ------------------------------------------------------------------ 库名后缀护栏


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:p@localhost:5432/hr_ai_robot_test",
        "postgresql+asyncpg://u:p@localhost:5432/hr_ai_robot_e2e",
    ],
)
def test_disposable_database_accepts_test_suffixes(url: str) -> None:
    """一次性库后缀必须放行，否则测试根本跑不起来。"""
    assert assert_disposable_database(url)


@pytest.mark.parametrize(
    "url",
    [
        PROD_URL,  # 生产库本体
        "postgresql+asyncpg://u:p@localhost:5432/hr_ai_robot_prod",
        "postgresql+asyncpg://u:p@localhost:5432/postgres",
        "postgresql+asyncpg://u:p@localhost:5432/jobInsight",
    ],
)
def test_disposable_database_rejects_non_test_suffixes(url: str) -> None:
    """名字不带 _test/_e2e 的库一律拒绝——包括长得像但不是的库名。"""
    with pytest.raises(RuntimeError) as excinfo:
        assert_disposable_database(url)
    assert "拒绝" in str(excinfo.value)


def test_disposable_database_rejects_missing_name() -> None:
    """连接串里没有库名时也不能蒙混过关。"""
    with pytest.raises(RuntimeError):
        assert_disposable_database("postgresql+asyncpg://u:p@localhost:5432")


def test_disposable_suffixes_are_trailing_matches() -> None:
    """后缀必须匹配结尾，不能是「包含」——`hr_ai_robot_test_backup` 不该被当成测试库。"""
    assert "_test" in DISPOSABLE_SUFFIXES
    with pytest.raises(RuntimeError):
        assert_disposable_database("postgresql+asyncpg://u:p@localhost:5432/hr_ai_robot_test_backup")


# ------------------------------------------------------------------ 派生与本机优先


def test_derive_is_idempotent() -> None:
    """重复派生不会叠成 hr_ai_robot_test_test。"""
    once = derive_test_database_url(PROD_URL)
    assert once.endswith("/hr_ai_robot_test")
    assert derive_test_database_url(once) == once


def test_derive_with_e2e_suffix() -> None:
    assert derive_test_database_url(PROD_URL, suffix="_e2e").endswith("/hr_ai_robot_e2e")


def test_is_local_host() -> None:
    for host in ("localhost", "127.0.0.1", "::1", ""):
        assert is_local_host(host) is True
    for host in ("10.191.44.66", "db.internal", "postgres"):
        assert is_local_host(host) is False


def test_prefer_local_keeps_local_target_untouched() -> None:
    """目标已经是本机时不该被改写。"""
    local = "postgresql+asyncpg://hr:hr@localhost:5432/hr_ai_robot_test"
    url, reason = prefer_local_test_target(local)
    assert url == local
    assert "本机" in reason


def test_prefer_local_falls_back_to_remote_when_port_closed() -> None:
    """本机端口不通时保留远程——断网/没开 Docker 也能跑测试，不静默失败。

    端口 1 是保留端口，正常情况下不会有人监听，用它模拟「本机没有 PostgreSQL」。
    """
    url, reason = prefer_local_test_target(REMOTE_URL, local_port=1)
    assert url == REMOTE_URL
    assert "未监听" in reason


def test_prefer_local_switches_when_local_port_open() -> None:
    """本机端口可连时，必须换成本机同名库（绝不在共享服务器上跑 TRUNCATE）。"""
    # 借一个确定在监听的本机端口（测试环境里 pytest 自身不保证有），
    # 用 subprocess 起一个临时监听器成本太高；这里改为直接构造一个必然可连的场景：
    # 若 5432 恰好开着就验证切换，否则跳过——避免用例强依赖外部环境。
    from core.db import port_open

    if not port_open("127.0.0.1", 5432, timeout=1.0):
        pytest.skip("本机 5432 未监听，无法验证切换分支")

    url, reason = prefer_local_test_target(REMOTE_URL)
    assert url.startswith(DEFAULT_LOCAL_PG_DSN)
    assert url.endswith("/hr_ai_robot_test")
    assert "已切换" in reason
