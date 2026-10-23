import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import FolderLocation


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None
    created_at: datetime
    used_storage: int


class MeOut(UserOut):
    """/me yanıtı: profil + geçerli plan ve depolama sınırı (kalan kullanım = storage_limit - used_storage)."""

    plan_name: str
    storage_limit: int
    max_file_size: int  # tek bir dosyanın en büyük boyutu (bayt)
    trash_retention_days: int  # çöpteki öğelerin kalıcı silinmeden önce kaç gün bekleyeceği


class MeUpdate(BaseModel):
    """Kullanıcı ayarları (şimdilik yalnızca çöp kutusu bekleme süresi). Gönderilmeyen alan değişmez."""

    trash_retention_days: int | None = Field(default=None, ge=1, le=365)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("Güncellenecek en az bir alan gönderilmeli")
        if "trash_retention_days" in self.model_fields_set and self.trash_retention_days is None:
            raise ValueError("trash_retention_days boş (null) olamaz")
        return self


# ---------------------------------------------------------------------------
# Klasörler
# ---------------------------------------------------------------------------

_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def _clean_folder_name(value: str | None) -> str | None:
    """Klasör adını doğrular. Hata durumunda FastAPI otomatik 422 döner."""
    if value is None:
        return None
    name = value.strip()
    if not 1 <= len(name) <= 255:
        raise ValueError("Klasör adı 1-255 karakter olmalı")
    # "/" ve "\" yol ayırıcılarıdır; ad içinde olursa ileride dosya yolu oluştururken karışıklık çıkar.
    if "/" in name or "\\" in name:
        raise ValueError("Klasör adı '/' veya '\\' içeremez")
    return name


def _clean_color(value: str | None) -> str | None:
    """Rengi "#RRGGBB" biçiminde (büyük harfle) saklar. Boşsa renk yok demektir."""
    if value is None:
        return None
    color = value.strip()
    if not _COLOR_RE.fullmatch(color):
        raise ValueError("Renk '#RRGGBB' biçiminde olmalı (ör. #1E88E5)")
    return color.upper()


class FolderCreate(BaseModel):
    """İstemcinin klasör oluştururken gönderdiği JSON."""

    name: str
    # Boşsa klasör kök seviyede oluşturulur.
    parent_id: uuid.UUID | None = None
    # Yalnızca kök klasörler için anlamlı. Alt klasör konumunu üst klasöründen devralır.
    location: FolderLocation | None = None
    color: str | None = None
    favorite: bool = False

    _check_name = field_validator("name")(_clean_folder_name)
    _check_color = field_validator("color")(_clean_color)


class FolderUpdate(BaseModel):
    """Klasörü güncellemek için (ad, renk, favori, üst klasör). Gönderilmeyen alan değişmez.

    `color: null` göndermek rengi siler; `color` hiç gönderilmezse renk olduğu gibi kalır.
    Aynı şekilde `parent_id: null` klasörü ana sayfanın en üstüne taşır, `parent_id` hiç
    gönderilmezse klasör olduğu yerde kalır. Bu ayrımı model_fields_set (istemcinin gerçekten
    gönderdiği alanlar) sağlar.
    """

    name: str | None = None
    color: str | None = None
    favorite: bool | None = None
    parent_id: uuid.UUID | None = None

    _check_name = field_validator("name")(_clean_folder_name)
    _check_color = field_validator("color")(_clean_color)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        sent = self.model_fields_set
        if not sent:
            raise ValueError("Güncellenecek en az bir alan gönderilmeli")
        if "name" in sent and self.name is None:
            raise ValueError("Klasör adı boş (null) olamaz")
        if "favorite" in sent and self.favorite is None:
            raise ValueError("favorite true ya da false olmalı")
        return self


class FolderOut(BaseModel):
    """Sunucunun istemciye döndürdüğü klasör bilgisi."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    location: FolderLocation
    color: str | None
    favorite: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Dosyalar
# ---------------------------------------------------------------------------

_MIME_RE = re.compile(r"[A-Za-z0-9!#$&^_.+-]{1,127}/[A-Za-z0-9!#$&^_.+-]{1,127}")


def _clean_file_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 255:
        raise ValueError("Dosya adı 1-255 karakter olmalı")
    if "/" in name or "\\" in name:
        raise ValueError("Dosya adı '/' veya '\\' içeremez")
    return name


class FileInitRequest(BaseModel):
    """Yüklemeyi başlatmak için istemcinin gönderdiği bilgi (dosyanın kendisi değil)."""

    name: str
    size: int = Field(gt=0, description="Bayt cinsinden gerçek dosya boyutu")
    # Tür bilinmiyorsa (ya da gönderilmediyse) genel ikili dosya türü kullanılır.
    mime_type: str = "application/octet-stream"
    folder_id: uuid.UUID | None = None  # boşsa klasörsüz (en üst seviye)

    _check_name = field_validator("name")(_clean_file_name)

    @field_validator("mime_type", mode="before")
    @classmethod
    def _check_mime(cls, value):
        """İmza bu türe göre atıldığı için küçük harfe çevrilip biçimi denetlenir. null/boş = bilinmiyor."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return "application/octet-stream"
        if not isinstance(value, str):
            raise ValueError("Geçersiz dosya türü")
        mime = value.strip().lower()
        if not _MIME_RE.fullmatch(mime):
            raise ValueError("Geçersiz dosya türü")
        return mime


class FileUpdate(BaseModel):
    """Dosyanın adını, favori durumunu ve/veya klasörünü günceller. Gönderilmeyen alan değişmez.

    `folder_id: null` dosyayı en üst seviyeye (klasörsüz) taşır; `folder_id` hiç gönderilmezse dosya olduğu
    yerde kalır. Bu ayrımı model_fields_set (istemcinin gerçekten gönderdiği alanlar) sağlar.
    """

    name: str | None = None
    favorite: bool | None = None
    folder_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, value):
        return None if value is None else _clean_file_name(value)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        sent = self.model_fields_set
        if not sent:
            raise ValueError("Güncellenecek en az bir alan gönderilmeli")
        if "name" in sent and self.name is None:
            raise ValueError("Dosya adı boş (null) olamaz")
        if "favorite" in sent and self.favorite is None:
            raise ValueError("favorite true ya da false olmalı")
        return self


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    folder_id: uuid.UUID | None
    name: str
    size: int
    mime_type: str | None
    favorite: bool
    status: str
    created_at: datetime


class FileInitResponse(BaseModel):
    file: FileOut
    upload_url: str
    # Yükleme isteğine AYNEN eklenmesi gereken başlıklar (imza bunlara göre atıldı).
    upload_headers: dict[str, str]
    expires_in: int


class DownloadUrlResponse(BaseModel):
    url: str
    expires_in: int


# ---------------------------------------------------------------------------
# Çöp kutusu
# ---------------------------------------------------------------------------


class TrashFileOut(BaseModel):
    id: uuid.UUID
    name: str
    size: int
    mime_type: str | None
    deleted_at: datetime
    purge_at: datetime  # bu andan sonra kalıcı silinir
    days_left: int  # kalıcı silinmeye kalan gün (yukarı yuvarlanır)


class TrashFolderOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str | None
    file_count: int  # klasörle birlikte çöpe atılan dosya sayısı
    size: int  # bunların toplam boyutu (bayt)
    deleted_at: datetime
    purge_at: datetime
    days_left: int


class TrashFolderContents(BaseModel):
    """Çöpteki bir klasörün içi (salt okunur): klasörün kendisi, alt klasörleri ve dosyaları."""

    retention_days: int
    folder: TrashFolderOut
    folders: list[TrashFolderOut]
    files: list[TrashFileOut]


class TrashOut(BaseModel):
    retention_days: int
    files: list[TrashFileOut]
    folders: list[TrashFolderOut]


class TrashIds(BaseModel):
    """Çöp kutusunda işlem yapılacak öğeler."""

    file_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    folder_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.file_ids and not self.folder_ids:
            raise ValueError("En az bir öğe belirtilmeli")
        return self


class TrashCount(BaseModel):
    count: int
