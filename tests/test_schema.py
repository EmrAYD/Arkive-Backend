"""ER diyagramındaki tabloların veritabanı kuralları (silme zincirleri, CHECK, UNIQUE)."""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    AIResult,
    File,
    Folder,
    Payment,
    Share,
    SubscriptionPlan,
    User,
    UserSubscription,
)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


def make_user(db, uid="u1"):
    user = User(firebase_uid=uid, email=f"{uid}@example.com", name=uid)
    db.add(user)
    db.commit()
    return user


def make_file(db, user, folder=None, key=None, **kw):
    f = File(
        user_id=user.id,
        folder_id=folder.id if folder else None,
        name="a.pdf",
        size=100,
        storage_key=key or str(uuid.uuid4()),
        **kw,
    )
    db.add(f)
    db.commit()
    return f


def make_plan(db, name="Free"):
    plan = SubscriptionPlan(name=name, price=0, storage_limit=100 * 1024**2)
    db.add(plan)
    db.commit()
    return plan


# --- Folders: yeni alanlar ---------------------------------------------------


def test_folder_color_and_favorite_defaults(db):
    user = make_user(db)
    folder = Folder(user_id=user.id, name="Belgeler", location="home")
    db.add(folder)
    db.commit()
    db.refresh(folder)
    assert folder.color is None
    assert folder.favorite is False


# --- Files -------------------------------------------------------------------


def test_file_defaults(db):
    f = make_file(db, make_user(db))
    db.refresh(f)
    assert f.status == "uploading"
    assert f.favorite is False
    assert f.folder_id is None


def test_file_storage_key_must_be_unique(db):
    user = make_user(db)
    make_file(db, user, key="same-key")
    with pytest.raises(IntegrityError):
        make_file(db, user, key="same-key")


def test_file_status_check(db):
    with pytest.raises(IntegrityError):
        make_file(db, make_user(db), status="bogus")


def test_file_negative_size_rejected(db):
    user = make_user(db)
    db.add(File(user_id=user.id, name="x", size=-1, storage_key="k"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_deleting_folder_deletes_its_files(db):
    user = make_user(db)
    folder = Folder(user_id=user.id, name="F", location="home")
    db.add(folder)
    db.commit()
    make_file(db, user, folder)
    db.delete(folder)
    db.commit()
    assert db.query(File).count() == 0


def test_deleting_user_deletes_files(db):
    user = make_user(db)
    make_file(db, user)
    db.delete(user)
    db.commit()
    assert db.query(File).count() == 0


# --- Abonelik ve ödeme -------------------------------------------------------


def test_plan_name_unique(db):
    make_plan(db, "Free")
    with pytest.raises(IntegrityError):
        make_plan(db, "Free")


def test_plan_in_use_cannot_be_deleted(db):
    user = make_user(db)
    plan = make_plan(db)
    db.add(UserSubscription(user_id=user.id, plan_id=plan.id))
    db.commit()
    db.delete(plan)
    with pytest.raises(IntegrityError):
        db.commit()


def test_subscription_end_must_be_after_start(db):
    from datetime import datetime, timedelta, timezone

    user = make_user(db)
    plan = make_plan(db)
    now = datetime.now(timezone.utc)
    db.add(
        UserSubscription(
            user_id=user.id, plan_id=plan.id, start_date=now, end_date=now - timedelta(days=1)
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_payment_blocks_subscription_deletion(db):
    user = make_user(db)
    sub = UserSubscription(user_id=user.id, plan_id=make_plan(db).id)
    db.add(sub)
    db.commit()
    db.add(Payment(user_subscription_id=sub.id, provider_payment_id="pay_1", price=49.99))
    db.commit()
    db.delete(sub)
    with pytest.raises(IntegrityError):
        db.commit()


def test_provider_payment_id_unique(db):
    user = make_user(db)
    sub = UserSubscription(user_id=user.id, plan_id=make_plan(db).id)
    db.add(sub)
    db.commit()
    db.add(Payment(user_subscription_id=sub.id, provider_payment_id="pay_1", price=1))
    db.commit()
    db.add(Payment(user_subscription_id=sub.id, provider_payment_id="pay_1", price=1))
    with pytest.raises(IntegrityError):
        db.commit()


# --- AI sonuçları ------------------------------------------------------------


def test_ai_result_requires_file(db):
    db.add(AIResult(file_id=None))
    with pytest.raises(IntegrityError):
        db.commit()


def test_ai_result_stores_json_and_cascades_with_file(db):
    f = make_file(db, make_user(db))
    db.add(AIResult(file_id=f.id, status="done", result={"etiketler": ["fatura", "2026"]}))
    db.commit()
    assert db.query(AIResult).one().result == {"etiketler": ["fatura", "2026"]}
    db.delete(f)
    db.commit()
    assert db.query(AIResult).count() == 0


# --- Paylaşımlar -------------------------------------------------------------


def test_share_defaults_to_view(db):
    a, b = make_user(db, "a"), make_user(db, "b")
    f = make_file(db, a)
    s = Share(file_id=f.id, shared_by=a.id, shared_with=b.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    assert s.permission == "view"


def test_cannot_share_with_yourself(db):
    a = make_user(db, "a")
    f = make_file(db, a)
    db.add(Share(file_id=f.id, shared_by=a.id, shared_with=a.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_file_cannot_be_shared_twice_with_same_user(db):
    a, b = make_user(db, "a"), make_user(db, "b")
    f = make_file(db, a)
    db.add(Share(file_id=f.id, shared_by=a.id, shared_with=b.id))
    db.commit()
    db.add(Share(file_id=f.id, shared_by=a.id, shared_with=b.id, permission="edit"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_share_invalid_permission_rejected(db):
    a, b = make_user(db, "a"), make_user(db, "b")
    f = make_file(db, a)
    db.add(Share(file_id=f.id, shared_by=a.id, shared_with=b.id, permission="owner"))
    with pytest.raises(IntegrityError):
        db.commit()
