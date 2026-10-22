import uuid

from sqlalchemy import func, select

from app.models import User


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_me_without_token_is_401(client):
    r = client.get("/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_first_login_creates_user(client, login_as, session_factory):
    login_as(uid="uid-1", email="ali@example.com", name="Ali Veli")

    r = client.get("/me")

    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "ali@example.com"
    assert body["name"] == "Ali Veli"
    assert body["used_storage"] == 0
    assert "id" in body and "created_at" in body
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_second_call_does_not_duplicate(client, login_as, session_factory):
    login_as(uid="uid-1", email="ali@example.com", name="Ali Veli")
    first = client.get("/me").json()
    second = client.get("/me").json()

    assert first["id"] == second["id"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_profile_updates_but_missing_claims_do_not_erase(client, login_as):
    login_as(uid="uid-1", email="ali@example.com", name="Ali Veli")
    client.get("/me")

    login_as(uid="uid-1", email="ali@example.com", name="Ali Yılmaz")
    assert client.get("/me").json()["name"] == "Ali Yılmaz"

    # E-posta/şifre ile girişte token'da name olmayabilir: eski ad silinmemeli.
    login_as(uid="uid-1", email="ali@example.com")
    assert client.get("/me").json()["name"] == "Ali Yılmaz"


def test_different_uids_are_different_users(client, login_as, session_factory):
    login_as(uid="uid-1", email="a@example.com")
    id1 = client.get("/me").json()["id"]
    login_as(uid="uid-2", email="b@example.com")
    id2 = client.get("/me").json()["id"]

    assert id1 != id2
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 2


def test_user_without_email_or_name(client, login_as):
    login_as(uid="uid-3")
    body = client.get("/me").json()
    assert body["email"] is None and body["name"] is None


# --- Plan ve depolama sınırı -------------------------------------------------


def _add_plan(session_factory, name, limit):
    from app.models import SubscriptionPlan

    with session_factory() as db:
        plan = SubscriptionPlan(name=name, price=0, storage_limit=limit)
        db.add(plan)
        db.commit()
        return plan.id


def test_me_without_plan_rows_falls_back_to_free_100mb(client, login_as):
    login_as(uid="p1")
    body = client.get("/me").json()
    assert body["plan_name"] == "Free"
    assert body["storage_limit"] == 100 * 1024**2


def test_me_uses_free_plan_from_table(client, login_as, session_factory):
    _add_plan(session_factory, "Free", 50 * 1024**2)
    login_as(uid="p1")
    body = client.get("/me").json()
    assert (body["plan_name"], body["storage_limit"]) == ("Free", 50 * 1024**2)


def test_me_uses_active_subscription_plan(client, login_as, session_factory):
    from app.models import UserSubscription

    _add_plan(session_factory, "Free", 100 * 1024**2)
    plus = _add_plan(session_factory, "Plus", 100 * 1024**3)
    login_as(uid="p1")
    user_id = uuid.UUID(client.get("/me").json()["id"])
    with session_factory() as db:
        db.add(UserSubscription(user_id=user_id, plan_id=plus, status="active"))
        db.commit()
    body = client.get("/me").json()
    assert (body["plan_name"], body["storage_limit"]) == ("Plus", 100 * 1024**3)


def test_me_ignores_expired_and_cancelled_subscriptions(client, login_as, session_factory):
    from datetime import datetime, timedelta, timezone

    from app.models import UserSubscription

    _add_plan(session_factory, "Free", 100 * 1024**2)
    plus = _add_plan(session_factory, "Plus", 100 * 1024**3)
    login_as(uid="p1")
    user_id = uuid.UUID(client.get("/me").json()["id"])
    now = datetime.now(timezone.utc)
    with session_factory() as db:
        db.add(UserSubscription(user_id=user_id, plan_id=plus, status="cancelled"))
        db.add(UserSubscription(
            user_id=user_id, plan_id=plus, status="active",
            start_date=now - timedelta(days=60), end_date=now - timedelta(days=30),
        ))
        db.commit()
    assert client.get("/me").json()["plan_name"] == "Free"
