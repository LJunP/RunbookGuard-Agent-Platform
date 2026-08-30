"""uvicorn 入口。"""

from __future__ import annotations

from .app import create_app
from .scenarios import ScenarioLibrary

app = create_app(ScenarioLibrary.from_builtin_dir())
