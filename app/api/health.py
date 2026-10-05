"""GET /health: liveness plus the package version, uptime and the registered broker names."""
from __future__ import annotations

import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])
PACKAGE_NAME = "trade-execution-engine"


def app_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:  # not pip-installed
        return "0.1.0-dev"


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    state = request.app.state
    return {
        "status": "ok",
        "version": app_version(),
        "env": state.settings.app_env,
        "uptime_s": round(time.monotonic() - state.started_at, 1),
        "brokers": [info.name for info in state.registry.describe()],
    }
