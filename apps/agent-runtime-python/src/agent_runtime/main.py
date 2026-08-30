"""uvicorn 入口。"""

from __future__ import annotations

from .app import create_app

app = create_app()
