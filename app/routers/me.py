from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import User
from app.plans import current_plan, max_file_size
from app.schemas import MeOut, MeUpdate, UserOut

router = APIRouter(tags=["me"])


def _me_out(db: Session, user: User) -> MeOut:
    plan_name, storage_limit = current_plan(db, user)
    return MeOut(
        **UserOut.model_validate(user).model_dump(),
        plan_name=plan_name,
        storage_limit=storage_limit,
        max_file_size=max_file_size(plan_name),
        trash_retention_days=user.trash_retention_days,
    )


@router.get("/me", response_model=MeOut)
def read_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeOut:
    """Giriş yapmış kullanıcının profili ve depolama durumu. İlk çağrıda kullanıcı veritabanına eklenir."""
    return _me_out(db, user)


@router.patch("/me", response_model=MeOut)
def update_me(
    body: MeUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> MeOut:
    """Kullanıcı ayarlarını günceller. Çöp kutusu süresi kısaltılırsa, süresi dolan öğeler bir sonraki
    temizlikte (en geç bir saat içinde ya da çöp kutusu açılınca) kalıcı silinir."""
    if body.trash_retention_days is not None:
        user.trash_retention_days = body.trash_retention_days
    db.commit()
    return _me_out(db, user)
