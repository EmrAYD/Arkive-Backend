"""Dosya adı çakışmalarının otomatik çözümü.

Aynı klasörde (ya da klasörsüz üst seviyede) aynı adlı iki dosya bulunamaz; çakışma olduğunda hata
verilmez, adı otomatik değiştirilir:
  - yükleme / yeniden adlandırma / taşıma:  "rapor.pdf" -> "rapor (1).pdf" -> "rapor (2).pdf" ...
  - çöpten geri yükleme:                    "rapor.pdf" -> "rapor (geri yüklendi).pdf" -> "rapor (geri yüklendi 2).pdf" ...
Sıra numarası uzantıdan ÖNCE gelir. Karşılaştırma büyük/küçük harfe duyarsızdır (klasörlerdeki gibi).
"""
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import File

_MAX_LEN = 255  # files.name sütununun uzunluğu
_NUMBERED = re.compile(r"^(.*) \((\d+)\)$")  # "rapor (3)" -> ("rapor", 3)


def split_extension(name: str) -> tuple[str, str]:
    """"rapor.pdf" -> ("rapor", ".pdf"); ".gitignore" ve "uzantisiz" -> (ad, "")."""
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def _compose(stem: str, suffix: str, ext: str) -> str:
    """Soneki ve uzantıyı koruyarak toplam uzunluğu sınırın altında tutar (gerekirse gövdeyi kısaltır)."""
    room = _MAX_LEN - len(suffix) - len(ext)
    return stem[: max(room, 1)] + suffix + ext


def _taken_names(
    db: Session, user_id: uuid.UUID, folder_id: uuid.UUID | None, exclude_id: uuid.UUID | None
) -> set[str]:
    """Hedef konumda kullanılan (küçük harfli) dosya adları.

    Çöpteki dosyalar ve başarısız yüklemeler sayılmaz; yükleme sürmekte olanlar sayılır ki aynı anda
    yüklenen iki aynı adlı dosya da farklı adlar alsın.
    """
    query = select(func.lower(File.name)).where(
        File.user_id == user_id,
        File.deleted_at.is_(None),
        File.status.in_(("uploading", "ready")),
    )
    query = query.where(File.folder_id.is_(None) if folder_id is None else File.folder_id == folder_id)
    if exclude_id is not None:
        query = query.where(File.id != exclude_id)
    return set(db.scalars(query))


def unique_file_name(
    db: Session,
    user_id: uuid.UUID,
    folder_id: uuid.UUID | None,
    name: str,
    exclude_id: uuid.UUID | None = None,
    restored: bool = False,
) -> str:
    """Hedefte çakışmayan bir ad döner; çakışma yoksa adı olduğu gibi bırakır.

    exclude_id: yeniden adlandırılan/taşınan dosyanın kendisi (kendisiyle çakışmasın).
    restored:   True ise sayı yerine "(geri yüklendi)" soneki kullanılır (çöpten geri yükleme).
    """
    taken = _taken_names(db, user_id, folder_id, exclude_id)
    if name.lower() not in taken:
        return name

    stem, ext = split_extension(name)
    if not restored:
        # "rapor (1).pdf" zaten varsa yeni ad "rapor (1) (1)" değil "rapor (2)" olur.
        match = _NUMBERED.match(stem)
        if match:
            stem = match.group(1)

    n = 1
    while True:
        if restored:
            suffix = " (geri yüklendi)" if n == 1 else f" (geri yüklendi {n})"
        else:
            suffix = f" ({n})"
        candidate = _compose(stem, suffix, ext)
        if candidate.lower() not in taken:
            return candidate
        n += 1
