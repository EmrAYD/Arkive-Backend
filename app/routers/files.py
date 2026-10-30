import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.db import get_db
from app.models import AIResult, File, FolderLocation, User
from app.naming import unique_file_name
from app.plans import current_plan, max_file_size
from app import ai_results, summarize
from app import trash as trash_logic
from app.routers.folders import get_owned_folder
from app.schemas import (
    DownloadUrlResponse,
    FileInitRequest,
    FileInitResponse,
    FileOut,
    FileUpdate,
    SummarizeIn,
    SummaryIn,
    SummaryOut,
)
from app.storage import Storage, StorageError, get_optional_storage, get_storage

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
    _hash_if_summarizable(db, file, storage)
    db.refresh(file)  # has_summary, içerik özeti belli olduktan sonra yeniden hesaplanır
    return file


def _hash_if_summarizable(db: Session, file: File, storage: Storage) -> None:
    """Özetlenebilir türdeki dosyanın içerik özetini çıkarır: aynı içerikle yeniden yüklenen dosya, daha önce
    özetlenmiş kopyasının özetini hemen paylaşsın (AI düğmesi dolu gelsin). Başarısız olması yüklemeyi bozmaz."""
    if file.content_hash is not None or summarize.kind_of(file.name, file.mime_type) is None:
        return
    if file.size > settings.summary_max_file_bytes:
        return
    try:
        _read_and_hash(db, file, storage)
    except (HTTPException, StorageError):
        log.warning("İçerik özeti çıkarılamadı: %s", file.id)


@router.get("", response_model=list[FileOut])
def list_files(
    folder_id: uuid.UUID | None = Query(
        default=None, description="Boşsa klasörsüz (en üst seviyedeki) dosyalar listelenir"
    ),
    favorite: bool = Query(
        default=False,
        description="true ise klasörden bağımsız olarak TÜM favori dosyalar listelenir (folder_id yok sayılır)",
    ),
    location: FolderLocation = Query(
        default=FolderLocation.home,
        description="Yalnızca klasörsüz (folder_id yok) listelemede anlamlı: home = ana sayfanın en üstü, "
        "ai_results = AI Sonuçlarım sayfasının en üstü (yapay zekanın ürettiği dosyalar)",
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
        query = query.where(
            File.folder_id.is_(None), File.is_ai_result.is_(location == FolderLocation.ai_results)
        )
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
        # Normal dosyalar ana sayfa klasörlerine, yapay zeka sonuç dosyaları AI Sonuçlarım klasörlerine taşınır.
        wanted_location = "ai_results" if file.is_ai_result else _UPLOAD_LOCATION
        if folder.location != wanted_location:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Yapay zeka sonuçları yalnızca AI Sonuçlarım klasörlerine taşınabilir"
                if file.is_ai_result
                else "Dosya bu klasöre taşınamaz (AI sonuçları klasörlerine dosya konulamaz)",
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


def _summary_out(row: AIResult) -> SummaryOut:
    data = row.result or {}
    return SummaryOut(
        summary=data.get("summary", ""),
        source=data.get("source", "cloud"),
        model=data.get("model"),
        truncated=bool(data.get("truncated", False)),
        result_file_id=row.result_file_id,
        created_at=row.date,
    )


def _summarizable_file(db: Session, user: User, file_id: uuid.UUID) -> File:
    """Özetlenebilir (hazır, desteklenen türde, çöpte olmayan) kendi dosyası; değilse uygun hata."""
    file = _get_owned_file(db, user, file_id)
    if file.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "Dosya henüz hazır değil")
    if file.is_ai_result:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Yapay zeka sonuçları yeniden özetlenemez.")
    if summarize.kind_of(file.name, file.mime_type) is None:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, summarize.UNSUPPORTED_MESSAGE)
    if file.size > settings.summary_max_file_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Dosya özetlenemeyecek kadar büyük.")
    return file


def _cached_summary(
    db: Session, user: User, file: File, storage: Storage, *, need_llm: bool = False
) -> tuple[AIResult | None, bytes | None]:
    """(kayıtlı özet, dosya içeriği): dosyanın ya da aynı içerikli bir kopyasının özeti varsa (özet, None); yoksa
    (None, özetlenecek içerik). İçerik özeti ilk kez burada hesaplanıyorsa kopyasının özeti de ancak şimdi aranabilir.
    [need_llm]: bulut yapay zeka yapılandırılmamışsa dosyayı boşuna okumadan 503 verir."""
    existing = ai_results.find_summary(db, user, file.content_hash)
    if existing is not None:
        return existing, None
    if need_llm and not (settings.llm_base_url and settings.llm_model):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Bulut yapay zeka yapılandırılmamış.")
    hash_was_known = file.content_hash is not None
    data = _read_and_hash(db, file, storage)
    if hash_was_known:
        return None, data
    return ai_results.find_summary(db, user, file.content_hash), data


def _read_and_hash(db: Session, file: File, storage: Storage) -> bytes:
    """Dosyanın içeriğini okur ve (henüz yoksa) içerik özetini kaydeder."""
    try:
        data = storage.read_object(file.storage_key, settings.summary_max_file_bytes)
    except ValueError:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Dosya özetlenemeyecek kadar büyük.")
    if file.content_hash is None:
        file.content_hash = summarize.content_hash(data)
        db.commit()
    return data


def _store_summary(
    db: Session,
    user: User,
    file: File,
    text: str,
    origin: str,
    model: str | None,
    storage: Storage,
    truncated: bool = False,
    ai_row: AIResult | None = None,
    template: str | None = None,
    pdf: bytes | None = None,
) -> AIResult:
    """Özeti `ai_results` tablosuna yazar ve "AI Sonuçlarım" sayfasına PDF dosyası olarak da koyar. [pdf]: telefonun
    ürettiği hazır PDF (yoksa sunucu üretir)."""
    result_file = ai_results.save_summary_file(db, user, storage, file, text, template, pdf)
    return ai_results.finish(
        db, file, summary=text, origin=origin, model=model, result_file=result_file, truncated=truncated, row=ai_row
    )


@router.get("/{file_id}/summary", response_model=SummaryOut)
def get_summary(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SummaryOut:
    """Dosyanın kayıtlı özeti (yoksa 404). Aynı içerikli bir kopyanın özeti de bu dosyanın özetidir."""
    file = _get_owned_file(db, user, file_id, include_trashed=True)
    row = ai_results.find_summary(db, user, file.content_hash)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bu dosyanın özeti yok")
    return _summary_out(row)


def _summary_http_error(e: summarize.SummaryError) -> HTTPException:
    """Özetleme hatasını HTTP hatasına çevirir. Kodlu hatalarda (parola gerekli/yanlış) `detail` {code, message} olur."""
    if e.code:
        return HTTPException(e.status_code, {"code": e.code, "message": e.message})
    return HTTPException(e.status_code, e.message)


@router.post("/{file_id}/summary", response_model=SummaryOut)
async def summarize_file(
    file_id: uuid.UUID,
    request: Request,
    body: SummarizeIn | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> SummaryOut:
    """Dosyayı sunucudaki yapay zekayla özetler ve özeti kaydeder. Dosyanın (ya da aynı içerikli bir kopyasının)
    özeti zaten varsa yeniden üretilmez, kayıtlı özet döner: aynı dosya ikinci kez özetlenemez.

    Uygulama bunu dosya telefonda özetlenemeyecek kadar büyük/uzun olduğunda ya da ayar "Bulut" olduğunda çağırır.
    Dosya sunucuya yeniden yüklenmez; backend onu doğrudan depolamadan okur.

    Parolalı PDF/DOCX için gövdede `password` verilir; parola dosyayı bellekte açmak için kullanılır, saklanmaz.
    Parola gerekiyorsa ve yoksa ya da yanlışsa 423 döner (`detail.code`: password_required / password_invalid).

    İstemci bağlantıyı keserse (uygulamada özetleme iptal edilirse) özetleme hemen durur: yeni LLM çağrısı başlatılmaz,
    süren çağrı kesilir ve model sunucusu üretimi bırakır. Kayıt "başarısız" olur; sonraki istek yeniden özetler.
    """
    cancel = threading.Event()

    async def watch_disconnect() -> None:
        while not cancel.is_set():
            if await request.is_disconnected():
                cancel.set()
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch_disconnect())
    try:
        return await run_in_threadpool(_summarize_file, file_id, body, user, db, storage, cancel)
    finally:
        cancel.set()  # iş bitti (ya da düştü): gözcü de dursun
        watcher.cancel()


def _summarize_file(
    file_id: uuid.UUID, body: SummarizeIn | None, user: User, db: Session, storage: Storage, cancel: threading.Event
) -> SummaryOut:
    file = _summarizable_file(db, user, file_id)
    existing, data = _cached_summary(db, user, file, storage, need_llm=True)
    if existing is not None:
        return _summary_out(existing)
    # Parolalı dosya özetleme kaydı açılmadan çözülür: parola eksik/yanlışsa "başarısız" satırı oluşmaz.
    try:
        data = summarize.unlock(data, file.name, file.mime_type, body.password if body else None)
    except summarize.SummaryError as e:
        raise _summary_http_error(e)
    ai_row = ai_results.begin(db, file)  # ai_results: durum "processing"
    try:
        extracted = summarize.extract(data, file.name, file.mime_type)
        with summarize.cancellable(cancel):
            text = summarize.summarize(extracted)
    except summarize.SummaryError as e:
        ai_results.fail(db, ai_row, e.message)
        raise _summary_http_error(e)
    except summarize.Cancelled:
        ai_results.fail(db, ai_row, "Özetleme iptal edildi.")
        raise HTTPException(499, "Özetleme iptal edildi.")  # istemci gitti; yanıtı okuyan olmaz
    if extracted.truncated:
        text += summarize.TRUNCATED_NOTE
    row = _store_summary(
        db, user, file, text, "cloud", settings.llm_model, storage, truncated=extracted.truncated, ai_row=ai_row,
        template=body.template if body else None,
    )
    return _summary_out(row)


@router.put("/{file_id}/summary", response_model=SummaryOut)
def save_local_summary(
    file_id: uuid.UUID,
    body: SummaryIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> SummaryOut:
    """Telefonda üretilen özeti kaydeder (özet sunucuya gitmeden önce telefonda yapılmıştır). Dosyanın zaten özeti
    varsa mevcut özet korunur ve o döner."""
    file = _summarizable_file(db, user, file_id)
    existing, _ = _cached_summary(db, user, file, storage)
    if existing is not None:
        return _summary_out(existing)
    ai_row = ai_results.begin(db, file)
    row = _store_summary(
        db, user, file, body.summary, "local", body.model, storage, ai_row=ai_row, template=body.template,
        pdf=ai_results.client_pdf(body.pdf_base64),
    )
    return _summary_out(row)
