"""API FastAPI de GoNet."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.bot_api.security import validate_runtime_security
from apps.bot_api.routes.gateway import router as gateway_router
from apps.bot_api.routes.health import router as health_router
from apps.bot_api.routes.internal import router as internal_router
from apps.bot_api.routes.meta_webhook import router as meta_router
from apps.bot_api.routes.ocr_callback import router as ocr_callback_router
from apps.bot_api.routes.outbound import router as outbound_router
from packages.orchestrator.inactivity import InactivityScheduler
from packages.shared.config import get_settings
from packages.shared.errors import SharedStateUnavailableError
from packages.shared.logging import setup_logging

settings = get_settings()
setup_logging()
scheduler = InactivityScheduler(settings=settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Arranca las validaciones de runtime y el scheduler de inactividad al levantar la API."""
    validate_runtime_security()
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.shutdown()

app = FastAPI(
    title="GoNet Platform Bot API",
    version="0.1.0",
    description="Plano principal del orquestador conversacional multiagente.",
    lifespan=lifespan,
)


@app.exception_handler(SharedStateUnavailableError)
async def handle_shared_state_unavailable(_: Request, exc: SharedStateUnavailableError):
    """Convierte la falta de estado compartido en una respuesta HTTP 503."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": exc.args[0] if exc.args else "shared_state_unavailable",
            "status": "error",
        },
    )


app.include_router(gateway_router)
app.include_router(health_router)
app.include_router(internal_router)
app.include_router(meta_router)
app.include_router(ocr_callback_router)
app.include_router(outbound_router)


def run() -> None:
    """Arranca la API con Uvicorn usando la configuracion cargada."""
    import uvicorn

    uvicorn.run("apps.bot_api.main:app", host="0.0.0.0", port=settings.port, reload=False)
