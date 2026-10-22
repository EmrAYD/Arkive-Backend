"""Çöp kutusu mantığı: yumuşak silme, geri yükleme, kalıcı silme ve süresi dolanları temizleme.

Silme iki aşamalıdır:
  1. "Sil" öğeyi çöpe atar: `deleted_at` dolar, öğe normal listelerden kaybolur ama dosya içeriği RustFS'te
     ve kotada kalır (geri yüklenebilsin diye).
  2. Çöpten silinince ya da süre (kullanıcının `trash_retention_days` ayarı, varsayılan 30 gün) dolunca
     kayıtlar ve RustFS nesneleri kalıcı silinir, kullanım sayacı düşer.

Bir klasör çöpe atılınca içindeki her şey (alt klasörler ve dosyalar) AYNI zaman damgasıyla işaretlenir.
Çöp kutusunda yalnızca "kök" öğeler görünür: üst klasörüyle aynı damgayı taşımayanlar (doğrudan atılanlar).
Geri yüklerken de aynı damgayı taşıyanlar birlikte geri gelir; daha önce ayrıca atılmış olanlar çöpte kalır.
"""
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from app.models import File, Folder, User
from app.naming import unique_file_name
from app.storage import Storage, StorageError

log = logging.getLogger(__name__)

_CHUNK = 500  # IN (...) listelerini SQLite'ın değişken sınırına takılmasın diye parçalarız


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """SQLite saat dilimsiz tarih döner; PostgreSQL'de saat dilimli. İkisini de UTC'li hale getirir."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def purge_at(deleted_at: datetime, retention_days: int) -> datetime:
    return as_utc(deleted_at) + timedelta(days=retention_days)


def days_left(deleted_at: datetime, retention_days: int, now: datetime | None = None) -> int:
    """Kalıcı silinmeye kaç gün kaldı (yukarı yuvarlanır; en az 0)."""
    remaining = purge_at(deleted_at, retention_days) - (now or utcnow())
    return max(0, math.ceil(remaining.total_seconds() / 86400))


def _tree(root_ids: list[uuid.UUID]):
    """Verilen klasörler ve tüm alt klasörlerinin kimlikleri (özyinelemeli sorgu)."""
    tree = select(Folder.id).where(Folder.id.in_(root_ids)).cte("tree", recursive=True)
    parent = tree.alias()
    return tree.union_all(select(Folder.id).where(Folder.parent_id == parent.c.id))


def _chunks(items: list, size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --- Çöpe atma -------------------------------------------------------------


def trash_file(db: Session, file: File, ts: datetime | None = None) -> None:
    file.deleted_at = ts or utcnow()
    db.commit()


def trash_folder(db: Session, folder: Folder, ts: datetime | None = None) -> None:
    """Klasörü, alt klasörlerini ve içindeki dosyaları aynı damgayla çöpe atar.

    Daha önce ayrıca çöpe atılmış olanların (deleted_at dolu) damgası değişmez.
    """
    ts = ts or utcnow()
    tree = _tree([folder.id])
    db.execute(
        update(Folder)
        .where(Folder.id.in_(select(tree.c.id)), Folder.deleted_at.is_(None))
        .values(deleted_at=ts)
    )
    db.execute(
        update(File)
        .where(File.folder_id.in_(select(tree.c.id)), File.deleted_at.is_(None))
        .values(deleted_at=ts)
    )
    db.commit()


# --- Çöp kutusunun içeriği -------------------------------------------------


def root_trashed_files(db: Session, user: User) -> list[File]:
    """Çöpteki, doğrudan atılmış dosyalar (üst klasörüyle birlikte atılanlar hariç)."""
    query = (
        select(File)
        .outerjoin(Folder, File.folder_id == Folder.id)
        .where(
            File.user_id == user.id,
            File.deleted_at.is_not(None),
            File.status == "ready",
            or_(
                File.folder_id.is_(None),
                Folder.deleted_at.is_(None),
                Folder.deleted_at != File.deleted_at,
            ),
        )
        .order_by(File.deleted_at.desc(), func.lower(File.name))
    )
    return list(db.scalars(query))


def root_trashed_folders(db: Session, user: User) -> list[Folder]:
    parent = aliased(Folder)
    query = (
        select(Folder)
        .outerjoin(parent, Folder.parent_id == parent.id)
        .where(
            Folder.user_id == user.id,
            Folder.deleted_at.is_not(None),
            or_(
                Folder.parent_id.is_(None),
                parent.deleted_at.is_(None),
                parent.deleted_at != Folder.deleted_at,
            ),
        )
        .order_by(Folder.deleted_at.desc(), func.lower(Folder.name))
    )
    return list(db.scalars(query))


def get_trashed_folder(db: Session, user: User, folder_id: uuid.UUID) -> Folder:
    """Kullanıcının çöpteki klasörü; çöpte olmayan ya da başkasının klasörü için 404."""
    folder = db.scalar(
        select(Folder).where(
            Folder.id == folder_id, Folder.user_id == user.id, Folder.deleted_at.is_not(None)
        )
    )
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Klasör çöp kutusunda bulunamadı")
    return folder


def trashed_children(db: Session, folder: Folder) -> tuple[list[Folder], list[File]]:
    """Çöpteki klasörün içindekiler: onunla AYNI damgayla atılmış alt klasörler ve hazır dosyalar.

    Klasörden önce ayrıca atılmış olanlar (farklı damga) çöp kutusunun kök listesinde görünür, burada değil.
    """
    folders = list(
        db.scalars(
            select(Folder)
            .where(
                Folder.user_id == folder.user_id,
                Folder.parent_id == folder.id,
                Folder.deleted_at == folder.deleted_at,
            )
            .order_by(func.lower(Folder.name))
        )
    )
    files = list(
        db.scalars(
            select(File)
            .where(
                File.user_id == folder.user_id,
                File.folder_id == folder.id,
                File.status == "ready",
                File.deleted_at == folder.deleted_at,
            )
            .order_by(func.lower(File.name))
        )
    )
    return folders, files


def folder_contents(db: Session, folder: Folder) -> tuple[int, int]:
    """(dosya sayısı, toplam bayt): klasörle BİRLİKTE çöpe atılmış hazır dosyalar."""
    tree = _tree([folder.id])
    count, size = db.execute(
        select(func.count(File.id), func.coalesce(func.sum(File.size), 0)).where(
            File.folder_id.in_(select(tree.c.id)),
            File.status == "ready",
            File.deleted_at == folder.deleted_at,
        )
    ).one()
    return count, size


# --- Geri yükleme ----------------------------------------------------------


def _unique_folder_name(
    db: Session, user_id: uuid.UUID, parent_id: uuid.UUID | None, location: str, name: str
) -> str:
    """Hedefte aynı adlı (çöpte olmayan) klasör varsa "Ad (geri yüklendi)", "Ad (geri yüklendi 2)"… döner."""

    def taken(candidate: str) -> bool:
        query = select(func.count()).select_from(Folder).where(
            Folder.user_id == user_id,
            Folder.deleted_at.is_(None),
            func.lower(Folder.name) == candidate.lower(),
        )
        if parent_id is None:
            query = query.where(Folder.parent_id.is_(None), Folder.location == location)
        else:
            query = query.where(Folder.parent_id == parent_id)
        return db.scalar(query) > 0

    if not taken(name):
        return name
    n = 1
    while True:
        suffix = " (geri yüklendi)" if n == 1 else f" (geri yüklendi {n})"
        candidate = name[: 255 - len(suffix)] + suffix
        if not taken(candidate):
            return candidate
        n += 1


def restore(db: Session, user: User, file_ids: list[uuid.UUID], folder_ids: list[uuid.UUID]) -> int:
    """Çöpteki öğeleri geri yükler; kaç öğe geri yüklendiğini döner (bulunamayanlar sessizce atlanır).

    Özgün üst klasör artık yoksa ya da o da çöpteyse öğe en üst seviyeye (klasörsüz) geri gelir.
    Hedefte aynı adlı bir klasör varsa geri yüklenen klasörün adına "(geri yüklendi)" eklenir; aynı adlı
    dosya varsa dosyanın adına uzantıdan önce "(geri yüklendi)" eklenir.
    """
    restored = 0

    folders = list(
        db.scalars(
            select(Folder).where(
                Folder.user_id == user.id,
                Folder.id.in_(folder_ids),
                Folder.deleted_at.is_not(None),
            )
        )
    ) if folder_ids else []
    for folder in folders:
        ts = folder.deleted_at
        parent_id = folder.parent_id
        if parent_id is not None:
            parent = db.get(Folder, parent_id)
            if parent is None or parent.deleted_at is not None:
                parent_id = None
        name = _unique_folder_name(db, user.id, parent_id, folder.location, folder.name)
        # Önce kökü (ad çakışması çözülmüş olarak), sonra aynı damgalı alt öğeleri geri getir.
        db.execute(
            update(Folder)
            .where(Folder.id == folder.id)
            .values(deleted_at=None, parent_id=parent_id, name=name)
        )
        tree = _tree([folder.id])
        db.execute(
            update(Folder)
            .where(Folder.id.in_(select(tree.c.id)), Folder.deleted_at == ts)
            .values(deleted_at=None)
        )
        db.execute(
            update(File)
            .where(File.folder_id.in_(select(tree.c.id)), File.deleted_at == ts)
            .values(deleted_at=None)
        )
        restored += 1
    db.commit()  # dosya kontrolleri klasörlerin güncel durumunu görsün

    files = list(
        db.scalars(
            select(File).where(
                File.user_id == user.id, File.id.in_(file_ids), File.deleted_at.is_not(None)
            )
        )
    ) if file_ids else []
    for file in files:
        if file.folder_id is not None:
            parent = db.get(Folder, file.folder_id)
            if parent is None or parent.deleted_at is not None:
                file.folder_id = None
        # Hedefte aynı adlı dosya varsa "ad (geri yüklendi).uzantı"; adı çözmeden ÖNCE çöpten çıkarma,
        # böylece dosya kendi adıyla çakışmaz (çöpteki dosyalar zaten çakışma kontrolünde sayılmaz).
        file.name = unique_file_name(db, user.id, file.folder_id, file.name, restored=True)
        file.deleted_at = None
        db.flush()  # sıradaki dosya, bu dosyanın yeni adını da görsün
        restored += 1
    db.commit()
    return restored


# --- Kalıcı silme ----------------------------------------------------------


def hard_delete(
    db: Session,
    user: User,
    storage: Storage | None,
    file_ids: list[uuid.UUID],
    folder_ids: list[uuid.UUID],
) -> int:
    """Dosyaları ve klasörleri (alt klasörleri ve içindeki dosyalarla) KALICI siler.

    Veritabanındaki alt kayıtların silinmesini ON DELETE CASCADE yapar; ama veritabanı iki şeyi bilmez:
      1. RustFS'teki nesneler silinmeli,
      2. kullanım sayacı (used_storage) silinen tamamlanmış dosyaların boyutu kadar düşmeli.
    Önce veritabanı, sonra depolama silinir: tersi olursa kayıtlar var ama dosyalar yok durumu çıkardı
    (daha kötü). En kötü ihtimal, silinemeyen nesnelerin sahipsiz kalmasıdır; log'a yazılır.

    Kullanıcıya ait olmayan ya da bulunamayan kimlikler sessizce atlanır. Silinen öğe sayısını (istenen
    kimliklerden bulunanlar) döner.
    """
    files: dict[uuid.UUID, File] = {}
    for chunk in _chunks(file_ids):
        for f in db.scalars(select(File).where(File.user_id == user.id, File.id.in_(chunk))):
            files[f.id] = f
    folders: list[Folder] = []
    for chunk in _chunks(folder_ids):
        folders += list(
            db.scalars(select(Folder).where(Folder.user_id == user.id, Folder.id.in_(chunk)))
        )

    if folders:
        tree = _tree([f.id for f in folders])
        for f in db.scalars(select(File).where(File.folder_id.in_(select(tree.c.id)))):
            files[f.id] = f

    if files and storage is None:
        # Nesneleri silemeyeceğimiz halde kayıtları silersek RustFS'te sahipsiz dosyalar kalırdı.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Dosya depolama yapılandırılmamış")

    keys = [f.storage_key for f in files.values()]
    # Kullanım sayacına yalnızca tamamlanmış dosyalar yansımıştı (bkz. files.complete_upload).
    freed = sum(f.size for f in files.values() if f.status in ("ready", "deleted"))
    requested = len(folders) + sum(1 for fid in file_ids if fid in files)

    for chunk in _chunks(list(files)):
        db.execute(delete(File).where(File.id.in_(chunk)))
    for chunk in _chunks([f.id for f in folders]):
        db.execute(delete(Folder).where(Folder.id.in_(chunk)))
    if freed:
        db.execute(
            update(User)
            .where(User.id == user.id)
            .values(
                # GREATEST/max(a, b) PostgreSQL ile SQLite'ta farklı olduğu için taşınabilir CASE kullanılır.
                used_storage=case((User.used_storage < freed, 0), else_=User.used_storage - freed)
            )
        )
    db.commit()

    for key in keys:
        try:
            storage.delete(key)
        except StorageError:
            log.exception("Nesne silinemedi, sahipsiz kaldı: %s", key)
    return requested


def empty_trash(db: Session, user: User, storage: Storage | None) -> int:
    """Kullanıcının çöp kutusundaki her şeyi kalıcı siler."""
    files = root_trashed_files(db, user)
    folders = root_trashed_folders(db, user)
    return hard_delete(db, user, storage, [f.id for f in files], [f.id for f in folders])


def purge_expired(
    db: Session, storage: Storage | None, user_id: uuid.UUID | None = None, now: datetime | None = None
) -> int:
    """Çöpte bekleme süresi (kullanıcının ayarı) dolan öğeleri kalıcı siler. Silinen öğe sayısını döner.

    Hem arka plan görevi hem de çöp kutusu her listelendiğinde çağrılır. Depolama yapılandırılmamışsa
    dosya içeren işler atlanır (nesneler silinemez), bir sonraki turda yeniden denenir.
    """
    now = now or utcnow()
    query = select(User)
    if user_id is not None:
        query = query.where(User.id == user_id)

    total = 0
    for user in list(db.scalars(query)):
        cutoff = now - timedelta(days=user.trash_retention_days)
        folder_ids = list(
            db.scalars(
                select(Folder.id).where(
                    Folder.user_id == user.id,
                    Folder.deleted_at.is_not(None),
                    Folder.deleted_at < cutoff,
                )
            )
        )
        file_ids = list(
            db.scalars(
                select(File.id).where(
                    File.user_id == user.id,
                    File.deleted_at.is_not(None),
                    File.deleted_at < cutoff,
                )
            )
        )
        if not (folder_ids or file_ids):
            continue
        try:
            total += hard_delete(db, user, storage, file_ids, folder_ids)
        except HTTPException:
            db.rollback()
            log.warning("Çöp temizliği atlandı (depolama yapılandırılmamış): %s", user.id)
    return total
