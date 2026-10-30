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

    # --- Bulut yapay zeka özetleme ---
    # OpenAI uyumlu bir sohbet uç noktası (kendi sunucunuzdaki vLLM, Ollama, llama.cpp vb.). Adres boşsa özetleme ucu
    # 503 döner; geri kalan uygulama etkilenmez. Örn: http://llm:8000/v1
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = 120
    # Uzun belgede parçaların aynı anda LLM'e gönderilecek sayısı. llama-server'ı `-np` ile aynı değerde başlatın
    # (ör. `-np 4 -c 32768`): üretim bellek hızıyla sınırlı olduğundan birkaç isteği birlikte işlemek toplamı kısaltır.
    # Sunucunun slotu azsa fazla istekler sırada bekler. 1: sırayla.
    llm_concurrency: int = 4
    # Model görsel de okuyabiliyorsa (Qwen3.5 gibi) true: görsel ağırlıklı/taranmış PDF'lerin sayfaları resim olarak
    # modele gönderilir. Metin-yalnızca bir modelde false yapın; o zaman yazısız PDF 422 döner.
    llm_vision: bool = True
    # Özetlenebilen dosya türleri (virgülle): text, pdf, docx. Birini kapatmak için listeden çıkarın (ör. SUMMARY_FILE_KINDS=text).
    summary_file_kinds: str = "text,pdf,docx"
    # Bir PDF'ten modele gönderilecek en çok sayfa, istek başına sayfa ve sayfanın çizim ölçeği (1.0 = 72 dpi).
    vision_max_pages: int = 12
    vision_pages_per_request: int = 3
    vision_render_scale: float = 1.4
    # Özetlenecek dosyanın en büyük boyutu (bayt) ve çıkarılan metnin en çok karakteri (fazlası kesilir).
    summary_max_file_bytes: int = 25 * 1024 * 1024
    summary_max_chars: int = 200_000

    # Çöp kutusunda süresi dolanları kalıcı silen arka plan görevinin çalışma aralığı (saniye).
    trash_purge_interval_seconds: int = 3600


settings = Settings()
