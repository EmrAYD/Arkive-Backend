# Arkive Backend

FastAPI + PostgreSQL + RustFS (S3 uyumlu). Şu an: Firebase token doğrulama, `GET /me`, klasörler ve ER diyagramındaki 8 tablonun tamamı
(dosya yükleme, abonelik, ödeme, AI sonuçları ve paylaşım uçları sonraki adımlarda eklenecek).

## Çalıştırma — Docker'sız (en kolay yol)

Gerekenler: Python 3.12 (python.org; Windows'ta kurulumda "Add python.exe to PATH" kutusunu işaretleyin).
Aşağıdaki komutlar `backend` klasöründe çalıştırılır. Sanal ortamı "activate" etmeye gerek yok.

**Windows (PowerShell):**

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env                       # sonra .env içindeki FIREBASE_PROJECT_ID'yi doldurun
.venv\Scripts\python -m alembic upgrade head     # tabloları oluşturur
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Mac / Linux:**

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Tarayıcıdan kontrol: `http://localhost:8000/health`, `http://localhost:8000/docs`.
Veri `backend/arkive.db` dosyasındadır; sıfırlamak için sunucuyu durdurup dosyayı silin ve
`alembic upgrade head` komutunu yeniden çalıştırın.

Bu modda PostgreSQL ve RustFS yoktur. PostgreSQL'i sonradan kurarsanız `.env` içindeki
`DATABASE_URL` satırını değiştirmeniz yeterlidir; kod aynı kalır. Dosya depolama (RustFS) dosya
yükleme adımında eklenecek.

## Çalıştırma — Docker ile (PostgreSQL + RustFS dahil)

```bash
cp .env.example .env        # değerleri doldurun (özellikle FIREBASE_PROJECT_ID)
docker compose up -d --build
```

| Servis | Adres |
|---|---|
| API | http://localhost:8000 (belgeler: `/docs`) |
| PostgreSQL | `127.0.0.1:5432` |
| RustFS S3 API | http://localhost:9000 |
| RustFS konsolu | http://localhost:9001 |

Kontrol:

```bash
curl http://localhost:8000/health                      # {"status":"ok"}
curl -i http://localhost:8000/me                        # 401 (token yok)
curl http://localhost:9000/health                       # RustFS ayakta mı
```

`API` başlarken `alembic upgrade head` ile tabloları otomatik oluşturur.

## Android'den bağlanma

- Emülatör bilgisayarı `http://10.0.2.2:8000` adresinden görür.
- Gerçek telefon için bilgisayarın yerel IP'sini kullanın (aynı Wi-Fi).
- Geliştirme sırasında HTTP kullanılacağı için Android 9+ varsayılan olarak cleartext trafiği engeller;
  debug build'e `android:usesCleartextTraffic="true"` (veya network security config) eklenmeli.
- Her istekte: `Authorization: Bearer <FirebaseUser.getIdToken(false) sonucu>`

## Testler

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

Testler PostgreSQL veya internet gerektirmez; Firebase doğrulaması test anahtarıyla imzalanmış
gerçek token'lara karşı çalışır.

## API uçları

Hepsi `Authorization: Bearer <Firebase token>` ister (`/health` hariç). Etkileşimli belge: `/docs`.

| Uç | Ne yapar |
|---|---|
| `GET /me` | Giriş yapan kullanıcının profili, geçerli planı (`plan_name`), toplam sınırı (`storage_limit`) ve kullanımı (`used_storage`), hepsi bayt. İlk çağrıda kullanıcı veritabanına eklenir |
| `POST /folders` | Klasör oluşturur: `{"name", "parent_id"?, "location"?, "color"?, "emoji"?, "favorite"?}`. `parent_id` boşsa kök klasör |
| `GET /folders?location=home` | Ana sayfanın kök klasörleri (`location=ai_results` → AI sonuçları sayfası) |
| `GET /folders?parent_id=<id>` | Bir klasörün içindeki (tek seviye) alt klasörler |
| `GET /folders/{id}` | Tek klasör |
| `PATCH /folders/{id}` | Günceller: `{"name"?, "color"?, "emoji"?, "favorite"?, "parent_id"?}`. Gönderilmeyen alan değişmez; `"color": null` rengi, `"emoji": null` (ya da boş metin) emojiyi siler. `emoji`: klasör simgesinin ortasında gösterilen tek bir emoji (en çok 16 karakter; düz harf/rakam, boşluk kabul edilmez). `parent_id` ile klasör (içindekilerle birlikte) taşınır, `null` = ana sayfanın en üstü; yalnızca `home` klasörleri taşınır, kendi içine/alt klasörüne taşınamaz (400), hedefte aynı ad varsa 409 |
| `GET /folders?favorite=true` | Derinliğinden bağımsız tüm favori klasörler ("Favorilerim") |
| `DELETE /folders/{id}` | Klasörü, alt klasörlerini ve içindeki dosyaları **çöp kutusuna taşır** (204). Kalıcı silme için `/trash` uçları |
| `POST /files/init` | Yüklemeyi başlatır: `{"name", "size", "mime_type"?, "folder_id"?}`. Kuralları denetler, `uploading` kaydı açar; `upload_url` ve `upload_headers` döner |
| *(istemci)* `PUT upload_url` | Dosya backend'den geçmeden doğrudan RustFS'e yüklenir (`upload_headers` aynen eklenir) |
| `POST /files/{id}/complete` | Yüklemeyi tamamlar: backend nesneyi RustFS'te doğrular, dosya `ready` olur, kullanım artar. Tekrar çağrılması zararsızdır |
| `GET /files?folder_id=` | Bir seviyedeki hazır dosyalar (`folder_id` yoksa klasörsüzler) |
| `GET /files?favorite=true` | Tüm favori dosyalar |
| `GET /files/{id}` | Tek dosyanın bilgisi (ön izleme için). Çöpteki dosya için de çalışır (çöp kutusunda ön izleme) |
| `GET /files/{id}/download` | İmzalı indirme/akış adresi (1 saat geçerli; video/ses ön izlemesi bu adresten akar). Çöpteki dosya için de çalışır |
| `PATCH /files/{id}` | `{"name"?, "favorite"?, "folder_id"?}` günceller. `folder_id` ile dosya taşınır (`null` = en üst seviye); yalnızca kendi `home` klasörlerinize taşınabilir |
| `DELETE /files/{id}` | Hazır dosyayı **çöp kutusuna taşır**; yarım kalmış (`uploading`/`failed`) yüklemeyi doğrudan kalıcı siler |
| `PATCH /me` | `{"trash_retention_days": 1..365}` — çöp kutusunda saklama süresi (varsayılan 30). `GET /me` de `trash_retention_days` döner |
| `GET /trash` | Çöp kutusu: `{retention_days, files[], folders[]}`. Her öğede `deleted_at`, `purge_at`, `days_left`. Yalnızca "kök" silinenler listelenir (silinen klasörün içindekiler klasörün altında kalır) |
| `GET /trash/folders/{id}` | Çöpteki bir klasörün içi (salt okunur): `{retention_days, folder, folders[], files[]}`. Klasörle birlikte atılanlar listelenir; iç içe klasörler aynı uçla açılır |
| `POST /trash/restore` | `{"file_ids"?, "folder_ids"?}` geri yükler. Özgün klasör yoksa/çöpteyse en üste döner; klasör adı doluysa "Ad (geri yüklendi)" olur. `{"count"}` döner |
| `POST /trash/delete` | `{"file_ids"?, "folder_ids"?}` seçilenleri **kalıcı** siler (RustFS nesneleri + kullanım sayacı) |
| `DELETE /trash` | Çöp kutusunu boşaltır |
| `GET /files/{id}/summary` | Dosyanın kayıtlı yapay zeka özeti `{summary, source, model, created_at}`; yoksa 404. Dosya nesnelerinde `has_summary` alanı da vardır (uygulamada AI düğmesi buna göre dolu/boş) |
| `POST /files/{id}/summary` | Dosyayı **sunucudaki yapay zekayla özetler ve kaydeder** → `{summary, source:"cloud", model, truncated, created_at}`. Dosyanın (ya da **aynı içerikli** bir kopyasının) özeti zaten varsa yeniden üretilmez, kayıtlı özet döner. Metin, PDF ve DOCX dosyaları (`SUMMARY_FILE_KINDS`). Özet iki yere yazılır: ER diyagramındaki `ai_results` tablosu (tek saklama yeri) ve **AI Sonuçlarım sayfasının en üstüne `<ad> - özet.txt` dosyası** (`files.is_ai_result`; indirilebilir, silinebilir, kotadan düşer, isteğe bağlı AI Sonuçlarım klasörlerine taşınabilir) (`ai_results`: bulutta `processing` → `done`/`failed`, telefonda doğrudan `done`; `result` JSON: özet metni, kaynak, model, sonuç dosyası). Özet yanıtı `result_file_id` ile .txt dosyasını gösterir (uygulama düğmeye basınca doğrudan bunun ön izlemesini açar); bu dosya çöp kutusundan da silinince (`ai_results.result_file_id` FK CASCADE) özet kaydı silinir ve dosya yeniden özetlenebilir, çöpteyken özet geçerlidir. Sonuç dosyaları yeniden özetlenemez (415), `FileOut.in_ai_results` ile işaretlenir. `GET /files?location=ai_results` bu sayfanın en üstündeki dosyaları listeler 415: desteklenmeyen tür, 413: çok büyük, 422: metin çıkarılamadı, 503: LLM yapılandırılmamış, 502: LLM yanıt vermedi. Uygulama bunu yalnızca dosya telefonda özetlenemeyecek kadar büyük/uzun olduğunda ya da ayar "Bulut" olduğunda çağırır |
| `PUT /files/{id}/summary` | **Telefonda** üretilen özeti kaydeder: `{"summary", "model"?}` (en çok 20 bin karakter). Özet zaten varsa mevcut olan korunur ve o döner |

## Bulut yapay zeka özetleme

**Özetler `ai_results` tablosunda saklanır** (kaynak dosya + durum + `result` JSON) ve dosyanın **içeriğine** (`files.content_hash`, SHA-256) bağlıdır: aynı kullanıcının aynı içerikli bir dosyası için tamamlanmış (`done`) sonuç varsa özet hazırdır. Bu yüzden aynı içerik başka adla/yeniden yüklenince yükleme biter bitmez `has_summary: true` olur ve tekrar özetlenmez. İçerik özeti yalnızca özetlenebilir türlerde (yükleme tamamlanınca ya da ilk özetlemede) hesaplanır. Kaynak dosya kalıcı silinse bile özet kaydı ve `<ad> - özet.txt` dosyası **kalır** (`ai_results.file_id` FK `SET NULL`, eşleşme `ai_results.content_hash` üzerinden); aynı içerik yeniden yüklenince düğme dolu gelir ve tekrar özetlenmez. Kayıt yalnızca sonuç dosyası kalıcı silinince (ya da başarısız/devam eden bir kayıtsa kaynak silinince) silinir; özetler kullanıcıya özeldir, kullanıcılar arasında paylaşılmaz.

`POST /files/{id}/summary` dosyayı depolamadan okur, içeriği çıkarır ve **kendi sunucunuzda çalışan** bir modele özetletir (Gemini/ChatGPT gibi hazır servis kullanılmaz; veri dışarı çıkmaz). Model **OpenAI uyumlu** bir `/chat/completions` ucu sunmalıdır. Önerilen model **Qwen2.5-VL**: hem metni hem sayfa görüntülerini okur.

Qwen2.5-VL'i sunmanın iki yolu (GPU'lu bir makinede):

```bash
# vLLM (önerilen). Bir istekteki sayfa sayısı (VISION_PAGES_PER_REQUEST, varsayılan 3) kadar görsele izin verin;
# bayrağın yazımı vLLM sürümüne göre değişir (image=4 ya da JSON), kendi sürümünüzün belgesine bakın.
vllm serve Qwen/Qwen2.5-VL-7B-Instruct --max-model-len 16384 --limit-mm-per-prompt image=4

# Ollama (daha kolay kurulum)
ollama pull qwen2.5vl:7b        # sunucu http://localhost:11434/v1 adresinde OpenAI uyumlu uç da sunar
```

`backend/.env` (Docker'da kök `.env`):

```
LLM_BASE_URL=http://llm-sunucusu:8000/v1   # vLLM; Ollama için http://llm-sunucusu:11434/v1
LLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct      # Ollama için qwen2.5vl:7b
LLM_API_KEY=                               # gerekmiyorsa boş
LLM_TIMEOUT_SECONDS=120
LLM_VISION=true                            # modeliniz görsel okuyamıyorsa false
SUMMARY_FILE_KINDS=text,pdf,docx          # özetlenebilen türler; birini kapatmak için listeden çıkarın
```

**Nasıl çalışır:**
- **Özetlenen türler** `SUMMARY_FILE_KINDS` ile belirlenir (varsayılan `text,pdf,docx`). Düz metin, DOCX ve metin ağırlıklı PDF: metin çıkarılır (`pypdf`, `python-docx`) ve modele metin olarak verilir. Uzun belgeler 12 bin karakterlik parçalara bölünüp parça parça özetlenir, sonra birleştirilir.
- **Görsel ağırlıklı ya da taranmış PDF** (iki sayfada bir görselden fazla ve en az 4 görsel, ya da sayfa başına 20'den az kelime): sayfalar resme çevrilip (`pypdfium2`) çıkarılan metinle birlikte Qwen2.5-VL'e verilir; böylece grafikler, tablolar ve taranmış sayfalar da özete girer. Aynı eşikler uygulamadaki `AiPolicy` ile birebir aynıdır. En çok `VISION_MAX_PAGES` (12) sayfa okunur, fazlası kesilir (`truncated: true`); istek başına `VISION_PAGES_PER_REQUEST` (3) sayfa gider.
- `LLM_VISION=false` ise bu yol kapanır; yazısı olmayan PDF 422 döner.
- DOCX içindeki görseller şimdilik okunmaz (yalnızca metin).

`LLM_BASE_URL` / `LLM_MODEL` boşsa uç 503 döner, uygulamanın geri kalanı etkilenmez. Metin `SUMMARY_MAX_CHARS` (200 bin) karakterden uzunsa kesilir, dosya `SUMMARY_MAX_FILE_BYTES` (25 MB) üstündeyse 413 döner.

## Plan ve depolama sınırı

Kullanıcının geçerli (durumu `active`, süresi dolmamış) aboneliği varsa o planın sınırı, yoksa **Free** planın sınırı
kullanılır (`app/plans.py`). Her kullanıcı için ayrıca Free abonelik satırı açmak gerekmez. Birden çok geçerli abonelik
varsa en yüksek sınırlı olan seçilir. Kalan kullanım = `storage_limit - used_storage`.

## Dosya yükleme

Dosya backend üzerinden **geçmez**; üç adımda yüklenir: `init` → istemci RustFS'e `PUT` → `complete`.
Böylece büyük dosyalar backend'in belleğini/bant genişliğini tüketmez. Güvenlik backend'dedir:

- Sınırlar `init` anında denetlenir: plana göre dosya başına en fazla boyut (Free 10 MB, Plus 2 GB — Plus değeri varsayımdır,
  `app/plans.py`) ve toplam kota. Tamamlanmamış yüklemeler de kotaya sayılır → aşılırsa `413`.
- İmzalı adres Content-Type ve Content-Length'i kapsar; farklı tür/boyutla yükleme reddedilir.
- `complete`, istemciye güvenmez: nesneyi RustFS'te `HEAD` ile kendisi doğrular; boyut uymazsa nesne silinir, kayıt `failed` olur.
- Kullanım sayacı yalnızca `complete` ile artar ve yalnızca bir kez (koşullu `UPDATE`).
- Nesne adı `<user_id>/<file_id>`'dir; görünen ad yalnızca veritabanındadır, bu yüzden yeniden adlandırma RustFS'e dokunmaz.
- Dosyalar yalnızca `home` konumundaki klasörlere (ya da klasörsüz) yüklenir.
- Silme önce çöp kutusuna gider (`deleted_at`). Çöpteki dosyalar kalıcı silinene kadar kotada sayılır. Sahipsiz kalan `uploading`/`failed` kayıtları için ileride periyodik temizlik gerekir.

`backend/.env` dosyasına `S3_*` ayarlarını ekleyin (bkz. `backend/.env.example`), `pip install -r requirements.txt` çalıştırın,
sonra gerçek RustFS ile denemek için: `python -m scripts.storage_check`.
Gerçek telefonda test için RustFS `--address 0.0.0.0:9000` ile başlatılmalı ve `S3_PUBLIC_ENDPOINT` bilgisayarın yerel IP'si olmalıdır.

## Klasör kuralları

- Klasör yalnızca sahibine görünür. Başkasının klasörü için `404` döner (varlığı bile sızdırılmaz).
- `location`: `home` (ana sayfa) veya `ai_results`. Yalnızca kök klasörlerde seçilir; alt klasör üst klasörünün konumunu devralır, çelişen konum `400` döner.
- Aynı yerde (aynı üst klasör; kökteyse aynı sayfa) aynı ad kullanılamaz, harf büyüklüğü fark etmez → `409`.
- Ad 1-255 karakter, baştaki/sondaki boşluklar atılır, `/` ve `\` içeremez → `422`.
- Silme, alt klasörleri ve dosya kayıtlarını veritabanı seviyesinde (`ON DELETE CASCADE`) siler; RustFS nesnelerini ve
  kullanım sayacını backend ayrıca temizler.

## Çöp kutusu

- Silinen dosya/klasör `deleted_at` ile işaretlenir; klasörle birlikte silinen her şey **aynı zaman damgasını** alır, böylece geri yüklemede birlikte döner.
- Saklama süresi kullanıcı başınadır (`users.trash_retention_days`, varsayılan 30, 1-365; `PATCH /me`). Ayar ekranı henüz yok.
- Süresi dolanlar saatte bir arka plan görevi (`TRASH_PURGE_INTERVAL_SECONDS`, varsayılan 3600) ve `GET /trash` çağrılırken kalıcı silinir.
- **Migration 0010 gerekir:** `python -m alembic upgrade head` (silinmiş klasörün adı yeniden kullanılabilsin diye ad kuralı `deleted_at IS NULL` ile sınırlandı).
- **Migration 0011 gerekir:** `python -m alembic upgrade head` — kullanıcının görünen adı artık tutulmaz; `users.name` sütunu silinir ve `/me` yanıtında `name` alanı yoktur (kullanıcı e-postayla tanınır). Silinen adlar geri gelmez.
- **Migration 0012 gerekir:** `python -m alembic upgrade head` — `folders.emoji` sütunu eklenir (isteğe bağlı, varsayılan boş). `GET /folders`, `GET /trash` yanıtlarında klasörlerde `emoji` alanı döner.

## Veritabanı tabloları (ER diyagramı)

`alembic upgrade head` komutu 8 tablonun hepsini oluşturur. Her tablo kendi migration dosyasındadır
(`backend/alembic/versions/`).

| Tablo | Migration | Not |
|---|---|---|
| `users` | 0001 | `firebase_uid`, Firebase'deki kullanıcıyla bağlar. Şifre Firebase'de tutulur |
| `folders` | 0002, 0003 | `parent_id` ile iç içe klasör; `color`, `favorite` 0003'te eklendi |
| `files` | 0004 | `storage_key` = RustFS'teki nesne adı; `folder_id` boşsa klasörsüz |
| `subscription_plans` | 0005 | Başlangıçta **Free** (toplam 100 MB) ve **Plus** (100 GB) eklenir |
| `user_subscriptions` | 0006 | Kullanıcı ↔ plan. Aboneliği olan plan silinemez |
| `payments` | 0007 | Ödemesi olan abonelik silinemez (mali kayıt korunur) |
| `ai_results` | 0008, 0017, 0018 | `file_id` isteğe bağlı (kaynak silinince `SET NULL`), `content_hash` (indeksli, aynı içerik eşleşmesi), `result_file_id` (özet .txt dosyası, CASCADE), `folder_id` isteğe bağlı; `result` PostgreSQL'de JSONB |
| `shares` | 0009 | Kendinle paylaşım yok; aynı dosya aynı kişiyle bir kez paylaşılır |

Silme kuralları: kullanıcı silinirse dosya, klasör, abonelik ve paylaşımları da silinir. Klasör silinirse
içindeki dosyalar ve alt klasörler silinir. Dosya silinirse AI sonuçları ve paylaşımları silinir.
RustFS'teki dosya nesneleri veritabanı tarafından silinmez; yükleme/silme kodunda ayrıca silinmelidir.

**Dosya başına boyut sınırı** (Free için 10 MB) veritabanında değil, yükleme kodunda kontrol edilecek.
Toplam sınır `subscription_plans.storage_limit` ve `users.used_storage` ile karşılaştırılır.

## PostgreSQL ile çalıştırma (Docker'sız, kendi kurulumunuz)

1. pgAdmin'de **Databases → sağ tık → Create → Database...** ile `arkive` adında boş bir veritabanı açın.
2. `backend/.env` dosyasında SQLite satırını silip şunu yazın (şifreniz `postgres` kullanıcısının şifresi):

   ```
   DATABASE_URL=postgresql+psycopg://postgres:SIFRENIZ@localhost:5432/arkive
   ```

3. `backend` klasöründe `python -m alembic upgrade head` çalıştırın. 8 tablo oluşur.
4. pgAdmin'de `arkive → Schemas → public → Tables → Refresh` ile görün.

Şemayı değiştirmek (ör. sütun eklemek) için "Notlar" bölümündeki adımları izleyin; tabloyu silip
baştan kurmaya gerek kalmaz.

## Notlar

- Yeni tablo eklemek için: `app/models.py`'a modeli yaz → `alembic revision -m "açıklama"` ile migration dosyası
  oluştur → `alembic upgrade head`. `alembic check` komutu, modeller ile migration'ların uyumlu olup olmadığını söyler.

- Token doğrulama servis hesabı anahtarı gerektirmez, yalnızca `FIREBASE_PROJECT_ID`
  (proje kimliği; proje numarası değil).
- RustFS için varsayılan `rustfsadmin` kimliği kullanılmamalı; compose, anahtarlar `.env`'de
  yoksa başlamaz.
