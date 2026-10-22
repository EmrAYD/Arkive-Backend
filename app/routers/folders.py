import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app import trash as trash_logic
from app.db import get_db
from app.models import Folder, FolderLocation, User
from app.schemas import FolderCreate, FolderOut, FolderUpdate

router = APIRouter(prefix="/folders", tags=["folders"])


# --- Yardımcı fonksiyonlar -------------------------------------------------


def get_owned_folder(db: Session, user: User, folder_id: uuid.UUID) -> Folder:
    """Klasörü getirir; yalnızca isteği yapan kullanıcıya aitse.

    Başkasının klasörü için 403 (yasak) yerine 404 (yok) döneriz: böylece bir
    klasör id'sinin sistemde var olup olmadığı dışarıdan öğrenilemez.
    """
    # Çöpteki klasör (deleted_at dolu) normal akışta yok sayılır; çöp kutusu işlemleri app/trash.py'dedir.
    folder = db.scalar(
        select(Folder).where(
            Folder.id == folder_id, Folder.user_id == user.id, Folder.deleted_at.is_(None)
        )
    )
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Klasör bulunamadı")
    return folder


def _name_taken(
    db: Session,
    user_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    location: str,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """Aynı yerde (aynı üst klasör, kökse aynı sayfa) aynı adlı klasör var mı?

    Büyük/küçük harf farkı yok sayılır ("Belgeler" ile "belgeler" aynı kabul edilir).
    Veritabanındaki benzersiz indeks yalnızca birebir aynı adı yakalar; bu kontrol
    kullanıcıya düzgün bir hata mesajı verebilmek ve harf farkını da yakalamak içindir.
    """
    query = select(func.count()).select_from(Folder).where(
        Folder.user_id == user_id,
        Folder.deleted_at.is_(None),  # çöpteki klasör adı bloke etmez
        func.lower(Folder.name) == name.lower(),
    )
    if parent_id is None:
        query = query.where(Folder.parent_id.is_(None), Folder.location == location)
    else:
        query = query.where(Folder.parent_id == parent_id)
    if exclude_id is not None:
        query = query.where(Folder.id != exclude_id)
    return db.scalar(query) > 0


_DUPLICATE_NAME = "Bu konumda aynı adlı bir klasör zaten var"


# --- Uç noktalar -----------------------------------------------------------


@router.post("", response_model=FolderOut, status_code=status.HTTP_201_CREATED)
def create_folder(
    body: FolderCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Folder:
    """Yeni klasör oluşturur (kökte ya da başka bir klasörün içinde)."""
    if body.parent_id is not None:
        parent = get_owned_folder(db, user, body.parent_id)
        # Alt klasör, üst klasörün sayfasında görünür. Farklı bir konum istenirse çelişki var demektir.
        if body.location is not None and body.location.value != parent.location:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Alt klasörün konumu üst klasörünün konumuyla aynı olmalı",
            )
        location = parent.location
    else:
        location = (body.location or FolderLocation.home).value

    if _name_taken(db, user.id, body.parent_id, location, body.name):
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_NAME)

    folder = Folder(
        user_id=user.id,
        parent_id=body.parent_id,
        name=body.name,
        location=location,
        color=body.color,
        favorite=body.favorite,
    )
    db.add(folder)
    try:
        db.commit()
    except IntegrityError:
        # İki istek aynı anda aynı adı oluşturmaya çalıştıysa kontrolü ikisi de geçmiş olabilir;
        # veritabanındaki benzersiz indeks ikincisini reddeder.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_NAME)
    return folder


@router.get("", response_model=list[FolderOut])
def list_folders(
    parent_id: uuid.UUID | None = Query(
        default=None, description="Verilirse bu klasörün alt klasörleri listelenir"
    ),
    location: FolderLocation = Query(
        default=FolderLocation.home,
        description="parent_id verilmediğinde: hangi sayfanın kök klasörleri",
    ),
    favorite: bool = Query(
        default=False,
        description="true ise derinliğinden bağımsız TÜM favori klasörler listelenir (diğer filtreler yok sayılır)",
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Folder]:
    """Bir seviyedeki klasörleri listeler (iç içe olanlar ayrı ayrı istenir).

    - `GET /folders?location=home`        → ana sayfanın kök klasörleri
    - `GET /folders?location=ai_results`  → yapay zekâ sonuçları sayfasının kök klasörleri
    - `GET /folders?parent_id=<id>`       → o klasörün içindeki klasörler
    - `GET /folders?favorite=true`        → tüm favori klasörler ("Favorilerim" sayfası)
    """
    query = select(Folder).where(Folder.user_id == user.id, Folder.deleted_at.is_(None))
    if favorite:
        query = query.where(Folder.favorite.is_(True))
    elif parent_id is not None:
        get_owned_folder(db, user, parent_id)  # yoksa / başkasınınsa 404
        query = query.where(Folder.parent_id == parent_id)
    else:
        query = query.where(Folder.parent_id.is_(None), Folder.location == location.value)
    return list(db.scalars(query.order_by(func.lower(Folder.name))))


@router.get("/{folder_id}", response_model=FolderOut)
def get_folder(
    folder_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Folder:
    return get_owned_folder(db, user, folder_id)


@router.patch("/{folder_id}", response_model=FolderOut)
def update_folder(
    folder_id: uuid.UUID,
    body: FolderUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Folder:
    """Klasörün adını, rengini, favori durumunu ve/veya yerini günceller (yalnızca gönderilen alanlar).

    `parent_id` gönderilirse klasör (alt klasörleri ve dosyalarıyla birlikte) o klasörün içine taşınır;
    `null` ana sayfanın en üstüne taşır. Yalnızca ana sayfadaki klasörler taşınabilir ve hedef de ana
    sayfada olmalı. Klasör kendi içine ya da kendi alt klasörlerinin içine taşınamaz.
    """
    folder = get_owned_folder(db, user, folder_id)
    sent = body.model_fields_set

    new_name = body.name if "name" in sent else folder.name
    new_parent_id = body.parent_id if "parent_id" in sent else folder.parent_id
    moving = "parent_id" in sent and new_parent_id != folder.parent_id

    if moving:
        if folder.location != FolderLocation.home.value:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Yapay zekâ sonuçları klasörleri taşınamaz",
            )
        if new_parent_id is not None:
            target = get_owned_folder(db, user, new_parent_id)
            if target.location != FolderLocation.home.value:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Klasör bu klasöre taşınamaz (AI sonuçları klasörlerine klasör konulamaz)",
                )
            # Hedeften köke doğru yürürüz; yolda taşınan klasörü görürsek hedef onun içindedir (döngü olurdu).
            cursor: Folder | None = target
            while cursor is not None:
                if cursor.id == folder.id:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "Klasör kendi içine taşınamaz",
                    )
                cursor = (
                    db.get(Folder, cursor.parent_id) if cursor.parent_id is not None else None
                )

    if new_name != folder.name or moving:
        if _name_taken(
            db, user.id, new_parent_id, folder.location, new_name, exclude_id=folder.id
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_NAME)
        folder.name = new_name
        folder.parent_id = new_parent_id
    if "color" in sent:
        folder.color = body.color
    if "favorite" in sent:
        folder.favorite = body.favorite

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE_NAME)
    return folder


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_folder(
    folder_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Klasörü, alt klasörlerini ve içindeki dosyaları ÇÖP KUTUSUNA atar (kalıcı silme: `/trash`).

    Hiçbir şey silinmez: öğeler `deleted_at` ile işaretlenir, geri yüklenebilir. RustFS nesneleri ve kullanım
    sayacı, çöpten kalıcı silinene (ya da bekleme süresi dolana) kadar olduğu gibi kalır.
    """
    folder = get_owned_folder(db, user, folder_id)
    trash_logic.trash_folder(db, folder)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
