"""Internal HTTP surface (ClusterIP only): health, status, plan, climate plan and metrics.

Every route is ``GET`` and read-only.  There is deliberately no endpoint that
changes state, triggers a solve or accepts a command.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST

__all__ = ["ServiceView", "create_app"]


class ServiceView(Protocol):
    """What the HTTP layer needs from the runtime (tests pass a fake)."""

    metrics: Any

    def readiness(self) -> Any: ...

    def status(self) -> dict[str, Any]: ...

    def plan_view(self) -> dict[str, Any]: ...

    def climate_plan_response(self) -> tuple[int, dict[str, Any]]: ...

    def climate_plan_summary(self) -> dict[str, Any]: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


def create_app(service: ServiceView, *, manage_lifecycle: bool = True) -> FastAPI:
    """Build the app; with *manage_lifecycle* the lifespan starts/stops *service*."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await service.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await service.stop()

    app = FastAPI(
        title="battery-optimizer (shadow)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        readiness = service.readiness()
        body = {
            "ready": readiness.ready,
            "inputs_ready": readiness.inputs_ready,
            "reasons": list(readiness.reasons),
            "details": dict(readiness.details),
        }
        return JSONResponse(body, status_code=200 if readiness.ready else 503)

    @app.get("/status")
    async def status() -> JSONResponse:
        return JSONResponse(service.status())

    @app.get("/plan")
    async def plan() -> JSONResponse:
        return JSONResponse(service.plan_view())

    @app.get("/climate/plan")
    async def climate_plan() -> JSONResponse:
        code, body = service.climate_plan_response()
        return JSONResponse(body, status_code=code)

    @app.get("/climate/plan/summary")
    async def climate_plan_summary() -> JSONResponse:
        return JSONResponse(service.climate_plan_summary())

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=service.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app
