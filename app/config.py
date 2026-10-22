from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # postgresql+psycopg://kullanici:sifre@host:5432/veritabani
    database_url: str

    # Firebase proje KİMLİĞİ. Token'ın "aud" alanı bununla karşılaştırılır; boş bırakılırsa
    # audience kontrolü atlanırdı, bu yüzden zorunlu (uygulama boşsa hiç başlamaz).
    firebase_project_id: str = Field(min_length=1)

    # --- Dosya depolama (RustFS, S3 uyumlu) ---
    # Anahtarlar boşsa uygulama yine başlar, yalnızca dosya uçları 503 döner.
    s3_access_key: str = ""
    s3_secret_key: str = ""
    # Backend'in RustFS'e bağlandığı adres.
    s3_endpoint: str = "http://localhost:9000"
    # İstemcinin (telefon/emülatör) RustFS'e bağlanacağı adres. Boşsa s3_endpoint kullanılır.
    # İmzalı yükleme adresi bu adresle imzalanır; emülatör için http://10.0.2.2:9000 gibi.
    s3_public_endpoint: str = ""
    s3_bucket: str = "arkive-files"
    s3_region: str = "us-east-1"
    # İmzalı adreslerin geçerlilik süresi (saniye). İndirme adresi uzun: ön izlemede video/ses bu adresten
    # akış olarak oynatılır ve ileri sarılınca aynı adrese yeniden istek atılır; süre dolarsa oynatma kopar.
    upload_url_expires_seconds: int = 900
    download_url_expires_seconds: int = 3600

    # Çöp kutusunda süresi dolanları kalıcı silen arka plan görevinin çalışma aralığı (saniye).
    trash_purge_interval_seconds: int = 3600


settings = Settings()
