import time

import cachecontrol
import google.auth.exceptions
import requests
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

bearer_scheme = HTTPBearer(auto_error=False)

# Google'ın imza sertifikaları HTTP cache başlıklarına uyularak önbelleğe alınır;
# aksi halde her istekte yeniden indirilirdi.
_google_request = google_requests.Request(
    session=cachecontrol.CacheControl(requests.Session())
)

_CLOCK_SKEW_SECONDS = 10


def verify_firebase_id_token(token: str) -> dict:
    """Firebase ID token'ını doğrular ve {"uid", "email", "name"} döner.

    Firebase'in "üçüncü taraf JWT kütüphanesiyle doğrulama" kurallarını uygular:
    imza (Google sertifikaları), exp/iat, aud == proje kimliği, iss, sub, auth_time.
    Servis hesabı anahtarı gerekmez, yalnızca proje kimliği yeterlidir.

    Geçersiz token için ValueError / GoogleAuthError, sertifikalara ulaşılamazsa
    TransportError fırlatır.
    """
    project_id = settings.firebase_project_id

    payload = google_id_token.verify_firebase_token(
        token,
        _google_request,
        audience=project_id,
        clock_skew_in_seconds=_CLOCK_SKEW_SECONDS,
    )

    if payload.get("iss") != f"https://securetoken.google.com/{project_id}":
        raise ValueError("Beklenmeyen token yayıncısı (iss)")

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 128:
        raise ValueError("Geçersiz kullanıcı kimliği (sub)")

    auth_time = payload.get("auth_time")
    if not isinstance(auth_time, (int, float)) or auth_time > time.time() + _CLOCK_SKEW_SECONDS:
        raise ValueError("Geçersiz kimlik doğrulama zamanı (auth_time)")

    return {
        "uid": sub,
        "email": payload.get("email"),
        "name": payload.get("name"),
    }


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_token_claims(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """Authorization: Bearer <Firebase ID token> başlığını doğrular, claim'leri döner.

    Testlerde bu bağımlılık override edilebilir.
    """
    if creds is None:
        raise _unauthorized("Kimlik doğrulama gerekli")
    try:
        return verify_firebase_id_token(creds.credentials)
    except google.auth.exceptions.TransportError:
        # Google'ın imza sertifikaları alınamadı: istemcinin hatası değil.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Kimlik doğrulama servisine şu anda ulaşılamıyor",
        )
    except (google.auth.exceptions.GoogleAuthError, ValueError):
        # Süresi dolmuş, imzası/audience'ı/yayıncısı hatalı ya da bozuk token.
        # Android tarafı süresi dolan token'ı kendisi yeniler; ayrım yapmaya gerek yok.
        raise _unauthorized("Geçersiz veya süresi dolmuş token")


def get_current_user(
    claims: dict = Depends(get_token_claims),
    db: Session = Depends(get_db),
) -> User:
    """Token'daki Firebase kullanıcısının kaydını döner; ilk girişte oluşturur (upsert)."""
    uid = claims["uid"]
    email = claims.get("email") or None
    name = claims.get("name") or None

    user = db.scalar(select(User).where(User.firebase_uid == uid))

    if user is None:
        user = User(firebase_uid=uid, email=email, name=name)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Aynı kullanıcının eş zamanlı iki isteği aynı anda kayıt açmaya çalıştı.
            db.rollback()
            user = db.scalar(select(User).where(User.firebase_uid == uid))
            if user is None:
                raise
        return user

    # Mevcut kullanıcı: token'da değer varsa güncelle, yoksa eskisini silme.
    changed = False
    if email and user.email != email:
        user.email = email
        changed = True
    if name and user.name != name:
        user.name = name
        changed = True
    if changed:
        db.commit()
    return user
