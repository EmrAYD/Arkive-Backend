import logging
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import trash as trash_logic
from app.auth import get_current_user
from app.db import get_db
from app.models import File, Folder, User
from app.schemas import (
    TrashCount,
    TrashFileOut,
    TrashFolderContents,
    TrashFolderOut,
    TrashIds,
    TrashOut,
)
from app.storage import Storage, get_optional_storage

router = APIRouter(prefix="/trash", tags=["trash"])
log = logging.getLogger(__name__)


def _file_out(f: File, days: int, now) -> TrashFileOut:
    return TrashFileOut(
        id=f.id,
        name=f.name,
        size=f.size,
        mime_type=f.mime_type,
        deleted_at=trash_logic.as_utc(f.deleted_at),
        purge_at=trash_logic.purge_at(f.deleted_at, days),
        days_left=trash_logic.days_left(f.deleted_at, days, now),
    )


def _folder_out(db: Session, f: Folder, days: int, now) -> TrashFolderOut:
    count, size = trash_logic.folder_contents(db, f)
    return TrashFolderOut(
        id=f.id,
        name=f.name,
        color=f.color,
        file_count=count,
        size=size,
        deleted_at=trash_logic.as_utc(f.deleted_at),
        purge_at=trash_logic.purge_at(f.deleted_at, days),
        days_left=trash_logic.days_left(f.deleted_at, days, now),
    )


@router.get("", response_model=TrashOut)
def list_trash(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage | None = Depends(get_optional_storage),
) -> TrashOut:
    """Çöp kutusu: doğrudan silinmiş dosya ve klasörler (bir klasörle birlikte silinenler o klasörün içindedir).

    Listelemeden önce süresi dolanlar kalıcı silinir; böylece arka plan görevi çalışmasa bile liste doğru olur.
    """
    try:
        trash_logic.purge_expired(db, storage, user_id=user.id)
    except Exception:  # temizlik başarısız olsa da liste açılabilmeli
        log.exception("Çöp temizliği başarısız")
        db.rollback()

    now = trash_logic.utcnow()
    days = user.trash_retention_days
    return TrashOut(
        retention_days=days,
        files=[_file_out(f, days, now) for f in trash_logic.root_trashed_files(db, user)],
        folders=[_folder_out(db, f, days, now) for f in trash_logic.root_trashed_folders(db, user)],
    )


@router.get("/folders/{folder_id}", response_model=TrashFolderContents)
def trashed_folder_contents(
    folder_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrashFolderContents:
    """Çöpteki bir klasörün içini gösterir (salt okunur; ön izleme için dosyalar `GET /files/{id}` ile açılır)."""
    folder = trash_logic.get_trashed_folder(db, user, folder_id)
    folders, files = trash_logic.trashed_children(db, folder)
    now = trash_logic.utcnow()
    days = user.trash_retention_days
    return TrashFolderContents(
        retention_days=days,
        folder=_folder_out(db, folder, days, now),
        folders=[_folder_out(db, f, days, now) for f in folders],
        files=[_file_out(f, days, now) for f in files],
    )


@router.post("/restore", response_model=TrashCount)
def restore_from_trash(
    body: TrashIds, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TrashCount:
    """Seçilen öğeleri geri yükler. Özgün klasörü yoksa ya da o da çöpteyse en üst seviyeye gelir."""
    return TrashCount(count=trash_logic.restore(db, user, body.file_ids, body.folder_ids))


@router.post("/delete", response_model=TrashCount)
def delete_from_trash(
    body: TrashIds,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage | None = Depends(get_optional_storage),
) -> TrashCount:
    """Seçilen öğeleri KALICI siler (geri alınamaz). Yalnızca çöpteki öğeler silinebilir."""
    # Çöpte olmayan (normal) bir öğenin kimliği gönderilirse sessizce yok sayılır: bu uç yalnızca çöp içindir.
    file_ids = list(
        db.scalars(
            select(File.id).where(
                File.user_id == user.id, File.id.in_(body.file_ids), File.deleted_at.is_not(None)
            )
        )
    ) if body.file_ids else []
    folder_ids = list(
        db.scalars(
            select(Folder.id).where(
                Folder.user_id == user.id,
                Folder.id.in_(body.folder_ids),
                Folder.deleted_at.is_not(None),
            )
        )
    ) if body.folder_ids else []
    return TrashCount(count=trash_logic.hard_delete(db, user, storage, file_ids, folder_ids))


@router.delete("", response_model=TrashCount)
def empty_trash(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage | None = Depends(get_optional_storage),
) -> TrashCount:
    """Çöp kutusunu boşaltır (her şeyi kalıcı siler)."""
    return TrashCount(count=trash_logic.empty_trash(db, user, storage))
