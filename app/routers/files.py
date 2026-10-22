import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.models import File, User
from app.naming import unique_file_name
from app.plans import current_plan, max_file_size
from app import trash as trash_logic
from app.routers.folders import get_owned_folder
from app.schemas import (
    DownloadUrlResponse,
    FileInitRequest,
    FileInitResponse,
    FileOut,
    FileUpdate,
)
from app.storage import Storage, get_optional_storage, get_storage

router = APIRouter(prefix="/files", tags=["files"])
log = logging.getLogger(__name__)

# Dosyalar yalnızca "home" konumundaki klasörlere (ya da klasörsüz) yüklenir; "ai_results" sayfası
# yapay zekânın ürettiği sonuçlara ayrılmıştır.
_UPLOAD_LOCATION = "home"


def _get_owned_file(
    db: Session, user: User, file_id: uuid.UUID, include_trashed: bool = False
) -> File:
    """Dosya yalnızca sahibine görünür; başkasınınki için 404 (varlığı bile sızdırılmaz)."""
    # Çöpteki dosya (deleted_at dolu) normal akışta yok sayılır; çöp kutusu işlemleri app/trash.py'dedir.
    # include_trashed: yalnızca okuma uçları (bilgi, indirme adresi) çöpteki dosyayı da açar; çöp kutusunda ön izleme için.
    query = select(File).where(File.id == file_id, File.user_id == user.id)
    if not include_trashed:
        query = query.where(File.deleted_at.is_(None))
    file = db.scalar(query)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dosya bulunamadı")
    return file


def _pending_upload_bytes(db: Session, user_id: uuid.UUID) -> int:
    """Başlatılmış ama henüz tamamlanmamış (ve süresi dolmamış) yüklemelerin toplam boyutu.

    Kota denetimine eklenir; yoksa bir kullanıcı çok sayıda yüklemeyi aynı anda başlatıp
    henüz kullanım sayacına yansımadıkları için sınırı aşabilirdi.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.upload_url_expires_seconds + 60)
    return db.scalar(
        select(func.coalesce(func.sum(File.size), 0)).where(
            File.user_id == user_id, File.status == "uploading", File.created_at > cutoff
        )
    )


@router.post("/init", response_model=FileInitResponse, status_code=status.HTTP_201_CREATED)
def init_upload(
    body: FileInitRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> FileInitResponse:
    """1. adım: yüklemeyi başlatır. Kuralları denetler, kaydı 'uploading' durumunda açar ve
    dosyanın doğrudan RustFS'e yükleneceği süreli, imzalı adresi döner."""
    plan_name, storage_limit = current_plan(db, user)

    if body.size > max_file_size(plan_name):
        raise HTTPException(
            413,
            f"Dosya çok büyük. {plan_name} planında dosya başına en fazla "
            f"{max_file_size(plan_name) // 1024**2} MB yüklenebilir.",
        )

    if user.used_storage + _pending_upload_bytes(db, user.id) + body.size > storage_limit:
        raise HTTPException(
            413, "Depolama alanınız yetersiz."
        )

    if body.folder_id is not None:
        folder = get_owned_folder(db, user, body.folder_id)
        if folder.location != _UPLOAD_LOCATION:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Bu klasöre dosya yüklenemez (AI sonuçları klasörlerine yükleme yapılamaz)",
            )

    file_id = uuid.uuid4()
    file = File(
        id=file_id,
        user_id=user.id,
        folder_id=body.folder_id,
        # Hedefte aynı adlı dosya varsa "ad (1)", "ad (2)"… olur (app/naming.py); istemci adı yanıttan okur.
        name=unique_file_name(db, user.id, body.folder_id, body.name),
        size=body.size,
        mime_type=body.mime_type,
        status="uploading",
        # Nesne adı kullanıcının verdiği dosya adından bağımsızdır: özel karakter, çakışma ve
        # başkasının dosyasının üzerine yazma sorunu olmaz. Dosyanın görünen adı veritabanında durur.
        storage_key=f"{user.id}/{file_id}",
    )
    db.add(file)
    db.commit()

    url = storage.presign_put(
        file.storage_key, body.mime_type, body.size, settings.upload_url_expires_seconds
    )
    return FileInitResponse(
        file=FileOut.model_validate(file),
        upload_url=url,
        upload_headers={"Content-Type": body.mime_type},
        expires_in=settings.upload_url_expires_seconds,
    )


@router.post("/{file_id}/complete", response_model=FileOut)
def complete_upload(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> File:
    """2. adım: istemci yüklemeyi bitirdiğini bildirir. Backend istemciye güvenmez, nesneyi RustFS'te
    kendisi kontrol eder; ancak orada ve bildirilen boyutta ise dosya 'ready' olur ve kullanım sayacı artar."""
    file = _get_owned_file(db, user, file_id)

    if file.status == "ready":  # tekrar çağrı: sayacı ikinci kez artırmadan aynı sonucu dön
        return file
    if file.status != "uploading":
        raise HTTPException(status.HTTP_409_CONFLICT, "Bu yükleme tamamlanamaz")

    actual = storage.object_size(file.storage_key)
    if actual is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Dosya henüz depolamaya ulaşmamış")
    if actual != file.size:
        storage.delete(file.storage_key)
        file.status = "failed"
        db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Yüklenen dosyanın boyutu bildirilenle uyuşmuyor"
        )

    # "Yalnızca hâlâ uploading ise ready yap": aynı anda gelen iki tamamlama isteğinden yalnızca biri
    # satırı değiştirebilir (rowcount == 1), kullanım sayacı da yalnızca onun tarafından artırılır.
    changed = db.execute(
        update(File).where(File.id == file.id, File.status == "uploading").values(status="ready")
    ).rowcount
    if changed == 1:
        db.execute(
            update(User).where(User.id == user.id).values(used_storage=User.used_storage + file.size)
        )
    db.commit()
    db.refresh(file)
    return file


@router.get("", response_model=list[FileOut])
def list_files(
    folder_id: uuid.UUID | None = Query(
        default=None, description="Boşsa klasörsüz (en üst seviyedeki) dosyalar listelenir"
    ),
    favorite: bool = Query(
        default=False,
        description="true ise klasörden bağımsız olarak TÜM favori dosyalar listelenir (folder_id yok sayılır)",
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[File]:
    """Hazır (yüklemesi tamamlanmış) dosyalar: bir seviyedekiler ya da tüm favoriler."""
    query = select(File).where(
        File.user_id == user.id, File.status == "ready", File.deleted_at.is_(None)
    )
    if favorite:
        query = query.where(File.favorite.is_(True))
    elif folder_id is not None:
        get_owned_folder(db, user, folder_id)  # yoksa / başkasınınsa 404
        query = query.where(File.folder_id == folder_id)
    else:
        query = query.where(File.folder_id.is_(None))
    return list(db.scalars(query.order_by(func.lower(File.name))))


@router.patch("/{file_id}", response_model=FileOut)
def update_file(
    file_id: uuid.UUID,
    body: FileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> File:
    """Dosyanın görünen adını, favori durumunu ve/veya klasörünü (taşıma) günceller.

    Yalnızca veritabanındaki kayıt değişir; RustFS'teki nesnenin adı (`<user_id>/<file_id>`)
    değişmez, bu yüzden yeniden adlandırma ve taşıma anlıktır ve dosya içeriğine dokunmaz.
    """
    file = _get_owned_file(db, user, file_id)
    if file.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "Dosya henüz hazır değil")
    sent = body.model_fields_set

    # Önce doğrula, sonra değiştir: hata verirsek kayıtta yarım değişiklik kalmasın.
    if "folder_id" in sent and body.folder_id is not None:
        folder = get_owned_folder(db, user, body.folder_id)  # yoksa / başkasınınsa 404
        if folder.location != _UPLOAD_LOCATION:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Dosya bu klasöre taşınamaz (AI sonuçları klasörlerine dosya konulamaz)",
            )

    # Yeni konumda aynı adlı başka dosya varsa ad otomatik "ad (1)", "ad (2)"… olur (hem yeniden adlandırmada
    # hem taşımada); dosyanın kendisi karşılaştırmadan hariç tutulur.
    if "name" in sent or "folder_id" in sent:
        target_folder = body.folder_id if "folder_id" in sent else file.folder_id
        wanted = body.name if "name" in sent else file.name
        file.name = unique_file_name(db, user.id, target_folder, wanted, exclude_id=file.id)
    if "folder_id" in sent:
        file.folder_id = body.folder_id
    if "favorite" in sent:
        file.favorite = body.favorite
    db.commit()
    db.refresh(file)
    return file


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage | None = Depends(get_optional_storage),
) -> Response:
    """Hazır dosyayı ÇÖP KUTUSUNA atar (kalıcı silme: `/trash`). Dosya içeriği ve kullanım sayacı, çöpten
    kalıcı silinene (ya da bekleme süresi dolana) kadar olduğu gibi kalır.

    Yarım kalmış ('uploading'/'failed') kayıtlar çöpe değil doğrudan KALICI silinir: istemci iptal/hata
    durumunda kendi açtığı kaydı bu yolla temizler, bunların geri yüklenecek bir içeriği yoktur.
    """
    file = _get_owned_file(db, user, file_id)
    if file.status == "ready":
        trash_logic.trash_file(db, file)
    else:
        trash_logic.hard_delete(db, user, storage, [file.id], [])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{file_id}", response_model=FileOut)
def get_file(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> File:
    """Tek dosyanın bilgisi (ön izleme ekranı dosyayı yalnızca id'siyle açar)."""
    file = _get_owned_file(db, user, file_id, include_trashed=True)
    if file.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "Dosya henüz hazır değil")
    return file


@router.get("/{file_id}/download", response_model=DownloadUrlResponse)
def download_url(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> DownloadUrlResponse:
    """Dosyayı indirmek için kısa süreli, imzalı adres döner."""
    file = _get_owned_file(db, user, file_id, include_trashed=True)
    if file.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "Dosya henüz hazır değil")
    return DownloadUrlResponse(
        url=storage.presign_get(file.storage_key, file.name, settings.download_url_expires_seconds),
        expires_in=settings.download_url_expires_seconds,
    )
