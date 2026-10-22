"""Dosya depolama (RustFS / S3) ile konuşan katman.

Dosyanın kendisi backend'den GEÇMEZ: istemci, backend'in ürettiği süreli ve imzalı bir adrese
doğrudan yükler/indirir. Backend yalnızca (1) bu adresleri üretir, (2) yükleme bitince nesnenin
gerçekten orada ve doğru boyutta olduğunu doğrular, (3) gerektiğinde nesneyi siler.
"""
import logging
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status

from app.config import settings

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Depolama sunucusuna ulaşılamadı ya da beklenmeyen bir hata verdi."""


class Storage:
    def __init__(
        self,
        *,
        endpoint: str,
        public_endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
    ) -> None:
        self.bucket = bucket

        def make(url: str):
            return boto3.client(
                "s3",
                endpoint_url=url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=Config(
                    signature_version="s3v4",
                    # RustFS "path-style" adres bekler: http://host:9000/bucket/anahtar
                    s3={"addressing_style": "path"},
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"max_attempts": 2},
                ),
            )

        # İç istemci: backend'in RustFS'e doğrudan bağlandığı adres (HEAD, silme).
        self._internal = make(endpoint)
        # Dış istemci: yalnızca imzalı adres üretir, ağa çıkmaz. İmza, adresteki "host" bilgisini de
        # kapsar; bu yüzden istemcinin (ör. emülatörün 10.0.2.2) kullanacağı adresle üretilmelidir.
        self._public = make(public_endpoint or endpoint)

    def presign_put(self, key: str, content_type: str, size: int, expires: int) -> str:
        """Yükleme adresi. Content-Type ve Content-Length İMZAYA DAHİLDİR: istemci farklı bir tür ya da
        boyutla yüklemeye çalışırsa depolama sunucusu isteği reddeder."""
        return self._public.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": size,
            },
            ExpiresIn=expires,
        )

    def presign_get(self, key: str, filename: str, expires: int) -> str:
        """İndirme adresi. Tarayıcı dosyayı 'filename' adıyla kaydetsin diye Content-Disposition eklenir."""
        from urllib.parse import quote

        disposition = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
        return self._public.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=expires,
        )

    def object_size(self, key: str) -> int | None:
        """Nesnenin bayt cinsinden boyutu; nesne yoksa None."""
        try:
            return self._internal.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(str(e)) from e
        except BotoCoreError as e:
            raise StorageError(str(e)) from e

    def delete(self, key: str) -> None:
        try:
            self._internal.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as e:
            raise StorageError(str(e)) from e


@lru_cache
def _build() -> Storage:
    return Storage(
        endpoint=settings.s3_endpoint,
        public_endpoint=settings.s3_public_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
        region=settings.s3_region,
    )


def get_optional_storage() -> Storage | None:
    """Anahtarlar yapılandırılmamışsa None. Dosya gerektirmeyen işler (ör. boş klasör silme) yine çalışır."""
    if not (settings.s3_access_key and settings.s3_secret_key):
        return None
    return _build()


def get_storage() -> Storage:
    storage = get_optional_storage()
    if storage is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Dosya depolama yapılandırılmamış (S3_ACCESS_KEY / S3_SECRET_KEY)",
        )
    return storage
