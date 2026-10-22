import enum
import uuid
from datetime import datetime

from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    """ER diyagramındaki Users tablosu.

    Diyagramdaki alanlara ek olarak firebase_uid tutulur: Firebase'deki kullanıcı ile
    bizim kaydımızı bağlayan anahtar budur.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Bayt cinsinden. Dosya yükleme tamamlandıkça güncellenecek.
    used_storage: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0"
    )
    # Silinen öğelerin çöp kutusunda kaç gün bekleyip kalıcı silineceği (ayarlardan değiştirilebilir).
    trash_retention_days: Mapped[int] = mapped_column(
        Integer, default=30, server_default="30"
    )

    __table_args__ = (
        CheckConstraint(
            "trash_retention_days BETWEEN 1 AND 365", name="ck_users_trash_retention_days"
        ),
    )


class FolderLocation(str, enum.Enum):
    """Klasörün uygulamanın hangi sayfasında görüneceği (ER diyagramındaki Folders.Location)."""

    home = "home"  # ana sayfa
    ai_results = "ai_results"  # yapay zekâ sonuçları sayfası


class Folder(Base):
    """ER diyagramındaki Folders tablosu.

    İç içe klasörler "kendine referans" ile kurulur: parent_id, başka bir klasörün
    id'sini gösterir. parent_id boşsa (NULL) klasör "kök"tür, yani sayfanın en üst
    seviyesindedir. Alt klasör, konumunu (location) üst klasöründen devralır.
    """

    __tablename__ = "folders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Klasörün sahibi. Kullanıcı silinirse klasörleri de silinir (ON DELETE CASCADE).
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Üst klasör. Üst klasör silinirse alt klasörler de veritabanı tarafından silinir.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(255))

    # FolderLocation değerlerinden biri. Veritabanında düz metin olarak saklanır;
    # PostgreSQL'in "enum" türü yerine CHECK kuralı kullanılır, çünkü yeni bir değer
    # eklemek gerektiğinde bu yöntemde migration çok daha basit kalır.
    location: Mapped[str] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # ER diyagramındaki Folders.Color ve Folders.Favorite
    color: Mapped[str | None] = mapped_column(String(20))
    favorite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Çöp kutusuna atıldığı an; NULL ise klasör normal listelerde görünür. Bir klasör çöpe atılınca içindeki
    # her şey (alt klasörler ve dosyalar) aynı zaman damgasıyla işaretlenir, geri yüklerken bu damga eşleşir.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        CheckConstraint("location IN ('home', 'ai_results')", name="ck_folders_location"),
        # Aynı yerde aynı adlı iki klasör olamaz. Kök klasörler ve alt klasörler için
        # ayrı iki "kısmi benzersiz indeks" gerekir: SQL'de NULL değerler birbirinden
        # farklı sayıldığı için parent_id boşken tek bir indeks çift kaydı engelleyemez.
        Index(
            "uq_folders_root_name",
            "user_id",
            "location",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL AND deleted_at IS NULL"),
            sqlite_where=text("parent_id IS NULL AND deleted_at IS NULL"),
        ),
        Index(
            "uq_folders_child_name",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NOT NULL AND deleted_at IS NULL"),
            sqlite_where=text("parent_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )


class File(Base):
    """ER diyagramındaki Files tablosu. Dosyanın kendisi RustFS'te, bilgileri burada tutulur.

    storage_key, dosyanın RustFS (S3) içindeki nesne adıdır. folder_id boşsa (NULL) dosya
    klasörsüz, yani en üst seviyededir. Klasör silinirse içindeki dosya kayıtları da silinir;
    RustFS'teki nesnelerin silinmesi ise yükleme/silme kodunda ayrıca yapılmalıdır.
    """

    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Bayt cinsinden.
    size: Mapped[int] = mapped_column(BigInteger)
    # uploading: yükleniyor | ready: kullanıma hazır | failed: yükleme başarısız | deleted: silindi
    status: Mapped[str] = mapped_column(String(16), default="uploading", server_default="uploading")
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    favorite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # Çöp kutusuna atıldığı an; NULL ise dosya normal listelerde görünür. Çöpteki dosya kotadan yer
    # tutmaya devam eder; kalıcı silinince (elle ya da süre dolunca) kullanım düşer.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        CheckConstraint("size >= 0", name="ck_files_size"),
        CheckConstraint(
            "status IN ('uploading', 'ready', 'failed', 'deleted')", name="ck_files_status"
        ),
    )


class SubscriptionPlan(Base):
    """ER diyagramındaki Subscription-Plans tablosu (Free, Plus).

    storage_limit toplam depolama sınırıdır (bayt). Dosya başına sınır veritabanında değil,
    yükleme kodunda kontrol edilir.
    """

    __tablename__ = "subscription_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    storage_limit: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_subscription_plans_price"),
        CheckConstraint("storage_limit > 0", name="ck_subscription_plans_storage_limit"),
    )


class UserSubscription(Base):
    """ER diyagramındaki User-Subscriptions tablosu: hangi kullanıcı hangi plana abone."""

    __tablename__ = "user_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Plan silinemez, abonesi varken silmeye çalışmak hata verir (ON DELETE RESTRICT).
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"), index=True
    )
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Boşsa (NULL) abonelik süresizdir (ör. Free plan).
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'expired', 'cancelled')", name="ck_user_subscriptions_status"
        ),
        CheckConstraint(
            "end_date IS NULL OR end_date > start_date", name="ck_user_subscriptions_dates"
        ),
    )


class Payment(Base):
    """ER diyagramındaki Payments tablosu.

    Ödeme kayıtları mali kayıt olduğu için abonelik silinse bile korunur: abonelik, ödemesi
    varken silinemez (ON DELETE RESTRICT).
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_subscriptions.id", ondelete="RESTRICT"), index=True
    )
    # Ödeme sağlayıcısındaki işlem numarası. Aynı ödeme iki kez kaydedilemesin diye benzersiz.
    provider_payment_id: Mapped[str] = mapped_column(String(255), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")

    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_payments_price"),
        CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'refunded')", name="ck_payments_status"
        ),
    )


class AIResult(Base):
    """ER diyagramındaki AI-Results tablosu: bir dosya için üretilen yapay zekâ sonucu.

    file_id zorunludur. folder_id, sonucun gösterileceği klasördür (isteğe bağlı).
    """

    __tablename__ = "ai_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    # PostgreSQL'de JSONB, SQLite'ta JSON olarak saklanır.
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')", name="ck_ai_results_status"
        ),
    )


class Share(Base):
    """ER diyagramındaki Shares tablosu: shared_by, dosyayı shared_with kullanıcısıyla paylaşır."""

    __tablename__ = "shares"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    shared_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    shared_with: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # view: yalnızca görüntüleme | edit: düzenleme
    permission: Mapped[str] = mapped_column(String(8), default="view", server_default="view")

    __table_args__ = (
        CheckConstraint("permission IN ('view', 'edit')", name="ck_shares_permission"),
        CheckConstraint("shared_by <> shared_with", name="ck_shares_not_self"),
        # Aynı dosya aynı kişiyle iki kez paylaşılamaz.
        UniqueConstraint("file_id", "shared_with", name="uq_shares_file_shared_with"),
    )
