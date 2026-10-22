"""RustFS bağlantı ve imzalı adres denetimi. Backend klasöründe çalıştırın:

    python -m scripts.storage_check

Testler sahte (bellekteki) depolama kullandığı için gerçek RustFS ile imzalı adreslerin uyuşup
uyuşmadığını yalnızca bu betik doğrular. Yükleme özelliğine güvenmeden önce bir kez çalıştırın.
Betik kendi geçici nesnesini oluşturur ve sonunda siler.
"""
import sys
import uuid

import requests

from app.config import settings
from app.storage import Storage, StorageError


def step(ok: bool, text: str) -> None:
    print(("  [OK]   " if ok else "  [HATA] ") + text)
    if not ok:
        sys.exit(1)


def main() -> None:
    if not (settings.s3_access_key and settings.s3_secret_key):
        sys.exit("S3_ACCESS_KEY / S3_SECRET_KEY .env dosyasında tanımlı değil.")

    # İmzalı adresleri BU bilgisayardan deneyeceğimiz için public adres yerine iç adresle imzalıyoruz
    # (10.0.2.2 yalnızca emülatörün içinden erişilebilir). İmza mantığı ikisinde de aynıdır.
    st = Storage(
        endpoint=settings.s3_endpoint,
        public_endpoint=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        bucket=settings.s3_bucket,
        region=settings.s3_region,
    )
    key = f"_check/{uuid.uuid4()}"
    data = "Arkive deneme içeriği 123".encode("utf-8")
    print(f"RustFS: {settings.s3_endpoint}  kova: {settings.s3_bucket}")

    try:
        st._internal.head_bucket(Bucket=settings.s3_bucket)
        step(True, "kovaya erişildi")

        url = st.presign_put(key, "text/plain", len(data), 60)
        r = requests.put(url, data=data, headers={"Content-Type": "text/plain"}, timeout=15)
        step(r.status_code in (200, 201), f"imzalı PUT ile yüklendi (HTTP {r.status_code}) {r.text[:200]}")

        step(st.object_size(key) == len(data), "HEAD ile boyut doğrulandı")
        step(st.object_size(key + "-yok") is None, "olmayan nesne için None döndü")

        # Content-Length imzaya dahil: farklı boyutla yükleme reddedilmeli (bu bir güvenlik özelliği).
        bad_url = st.presign_put(key + "-bad", "text/plain", 5, 60)
        rb = requests.put(bad_url, data=data, headers={"Content-Type": "text/plain"}, timeout=15)
        print(
            f"  [BİLGİ] imzadaki boyuttan farklı yükleme: HTTP {rb.status_code} "
            f"({'reddedildi, beklenen' if rb.status_code >= 400 else 'KABUL EDİLDİ: complete adımı boyutu yine denetler'})"
        )
        if rb.status_code < 400:
            st.delete(key + "-bad")

        g = requests.get(st.presign_get(key, "Rapor çalışma.txt", 60), timeout=15)
        step(g.status_code == 200 and g.content == data, "imzalı GET ile içerik aynen indi")
        cd = g.headers.get("Content-Disposition", "")
        step("attachment" in cd, f"Content-Disposition geldi: {cd}")
    except (StorageError, Exception) as e:  # noqa: BLE001 - kullanıcıya düz mesaj verilir
        print(f"  [HATA] {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        try:
            st.delete(key)
            print("  geçici nesne silindi")
        except Exception:  # noqa: BLE001
            print(f"  UYARI: geçici nesne silinemedi: {key}")

    print("Hepsi tamam. Yükleme akışı çalışmaya hazır.")


if __name__ == "__main__":
    main()
