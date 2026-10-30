"""Yapay zeka özetlerinin kaydı: ER diyagramındaki AI-Results satırı ve özetin PDF dosyası.

Özetler yalnızca `ai_results` tablosunda saklanır (kaynak dosya + durum + sonuç JSON'u). Aynı içerikli (files.content_hash)
dosyalar bu kaydı paylaşır, böylece yeniden yüklenen aynı dosya tekrar özetlenmez. Ayrıca özet, `files` tablosuna
"<ad> - özet.pdf" dosyası olarak yazılır (şablonu uygulamadaki ayardan gelir, bkz. summary_pdf.py): "AI Sonuçlarım" sayfasının en üstünde durur (klasörsüz); kullanıcı isterse
sayfadaki klasörlere taşır. Normal bir dosya gibi indirilir, yeniden adlandırılır, silinir; kotadan düşer.
"""
import base64
import binascii
import io
import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.models import AIResult, File, User
from app import summary_pdf
from app.naming import split_extension, unique_file_name
from app.storage import Storage, StorageError

log = logging.getLogger(__name__)

_MAX_NAME = 255
KIND_SUMMARY = "summary"


def _file_name(source_name: str, extension: str) -> str:
    stem, _ = split_extension(source_name)
    suffix = f" - özet.{extension}"
    return stem[: max(_MAX_NAME - len(suffix), 1)] + suffix


MAX_CLIENT_PDF_BYTES = 5 * 1024 * 1024


def client_pdf(pdf_base64: str | None) -> bytes | None:
    """Telefonun gönderdiği özet PDF'i geçerliyse baytları, değilse None (o zaman sunucu kendisi üretir).

    Geçerlilik: base64 çözülür, boyut sınırı aşılmaz, "%PDF-" ile başlar ve en az bir sayfası okunabilir."""
    if not pdf_base64:
        return None
    try:
        data = base64.b64decode(pdf_base64, validate=True)
        if not data.startswith(b"%PDF-") or len(data) > MAX_CLIENT_PDF_BYTES:
            return None
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return data if not reader.is_encrypted and len(reader.pages) >= 1 else None
    except (binascii.Error, ValueError):
        return None
    except Exception:  # noqa: BLE001  (bozuk PDF: pypdf çeşitli hatalar atabilir)
        log.warning("Telefonun gönderdiği özet PDF'i okunamadı; sunucu kendisi üretecek.", exc_info=True)
        return None


def _document(
    source_name: str, summary: str, template: str | None, pdf: bytes | None = None
) -> tuple[bytes, str, str]:
    """Özet dosyasının (içerik, uzantı, mime türü). [pdf]: telefonun ürettiği hazır PDF (varsa olduğu gibi kullanılır).
    PDF üretilemezse özet yine de kaybolmasın diye düz metne düşülür."""
    if pdf is not None:
        return pdf, "pdf", "application/pdf"
    try:
        return summary_pdf.render(source_name, summary, template), "pdf", "application/pdf"
    except Exception:  # noqa: BLE001  (yazı tipi/kütüphane sorunu özetin kendisini engellememeli)
        log.warning("Özet PDF'i üretilemedi, düz metne düşüldü: %s", source_name, exc_info=True)
        text = f"{source_name} - Arkive ile özetlenmiştir\n\n{summary.strip()}\n"
        return text.encode("utf-8"), "txt", "text/plain"


def save_summary_file(
    db: Session, user: User, storage: Storage, source: File, summary: str, template: str | None = None,
    pdf: bytes | None = None,
) -> File | None:
    """Özeti "AI Sonuçlarım" sayfasının en üstüne PDF dosyası olarak yazar ([template]: şablon kimliği, bkz. summary_pdf).

    En iyi çaba: depolama hatasında özet yine de kayıtlıdır, yalnızca dosya oluşmaz (None döner, log'a yazılır).
    """
    try:
        data, extension, mime_type = _document(source.name, summary, template, pdf)
        file_id = uuid.uuid4()
        key = f"{user.id}/{file_id}"
        storage.put_object(key, data, mime_type)
        file = File(
            id=file_id,
            user_id=user.id,
            folder_id=None,
            name=unique_file_name(db, user.id, None, _file_name(source.name, extension)),
            size=len(data),
            mime_type=mime_type,
            status="ready",
            storage_key=key,
            is_ai_result=True,
        )
        db.add(file)
        db.flush()
        db.execute(update(User).where(User.id == user.id).values(used_storage=User.used_storage + file.size))
        db.commit()
        return file
    except (StorageError, IntegrityError):
        db.rollback()
        log.warning("Özet dosyası oluşturulamadı: %s", source.id, exc_info=True)
        return None


def begin(db: Session, source: File) -> AIResult:
    """Bulutta özetleme başlarken `processing` kaydı açar. Aynı dosyanın yarım kalmış (processing/failed) kaydı varsa
    yenisi açılmaz, o yeniden kullanılır; böylece başarısız denemeler kayıt yığmaz."""
    row = db.scalar(
        select(AIResult)
        .where(AIResult.file_id == source.id, AIResult.status.in_(("processing", "failed")))
        .order_by(AIResult.date.desc())
    )
    if row is None:
        row = AIResult(file_id=source.id, folder_id=None, status="processing", result=None)
        db.add(row)
    else:
        row.status = "processing"
        row.result = None
    db.commit()
    return row


def fail(db: Session, row: AIResult, message: str) -> None:
    row.status = "failed"
    row.result = {"kind": KIND_SUMMARY, "error": message}
    db.commit()


def finish(
    db: Session,
    source: File,
    *,
    summary: str,
    origin: str,
    model: str | None,
    result_file: File | None,
    truncated: bool = False,
    row: AIResult | None = None,
) -> AIResult:
    """Üretilen özeti `ai_results` satırına `done` olarak yazar ([row] yoksa yeni satır açılır).
    [origin]: 'local' (telefonda üretildi) ya da 'cloud' (sunucuda)."""
    if row is None:
        row = AIResult(file_id=source.id)
        db.add(row)
    row.status = "done"
    row.content_hash = source.content_hash
    row.folder_id = result_file.folder_id if result_file is not None else None
    row.result_file_id = result_file.id if result_file is not None else None
    row.result = {
        "kind": KIND_SUMMARY,
        "summary": summary,
        "source": origin,
        "model": model,
        "truncated": truncated,
    }
    db.commit()
    return row


def find_summary(db: Session, user: User, content_hash: str | None) -> AIResult | None:
    """Kullanıcının bu içeriğe sahip bir dosyası için tamamlanmış özet (varsa en eskisi)."""
    if not content_hash:
        return None
    owner = aliased(File)
    return db.scalar(
        select(AIResult)
        .join(owner, owner.id == func.coalesce(AIResult.file_id, AIResult.result_file_id))
        .where(owner.user_id == user.id, AIResult.content_hash == content_hash, AIResult.status == "done")
        .order_by(AIResult.date, AIResult.id)
        .limit(1)
    )
