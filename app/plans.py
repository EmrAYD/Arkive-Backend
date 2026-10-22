"""Kullanıcının geçerli aboneliğini ve depolama sınırını bulur."""
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import SubscriptionPlan, User, UserSubscription

FREE_PLAN_NAME = "Free"
# subscription_plans tablosunda Free satırı yoksa (ör. tablo elle boşaltılmışsa) kullanılan yedek sınır.
FREE_FALLBACK_STORAGE_LIMIT = 100 * 1024**2  # 100 MB

# Dosya başına boyut sınırı. Toplam sınırın (subscription_plans.storage_limit) aksine veritabanında tutulmaz,
# yükleme başlarken kodda denetlenir. Listede olmayan plan Free gibi davranır.
MAX_FILE_SIZE_BY_PLAN = {
    "Free": 10 * 1024**2,  # 10 MB
    "Plus": 2 * 1024**3,  # 2 GB
}


def max_file_size(plan_name: str) -> int:
    return MAX_FILE_SIZE_BY_PLAN.get(plan_name, MAX_FILE_SIZE_BY_PLAN[FREE_PLAN_NAME])


def current_plan(db: Session, user: User) -> tuple[str, int]:
    """(plan adı, toplam depolama sınırı - bayt) döner.

    Kural: kullanıcının şu an geçerli (durumu 'active' ve süresi dolmamış) aboneliği varsa o planı,
    yoksa Free planı kullanılır. Birden fazla geçerli abonelik varsa en yüksek sınırlı olan seçilir.
    Her kullanıcı için ayrıca "Free abonelik" satırı açmaya gerek kalmaz.
    """
    now = datetime.now(timezone.utc)
    paid = db.execute(
        select(SubscriptionPlan.name, SubscriptionPlan.storage_limit)
        .join(UserSubscription, UserSubscription.plan_id == SubscriptionPlan.id)
        .where(
            UserSubscription.user_id == user.id,
            UserSubscription.status == "active",
            or_(UserSubscription.end_date.is_(None), UserSubscription.end_date > now),
        )
        .order_by(SubscriptionPlan.storage_limit.desc())
        .limit(1)
    ).first()
    if paid is not None:
        return paid.name, paid.storage_limit

    free = db.execute(
        select(SubscriptionPlan.name, SubscriptionPlan.storage_limit).where(
            SubscriptionPlan.name == FREE_PLAN_NAME
        )
    ).first()
    if free is not None:
        return free.name, free.storage_limit

    return FREE_PLAN_NAME, FREE_FALLBACK_STORAGE_LIMIT
