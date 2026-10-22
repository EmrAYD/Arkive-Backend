import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import SessionLocal
from app.routers import files, folders, me, trash
from app.storage import StorageError, get_optional_storage
from app.trash import purge_expired

log = logging.getLogger(__name__)


def run_trash_purge() -> int:
    """Süresi dolan çöp öğelerini kalıcı siler (her kullanıcı kendi bekleme süresine göre)."""
    with SessionLocal() as db:
        return purge_expired(db, get_optional_storage())


async def _purge_loop() -> None:
    while True:
        try:
            purged = await asyncio.to_thread(run_trash_purge)
            if purged:
                log.info("Çöp kutusundan %d öğe kalıcı silindi", purged)
        except Exception:  # görev hiçbir koşulda ölmesin; bir sonraki turda yeniden denenir
            log.exception("Çöp temizliği başarısız")
        await asyncio.sleep(settings.trash_purge_interval_seconds)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_purge_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Arkive API", version="0.1.0", lifespan=lifespan)
app.include_router(me.router)
app.include_router(folders.router)
app.include_router(files.router)
app.include_router(trash.router)


@app.exception_handler(StorageError)
def storage_error_handler(_request: Request, _exc: StorageError) -> JSONResponse:
    """RustFS'e ulaşılamadığında ya da hata verdiğinde istemciye anlaşılır bir 503 döner."""
    return JSONResponse(
        status_code=503, content={"detail": "Dosya depolama şu anda kullanılamıyor"}
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}
