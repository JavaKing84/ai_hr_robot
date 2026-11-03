"""进程入口：只负责创建应用并启动 uvicorn，不写任何业务逻辑。"""

from __future__ import annotations

import uvicorn

from config.settings import get_settings
from factory.app import create_app

app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
