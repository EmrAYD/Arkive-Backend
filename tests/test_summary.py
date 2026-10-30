import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from docx import Document
from pypdf import PdfWriter

from app import summarize
from app.config import settings


def _make_pdf_with_text(text: str) -> bytes:
    """İçinde gerçek metin olan en küçük PDF (tek sayfa, Helvetica)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return out


def _blank_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _docx(text: str) -> bytes:
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture()
def llm(monkeypatch):
    """LLM yapılandırılmış ve çağrıları kaydedilen sahte sohbet."""
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    monkeypatch.setattr(settings, "summary_file_kinds", "text,pdf,docx")  # varsayılan yalnızca "text"; testler hepsini açar
    calls: list[tuple[str, str]] = []

    def fake_chat(system, user, max_tokens=None):
        calls.append((system, user))
        return "ÖZET: kısa"

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    return calls


def _ready_file(client, storage, name, data: bytes, mime=None):
    extra = {"mime_type": mime} if mime else {}
    r = client.post("/files/init", json={"name": name, "size": len(data), **extra})
    assert r.status_code == 201, r.text
    meta = r.json()
    key = meta["upload_url"].removeprefix("https://fake-storage.test/put/")
    storage.objects[key] = len(data)
    storage.contents[key] = data
    done = client.post(f"/files/{meta['file']['id']}/complete")
    assert done.status_code == 200, done.text
    return done.json()["id"]


def test_requires_login(client, storage):
    assert client.post("/files/00000000-0000-0000-0000-000000000000/summary").status_code == 401


def test_summarizes_text_file(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", "Toplantı salı günü yapılacak.".encode(), "text/plain")
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["summary"], body["model"], body["source"], body["truncated"]) == ("ÖZET: kısa", "test-model", "cloud", False)
    assert "Toplantı salı günü" in llm[0][1]  # belge LLM'e gitti
    assert "yönergeye" in llm[0][0]            # sistem mesajı: belgedeki komutlara uyma


def test_summarizes_pdf_and_docx(client, storage, login_as, llm):
    login_as(uid="u1")
    pdf = _ready_file(client, storage, "rapor.pdf", _make_pdf_with_text("Hello summary world " + "word " * 30))
    assert client.post(f"/files/{pdf}/summary").status_code == 200
    assert "Hello summary world" in llm[-1][1]
    docx = _ready_file(client, storage, "tez.docx", _docx("Docx icerigi burada"))
    assert client.post(f"/files/{docx}/summary").status_code == 200
    assert "Docx icerigi burada" in llm[-1][1]


def test_turkish_legacy_encoding_is_decoded(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "eski.txt", "çalışma öğrenci ğüşiİ".encode("cp1254"))
    assert client.post(f"/files/{fid}/summary").status_code == 200
    assert "çalışma öğrenci" in llm[-1][1]


def test_scanned_pdf_without_text_is_422_when_model_has_no_vision(client, storage, login_as, llm, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "llm_vision", False)
    fid = _ready_file(client, storage, "tarama.pdf", _blank_pdf())
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 422
    assert "metin" in r.json()["detail"]
    assert llm == []  # metin yoksa LLM çağrılmaz


def _image_pdf(pages: int) -> bytes:
    """Her sayfası tek bir resimden ibaret (taranmış belge gibi) PDF."""
    from PIL import Image

    imgs = [Image.new("RGB", (200, 300), (i * 40 % 255, 100, 200)) for i in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def test_scanned_pdf_is_sent_as_page_images_to_vision_model(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "tarama.pdf", _image_pdf(2))
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 200, r.text
    assert len(llm) == 1                       # 2 sayfa tek istekte
    content = llm[0][1]
    assert isinstance(content, list)
    images = [p for p in content if p["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert r.json()["truncated"] is False


def test_many_pages_are_batched_and_capped(client, storage, login_as, llm, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "vision_max_pages", 5)
    monkeypatch.setattr(settings, "vision_pages_per_request", 2)
    fid = _ready_file(client, storage, "tarama.pdf", _image_pdf(7))
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 200
    assert len(llm) == 3 + 1                   # 5 sayfa => 2+2+1 grup + birleştirme
    assert r.json()["truncated"] is True       # 7 sayfanın yalnızca 5'i okundu


def test_text_pdf_with_enough_words_stays_text_only(client, storage, login_as, llm):
    login_as(uid="u1")
    words = " ".join(["kelime"] * 60)           # tek sayfada 60 kelime: taranmış sayılmaz
    fid = _ready_file(client, storage, "metin.pdf", _make_pdf_with_text(words))
    assert client.post(f"/files/{fid}/summary").status_code == 200
    assert isinstance(llm[0][1], str)           # görsel gönderilmedi


def test_visual_heuristics():
    assert summarize._is_visual(words=900, pages=3, images=9) is True    # çok görsel
    assert summarize._is_visual(words=900, pages=40, images=6) is False  # oran düşük
    # 38 slaytlık ders sunumu: her sayfada resim var ama yazı da var. Resim yolu yalnızca ilk 12 sayfayı okurdu, bu yüzden metin yolu.
    assert summarize._is_visual(words=2200, pages=38, images=33) is False
    assert summarize._is_visual(words=10, pages=38, images=33) is True   # aynı uzunlukta ama taranmış: resim yolu
    assert summarize._is_visual(words=10, pages=5, images=0) is True     # taranmış
    assert summarize._is_visual(words=900, pages=3, images=2) is False


def test_corrupt_pdf_is_422(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "bozuk.pdf", b"bu bir pdf degil")
    assert client.post(f"/files/{fid}/summary").status_code == 422


def test_unsupported_type_is_415(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "foto.png", b"\x89PNG....", "image/png")
    assert client.post(f"/files/{fid}/summary").status_code == 415


def test_too_large_is_413(client, storage, login_as, llm, monkeypatch):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "uzun.txt", b"a" * 100, "text/plain")
    monkeypatch.setattr(settings, "summary_max_file_bytes", 50)
    assert client.post(f"/files/{fid}/summary").status_code == 413


def test_llm_not_configured_is_503(client, storage, login_as, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "llm_base_url", "")
    fid = _ready_file(client, storage, "a.txt", b"merhaba", "text/plain")
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 503


def test_llm_failure_is_502(client, storage, login_as, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9/v1")  # kapalı port
    monkeypatch.setattr(settings, "llm_model", "m")
    monkeypatch.setattr(settings, "llm_timeout_seconds", 2)
    fid = _ready_file(client, storage, "a.txt", b"merhaba", "text/plain")
    assert client.post(f"/files/{fid}/summary").status_code == 502


def test_other_users_file_is_404(client, storage, login_as, llm):
    login_as(uid="u1", email="a@a.com")
    fid = _ready_file(client, storage, "a.txt", b"merhaba", "text/plain")
    login_as(uid="u2", email="b@b.com")
    assert client.post(f"/files/{fid}/summary").status_code == 404


def test_trashed_file_is_404(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba", "text/plain")
    assert client.delete(f"/files/{fid}").status_code == 204
    assert client.post(f"/files/{fid}/summary").status_code == 404


def test_long_text_is_summarized_in_parts_then_merged(client, storage, login_as, llm):
    login_as(uid="u1")
    big = ("Bu cümle belgenin bir parçasıdır. " * 40 + "\n") * 40   # ~55 bin karakter => birkaç parça
    fid = _ready_file(client, storage, "uzun.txt", big.encode(), "text/plain")
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 200
    parts = summarize._chunks(summarize.extract(big.encode(), "uzun.txt", "text/plain").text)
    assert len(parts) > 1
    assert len(llm) == len(parts) + 1                  # her parça + birleştirme
    assert "BÖLÜM ÖZETLERİ" in llm[-1][1]


def test_text_beyond_limit_is_truncated_flag(client, storage, login_as, llm, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "summary_max_chars", 100)
    fid = _ready_file(client, storage, "uzun.txt", ("kelime " * 100).encode(), "text/plain")
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 200 and r.json()["truncated"] is True


# --- Kayıtlı özet: has_summary, GET/PUT, tekrar özetleme yok, aynı içerik tek özeti paylaşır ------------------------


def _file(client, fid):
    return client.get(f"/files/{fid}").json()


def test_default_kinds_include_text_pdf_and_docx():
    from app.summarize import enabled_kinds

    assert settings.summary_file_kinds == "text,pdf,docx"
    assert enabled_kinds() == {"text", "pdf", "docx"}


def test_kinds_can_be_restricted_to_plain_text(client, storage, login_as, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "llm_model", "m")
    monkeypatch.setattr(settings, "summary_file_kinds", "text")
    pdf = _ready_file(client, storage, "rapor.pdf", _make_pdf_with_text("x " * 50))
    docx = _ready_file(client, storage, "tez.docx", _docx("merhaba"))
    assert client.post(f"/files/{pdf}/summary").status_code == 415
    assert client.post(f"/files/{docx}/summary").status_code == 415
    assert client.put(f"/files/{pdf}/summary", json={"summary": "x"}).status_code == 415


def test_has_summary_flag_follows_the_summary(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    assert _file(client, fid)["has_summary"] is False
    assert client.get(f"/files/{fid}/summary").status_code == 404
    assert client.post(f"/files/{fid}/summary").status_code == 200
    assert _file(client, fid)["has_summary"] is True
    assert client.get("/files").json()[0]["has_summary"] is True
    got = client.get(f"/files/{fid}/summary").json()
    assert got["summary"] == "ÖZET: kısa" and got["source"] == "cloud"


def test_second_summarize_returns_stored_summary_without_calling_llm(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    first = client.post(f"/files/{fid}/summary").json()
    calls_after_first = len(llm)
    second = client.post(f"/files/{fid}/summary")
    assert second.status_code == 200
    assert second.json()["summary"] == first["summary"]
    assert len(llm) == calls_after_first  # ikinci kez üretilmedi


def test_local_summary_is_saved_and_blocks_cloud_resummary(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    r = client.put(f"/files/{fid}/summary", json={"summary": "  Telefonda üretildi.  ", "model": "gemma3-1b"})
    assert r.status_code == 200, r.text
    assert (r.json()["summary"], r.json()["source"], r.json()["model"]) == ("Telefonda üretildi.", "local", "gemma3-1b")
    assert _file(client, fid)["has_summary"] is True
    # Özet varken bulut isteği yeniden üretmez; telefonun özeti döner
    again = client.post(f"/files/{fid}/summary").json()
    assert again["summary"] == "Telefonda üretildi." and llm == []
    # Özet varken ikinci PUT mevcut özeti ezmez
    r2 = client.put(f"/files/{fid}/summary", json={"summary": "Başka"})
    assert r2.json()["summary"] == "Telefonda üretildi."


def test_put_validates_summary(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba", "text/plain")
    assert client.put(f"/files/{fid}/summary", json={"summary": "   "}).status_code == 422
    assert client.put(f"/files/{fid}/summary", json={"summary": "x" * 20_001}).status_code == 422
    assert client.put(f"/files/{fid}/summary", json={}).status_code == 422


def test_reuploaded_identical_file_is_already_summarized(client, storage, login_as, llm):
    login_as(uid="u1")
    first = _ready_file(client, storage, "a.txt", b"ayni icerik", "text/plain")
    assert client.post(f"/files/{first}/summary").status_code == 200
    # Aynı içerik, başka adla yeniden yükleniyor: yükleme biter bitmez has_summary true, özet hazır.
    second = _ready_file(client, storage, "kopya.txt", b"ayni icerik", "text/plain")
    assert _file(client, second)["has_summary"] is True
    assert client.get(f"/files/{second}/summary").json()["summary"] == "ÖZET: kısa"
    calls = len(llm)
    assert client.post(f"/files/{second}/summary").status_code == 200
    assert len(llm) == calls  # tekrar özetlenmedi


def test_complete_response_already_carries_has_summary(client, storage, login_as, llm):
    login_as(uid="u1")
    first = _ready_file(client, storage, "a.txt", b"ayni", "text/plain")
    client.post(f"/files/{first}/summary")
    data = b"ayni"
    meta = client.post("/files/init", json={"name": "b.txt", "size": len(data), "mime_type": "text/plain"}).json()
    key = meta["upload_url"].removeprefix("https://fake-storage.test/put/")
    storage.objects[key] = len(data)
    storage.contents[key] = data
    done = client.post(f"/files/{meta['file']['id']}/complete").json()
    assert done["has_summary"] is True


def test_different_content_is_not_summarized(client, storage, login_as, llm):
    login_as(uid="u1")
    a = _ready_file(client, storage, "a.txt", b"bir", "text/plain")
    client.post(f"/files/{a}/summary")
    b = _ready_file(client, storage, "b.txt", b"iki", "text/plain")
    assert _file(client, b)["has_summary"] is False


def test_summary_is_not_shared_between_users(client, storage, login_as, llm):
    login_as(uid="u1", email="a@a.com")
    a = _ready_file(client, storage, "a.txt", b"ayni icerik", "text/plain")
    client.post(f"/files/{a}/summary")
    login_as(uid="u2", email="b@b.com")
    b = _ready_file(client, storage, "a.txt", b"ayni icerik", "text/plain")
    assert _file(client, b)["has_summary"] is False
    assert client.get(f"/files/{b}/summary").status_code == 404


def test_summary_survives_deleting_the_last_source_file_and_is_shared_with_copies(
    client, storage, login_as, llm, session_factory
):
    from sqlalchemy import select
    from app.models import AIResult

    login_as(uid="u1")
    a = _ready_file(client, storage, "a.txt", b"ayni icerik", "text/plain")
    client.post(f"/files/{a}/summary")
    b = _ready_file(client, storage, "b.txt", b"ayni icerik", "text/plain")

    def summaries():
        with session_factory() as db:
            return len(db.scalars(select(AIResult)).all())

    assert summaries() == 1
    client.delete(f"/files/{a}")
    client.request("POST", "/trash/delete", json={"file_ids": [a], "folder_ids": []})
    assert summaries() == 1                      # kopya (b) hâlâ var: sonuç kaydı ona devredildi
    assert _file(client, b)["has_summary"] is True
    assert client.get(f"/files/{b}/summary").json()["summary"] == "ÖZET: kısa"
    client.delete(f"/files/{b}")
    client.request("POST", "/trash/delete", json={"file_ids": [b], "folder_ids": []})
    # Son kaynak dosya gitti ama özet ve .txt dosyası kalır: aynı dosya yeniden yüklenince özet hazır gelir.
    assert summaries() == 1
    again = _ready_file(client, storage, "yeni-ad.txt", b"ayni icerik", "text/plain")
    assert _file(client, again)["has_summary"] is True
    assert client.get(f"/files/{again}/summary").json()["summary"] == "ÖZET: kısa"
    assert len(_ai_root_files(client)) == 1


def test_rename_keeps_summary(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba", "text/plain")
    client.post(f"/files/{fid}/summary")
    client.patch(f"/files/{fid}", json={"name": "yeni.txt"})
    assert _file(client, fid)["has_summary"] is True


def test_truncated_note_is_stored_with_the_summary(client, storage, login_as, llm, monkeypatch):
    login_as(uid="u1")
    monkeypatch.setattr(settings, "summary_max_chars", 100)
    fid = _ready_file(client, storage, "uzun.txt", ("kelime " * 100).encode(), "text/plain")
    r = client.post(f"/files/{fid}/summary").json()
    assert r["truncated"] is True and "yalnızca ilk bölümü" in r["summary"]
    assert "yalnızca ilk bölümü" in client.get(f"/files/{fid}/summary").json()["summary"]


# --- Özet, "AI Sonuçlarım" sayfasının en üstünde .txt dosyası olur; ai_results tablosuna da yazılır -------------------------


def _ai_root_files(client):
    return client.get("/files", params={"location": "ai_results"}).json()


def _home_root_files(client):
    return client.get("/files").json()


def _ai_rows(session_factory):
    from sqlalchemy import select

    from app.models import AIResult

    with session_factory() as db:
        return list(db.scalars(select(AIResult).order_by(AIResult.date)))


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def test_summary_becomes_a_pdf_file_at_the_top_of_ai_results(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    used_before = client.get("/me").json()["used_storage"]
    assert client.post(f"/files/{fid}/summary").status_code == 200

    files = _ai_root_files(client)
    assert [f["name"] for f in files] == ["notlar - özet.pdf"]
    result = files[0]
    assert result["folder_id"] is None and result["mime_type"] == "application/pdf"
    assert result["status"] == "ready" and result["in_ai_results"] is True
    data = storage.contents[f"{client.get('/me').json()['id']}/{result['id']}"]
    assert data.startswith(b"%PDF")
    text = _pdf_text(data)
    assert "Arkive ile özetlenmiştir" in text and "Kaynak dosya: notlar.txt" in text and "kısa" in text
    assert client.get("/me").json()["used_storage"] == used_before + result["size"]
    assert _file(client, fid)["in_ai_results"] is False
    # Ana sayfanın en üstünde görünmez; kaynak dosya orada kalır.
    assert [f["id"] for f in _home_root_files(client)] == [fid]
    # Özetler klasörü artık oluşturulmaz.
    assert client.get("/folders", params={"location": "ai_results"}).json() == []


def test_local_summary_is_saved_as_file_too(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    assert client.put(f"/files/{fid}/summary", json={"summary": "Telefonda yazıldı.", "model": "m"}).status_code == 200
    files = _ai_root_files(client)
    assert len(files) == 1
    key = f"{client.get('/me').json()['id']}/{files[0]['id']}"
    assert "Telefonda yazıldı." in _pdf_text(storage.contents[key])


def test_ai_results_table_gets_a_done_row_for_cloud_summary(client, storage, login_as, llm, session_factory):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{fid}/summary")
    rows = _ai_rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert str(row.file_id) == fid and row.status == "done" and row.folder_id is None
    assert row.result["kind"] == "summary" and row.result["summary"] == "ÖZET: kısa"
    assert row.result["source"] == "cloud" and row.result["model"] == "test-model"
    assert str(row.result_file_id) == _ai_root_files(client)[0]["id"]
    assert "result_file_id" not in row.result


def test_ai_results_table_gets_a_done_row_for_local_summary(client, storage, login_as, llm, session_factory):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.put(f"/files/{fid}/summary", json={"summary": "Telefonda yazıldı.", "model": "gemma"})
    (row,) = _ai_rows(session_factory)
    assert row.status == "done" and row.result["source"] == "local" and row.result["model"] == "gemma"


def test_failed_cloud_summary_leaves_a_failed_row_that_is_reused(client, storage, login_as, llm, session_factory, monkeypatch):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")

    def boom(system, user, max_tokens=None):
        raise summarize.SummaryError(502, "Yapay zeka yanıt vermedi.")

    monkeypatch.setattr(summarize, "_chat", boom)
    assert client.post(f"/files/{fid}/summary").status_code == 502
    assert client.post(f"/files/{fid}/summary").status_code == 502
    (row,) = _ai_rows(session_factory)  # ikinci deneme yeni satır açmadı
    assert row.status == "failed" and "yanıt vermedi" in row.result["error"]
    assert _file(client, fid)["has_summary"] is False

    monkeypatch.setattr(summarize, "_chat", lambda system, user, max_tokens=None: "ÖZET: düzeldi")
    assert client.post(f"/files/{fid}/summary").status_code == 200
    (row,) = _ai_rows(session_factory)
    assert row.status == "done" and row.result["summary"] == "ÖZET: düzeldi"


def test_second_summary_request_does_not_create_another_file_or_row(client, storage, login_as, llm, session_factory):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{fid}/summary")
    client.post(f"/files/{fid}/summary")
    client.put(f"/files/{fid}/summary", json={"summary": "başka"})
    assert len(_ai_root_files(client)) == 1
    assert len(_ai_rows(session_factory)) == 1


def test_same_named_sources_get_numbered_result_names(client, storage, login_as, llm):
    login_as(uid="u1")
    first = _ready_file(client, storage, "rapor.txt", b"birinci icerik", "text/plain")
    second = _ready_file(client, storage, "rapor.md", b"ikinci icerik", "text/markdown")
    client.post(f"/files/{first}/summary")
    client.post(f"/files/{second}/summary")
    assert sorted(f["name"] for f in _ai_root_files(client)) == ["rapor - özet (1).pdf", "rapor - özet.pdf"]


def test_result_files_cannot_be_summarized_again(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{fid}/summary")
    result_id = _ai_root_files(client)[0]["id"]
    assert client.post(f"/files/{result_id}/summary").status_code == 415
    assert client.put(f"/files/{result_id}/summary", json={"summary": "x"}).status_code == 415


def test_result_files_move_only_between_ai_results_folders(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{fid}/summary")
    result_id = _ai_root_files(client)[0]["id"]
    ai_folder = client.post("/folders", json={"name": "Derslerim", "location": "ai_results"}).json()
    home_folder = client.post("/folders", json={"name": "Ev"}).json()

    assert client.patch(f"/files/{result_id}", json={"folder_id": home_folder["id"]}).status_code == 400
    moved = client.patch(f"/files/{result_id}", json={"folder_id": ai_folder["id"]})
    assert moved.status_code == 200 and moved.json()["folder_id"] == ai_folder["id"]
    assert _ai_root_files(client) == []
    assert [f["id"] for f in client.get("/files", params={"folder_id": ai_folder["id"]}).json()] == [result_id]
    # Geri AI Sonuçlarım'ın en üstüne (ana sayfaya değil).
    back = client.patch(f"/files/{result_id}", json={"folder_id": None})
    assert back.status_code == 200
    assert [f["id"] for f in _ai_root_files(client)] == [result_id]
    assert [f["id"] for f in _home_root_files(client)] == [fid]
    # Normal dosya AI klasörüne taşınamaz.
    assert client.patch(f"/files/{fid}", json={"folder_id": ai_folder["id"]}).status_code == 400


def test_summary_endpoint_returns_the_result_file_id(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    posted = client.post(f"/files/{fid}/summary").json()
    result_id = _ai_root_files(client)[0]["id"]
    assert posted["result_file_id"] == result_id
    assert client.get(f"/files/{fid}/summary").json()["result_file_id"] == result_id


def test_result_file_in_trash_keeps_the_summary_until_permanently_deleted(
    client, storage, login_as, llm, session_factory
):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{fid}/summary")
    result_id = _ai_root_files(client)[0]["id"]

    # Çöpe atmak özeti silmez: düğme dolu kalır, özet açılır, yeniden özetleme kapalıdır.
    assert client.delete(f"/files/{result_id}").status_code in (200, 204)
    assert _file(client, fid)["has_summary"] is True
    assert client.get(f"/files/{fid}/summary").json()["summary"] == "ÖZET: kısa"
    assert len(_ai_rows(session_factory)) == 1

    # Çöp kutusundan da silince sonuç kaydı gider ve dosya yeniden özetlenebilir.
    assert client.request("POST", "/trash/delete", json={"file_ids": [result_id], "folder_ids": []}).status_code == 200
    assert _file(client, fid)["has_summary"] is False
    assert client.get(f"/files/{fid}/summary").status_code == 404
    assert _ai_rows(session_factory) == []
    again = client.post(f"/files/{fid}/summary")
    assert again.status_code == 200
    assert _file(client, fid)["has_summary"] is True
    new_files = _ai_root_files(client)
    assert len(new_files) == 1 and new_files[0]["id"] != result_id
    assert again.json()["result_file_id"] == new_files[0]["id"]


def test_restoring_the_result_file_from_trash_keeps_everything(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{fid}/summary")
    result_id = _ai_root_files(client)[0]["id"]
    client.delete(f"/files/{result_id}")
    assert client.post("/trash/restore", json={"file_ids": [result_id], "folder_ids": []}).status_code == 200
    assert [f["id"] for f in _ai_root_files(client)] == [result_id]
    assert _file(client, fid)["has_summary"] is True


def test_summary_survives_a_storage_failure_when_writing_the_file(client, storage, login_as, llm, monkeypatch, session_factory):
    from app.storage import StorageError

    def boom(*a, **k):
        raise StorageError("down")

    monkeypatch.setattr(storage, "put_object", boom)
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    assert client.post(f"/files/{fid}/summary").status_code == 200
    assert _file(client, fid)["has_summary"] is True
    assert _ai_root_files(client) == []
    (row,) = _ai_rows(session_factory)
    assert row.status == "done" and row.result_file_id is None


def test_deleting_the_source_keeps_the_result_file_and_blocks_a_second_summary(
    client, storage, login_as, llm, session_factory
):
    login_as(uid="u1")
    a = _ready_file(client, storage, "rapor.txt", b"merhaba dunya", "text/plain")
    client.post(f"/files/{a}/summary")
    result_id = _ai_root_files(client)[0]["id"]
    client.delete(f"/files/{a}")
    client.request("POST", "/trash/delete", json={"file_ids": [a], "folder_ids": []})

    (row,) = _ai_rows(session_factory)
    assert row.file_id is None and str(row.result_file_id) == result_id and row.status == "done"
    assert [f["id"] for f in _ai_root_files(client)] == [result_id]

    b = _ready_file(client, storage, "rapor-kopya.txt", b"merhaba dunya", "text/plain")
    assert _file(client, b)["has_summary"] is True
    assert client.get(f"/files/{b}/summary").json()["result_file_id"] == result_id
    calls_before = len(llm)
    assert client.post(f"/files/{b}/summary").status_code == 200
    assert len(llm) == calls_before            # yeniden özetlenmedi
    assert len(_ai_root_files(client)) == 1    # ikinci .txt oluşmadı

    # Özet dosyası da kalıcı silinirse kayıt gider ve dosya yeniden özetlenebilir.
    client.delete(f"/files/{result_id}")
    client.request("POST", "/trash/delete", json={"file_ids": [result_id], "folder_ids": []})
    assert _file(client, b)["has_summary"] is False


def test_source_summary_of_another_user_does_not_leak(client, storage, login_as, llm):
    login_as(uid="u1")
    a = _ready_file(client, storage, "a.txt", b"gizli icerik", "text/plain")
    client.post(f"/files/{a}/summary")
    client.delete(f"/files/{a}")
    client.request("POST", "/trash/delete", json={"file_ids": [a], "folder_ids": []})
    login_as(uid="u2", email="b@b.com")
    b = _ready_file(client, storage, "a.txt", b"gizli icerik", "text/plain")
    assert _file(client, b)["has_summary"] is False
    assert client.get(f"/files/{b}/summary").status_code == 404


def test_failed_attempt_rows_are_removed_when_the_source_is_deleted(client, storage, login_as, llm, session_factory, monkeypatch):
    login_as(uid="u1")
    a = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")

    def boom(system, user, max_tokens=None):
        raise summarize.SummaryError(502, "Yapay zeka yanıt vermedi.")

    monkeypatch.setattr(summarize, "_chat", boom)
    client.post(f"/files/{a}/summary")
    assert len(_ai_rows(session_factory)) == 1
    client.delete(f"/files/{a}")
    client.request("POST", "/trash/delete", json={"file_ids": [a], "folder_ids": []})
    assert _ai_rows(session_factory) == []


# --- Parolalı dosyalar -------------------------------------------------------------------------------------

DATA = __import__("pathlib").Path(__file__).parent / "data"
WORD_PASSWORD = "parola123"  # tests/data/encrypted-*.docx bu parolayla şifrelenmiştir (içinde "Gizli belge" yazar)


def _encrypted_pdf(text: str, user_password: str, owner_password: str = "sahip") -> bytes:
    from pypdf import PdfReader

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_make_pdf_with_text(text + " " + "word " * 30))))
    writer.encrypt(user_password=user_password, owner_password=owner_password, algorithm="AES-256")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _error_code(response):
    return response.json()["detail"]["code"]


def test_encrypted_pdf_asks_for_a_password_without_leaving_a_failed_row(client, storage, login_as, llm, session_factory):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "gizli.pdf", _encrypted_pdf("Gizli rapor icerigi", "123456"))
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 423 and _error_code(r) == "password_required"
    assert llm == []                        # LLM'e hiçbir şey gitmedi
    assert _ai_rows(session_factory) == []  # "başarısız" satırı da açılmadı


def test_wrong_pdf_password_is_423_invalid(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "gizli.pdf", _encrypted_pdf("Gizli rapor icerigi", "123456"))
    r = client.post(f"/files/{fid}/summary", json={"password": "yanlis"})
    assert r.status_code == 423 and _error_code(r) == "password_invalid"
    assert llm == []


def test_correct_pdf_password_summarizes_and_is_not_stored(client, storage, login_as, llm, session_factory):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "gizli.pdf", _encrypted_pdf("Gizli rapor icerigi", "123456"))
    r = client.post(f"/files/{fid}/summary", json={"password": "123456"})
    assert r.status_code == 200, r.text
    assert "Gizli rapor icerigi" in llm[-1][1]
    (row,) = _ai_rows(session_factory)
    assert row.status == "done" and "123456" not in str(row.result) and "123456" not in r.text


def test_pdf_that_only_has_an_owner_password_needs_no_password(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "kisitli.pdf", _encrypted_pdf("Kopyalama kisitli belge", user_password=""))
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 200, r.text
    assert "Kopyalama kisitli belge" in llm[-1][1]


@pytest.mark.parametrize("fixture", ["encrypted-standard.docx", "encrypted-agile.docx"])
def test_encrypted_docx_password_flow(client, storage, login_as, llm, session_factory, fixture):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "gizli.docx", (DATA / fixture).read_bytes())
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 423 and _error_code(r) == "password_required"
    r = client.post(f"/files/{fid}/summary", json={"password": "yanlis"})
    assert r.status_code == 423 and _error_code(r) == "password_invalid"
    assert llm == [] and _ai_rows(session_factory) == []
    r = client.post(f"/files/{fid}/summary", json={"password": WORD_PASSWORD})
    assert r.status_code == 200, r.text
    assert "Gizli belge" in llm[-1][1] and "ğüşıöç" in llm[-1][1]


def test_stored_summary_is_returned_without_asking_for_the_password_again(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "gizli.pdf", _encrypted_pdf("Gizli rapor icerigi", "123456"))
    assert client.post(f"/files/{fid}/summary", json={"password": "123456"}).status_code == 200
    calls = len(llm)
    again = client.post(f"/files/{fid}/summary")
    assert again.status_code == 200 and len(llm) == calls


def test_password_is_ignored_for_files_that_are_not_encrypted(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "acik.txt", b"Herkese acik metin", "text/plain")
    assert client.post(f"/files/{fid}/summary", json={"password": "gereksiz"}).status_code == 200
    docx = _ready_file(client, storage, "acik.docx", _docx("Acik docx"))
    assert client.post(f"/files/{docx}/summary", json={"password": "gereksiz"}).status_code == 200
    assert "Acik docx" in llm[-1][1]


def test_broken_ole_docx_is_422_not_500(client, storage, login_as, llm):
    login_as(uid="u1")
    data = bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 2000
    fid = _ready_file(client, storage, "bozuk.docx", data)
    assert client.post(f"/files/{fid}/summary").status_code == 422
    assert client.post(f"/files/{fid}/summary", json={"password": "x"}).status_code == 422


def test_password_too_long_is_rejected(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")
    assert client.post(f"/files/{fid}/summary", json={"password": "x" * 300}).status_code == 422


# --- Özet PDF'i ve şablonlar -------------------------------------------------------------------------------

_TURKISH_SUMMARY = (
    "Özet: Çalışma ğüşıöç ĞÜŞİÖÇ harflerini içerir.\n\nÖnemli noktalar:\n- Birinci madde\n- İkinci madde\n\n"
    "## Sonuç\nBu bir **kalın** paragraftır."
)


def _font_runs(pdf: bytes) -> list[tuple[str, float, str]]:
    """PDF'teki (metin, punto, yazı tipi) üçlüleri."""
    from pypdf import PdfReader

    runs: list[tuple[str, float, str]] = []

    def visit(text, cm, tm, font_dict, font_size):
        if text.strip():
            runs.append((text.strip(), round(font_size * abs(tm[0] or 1) if tm[0] not in (0, 1) else font_size, 1), (font_dict or {}).get("/BaseFont", "")))

    for page in PdfReader(io.BytesIO(pdf)).pages:
        page.extract_text(visitor_text=visit)
    return runs


def test_default_template_is_times_like_with_14pt_bold_headings_and_12pt_body():
    from app import summary_pdf

    runs = _font_runs(summary_pdf.render("rapor.docx", _TURKISH_SUMMARY))
    by_text = {t: (size, font) for t, size, font in runs}
    size, font = by_text["Özet"]
    assert size == 14 and "Bold" in font and "LiberationSerif" in font
    size, font = by_text["Önemli noktalar"]
    assert size == 14 and "Bold" in font
    size, font = by_text["Çalışma ğüşıöç ĞÜŞİÖÇ harflerini içerir."]
    assert size == 12 and "LiberationSerif" in font and "Bold" not in font


@pytest.mark.parametrize("template", ["klasik", "akademik", "modern", "kompakt"])
def test_every_template_renders_turkish_text(template):
    from app import summary_pdf

    text = _pdf_text(summary_pdf.render("ğüşıİ.docx", _TURKISH_SUMMARY, template))
    for part in ("ğüşıöç ĞÜŞİÖÇ", "Önemli noktalar", "Birinci madde", "Sonuç", "kalın"):
        assert part in text, (template, part)


def test_unknown_or_missing_template_falls_back_to_default():
    from app import summary_pdf

    assert summary_pdf.template_for(None).id == summary_pdf.DEFAULT_TEMPLATE
    assert summary_pdf.template_for("yok-boyle-sablon").id == summary_pdf.DEFAULT_TEMPLATE
    assert summary_pdf.render("a.txt", "Merhaba dünya.", "yok").startswith(b"%PDF")


def test_markdown_and_phone_formats_are_split_into_blocks():
    from app import summary_pdf

    blocks = summary_pdf.parse_blocks("# Başlık\nBir satır\nikinci satır\n\n- madde\n3. numaralı\n**Kalın başlık**\nÖzet: kısa metin")
    assert blocks == [
        ("heading", "Başlık", ""),
        ("paragraph", "Bir satır ikinci satır", ""),
        ("bullet", "madde", "•"),
        ("bullet", "numaralı", "3."),
        ("heading", "Kalın başlık", ""),
        ("heading", "Özet", ""),
        ("paragraph", "kısa metin", ""),
    ]


def test_special_characters_in_the_summary_do_not_break_the_pdf():
    from app import summary_pdf

    nasty = "<b>açık etiket & ters </i> < > &amp; **eşleşmeyen\n- <script>x</script>"
    assert summary_pdf.render("<x>&.txt", nasty).startswith(b"%PDF")


def test_cloud_summary_uses_the_template_from_the_request(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    assert client.post(f"/files/{fid}/summary", json={"template": "modern"}).status_code == 200
    (result,) = _ai_root_files(client)
    data = storage.contents[f"{client.get('/me').json()['id']}/{result['id']}"]
    assert any("LiberationSans" in font for _, _, font in _font_runs(data))


def test_local_summary_uses_the_template_from_the_request(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    r = client.put(f"/files/{fid}/summary", json={"summary": "Özet: telefonda üretildi", "model": "m", "template": "akademik"})
    assert r.status_code == 200
    (result,) = _ai_root_files(client)
    data = storage.contents[f"{client.get('/me').json()['id']}/{result['id']}"]
    assert "telefonda üretildi" in _pdf_text(data) and result["name"] == "notlar - özet.pdf"


def test_summary_file_falls_back_to_txt_if_pdf_rendering_fails(client, storage, login_as, llm, monkeypatch):
    from app import summary_pdf

    def boom(*args, **kwargs):
        raise RuntimeError("yazı tipi yok")

    monkeypatch.setattr(summary_pdf, "render", boom)
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    assert client.post(f"/files/{fid}/summary").status_code == 200
    (result,) = _ai_root_files(client)
    assert result["name"] == "notlar - özet.txt" and result["mime_type"] == "text/plain"


# --- Telefonun ürettiği PDF ------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def test_local_summary_stores_the_pdf_made_on_the_phone_as_is(client, storage, login_as, llm):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    phone_pdf = _make_pdf_with_text("Telefonda uretilen PDF")
    r = client.put(
        f"/files/{fid}/summary", json={"summary": "Özet: telefonda", "model": "m", "pdf_base64": _b64(phone_pdf)}
    )
    assert r.status_code == 200
    (result,) = _ai_root_files(client)
    assert result["name"] == "notlar - özet.pdf" and result["mime_type"] == "application/pdf"
    assert result["size"] == len(phone_pdf)
    assert storage.contents[f"{client.get('/me').json()['id']}/{result['id']}"] == phone_pdf
    assert client.get(f"/files/{fid}/summary").json()["result_file_id"] == result["id"]


@pytest.mark.parametrize(
    "bad",
    ["###", _b64(b"bu pdf degil"), _b64(b"%PDF-1.4 bozuk"), _b64(b"")],
)
def test_invalid_phone_pdf_falls_back_to_the_server_rendered_pdf(client, storage, login_as, llm, bad):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    r = client.put(f"/files/{fid}/summary", json={"summary": "Özet: sunucu üretir", "pdf_base64": bad})
    assert r.status_code == 200
    (result,) = _ai_root_files(client)
    data = storage.contents[f"{client.get('/me').json()['id']}/{result['id']}"]
    assert result["mime_type"] == "application/pdf" and "sunucu üretir" in _pdf_text(data)


def test_oversized_phone_pdf_is_ignored(client, storage, login_as, llm, monkeypatch):
    from app import ai_results

    monkeypatch.setattr(ai_results, "MAX_CLIENT_PDF_BYTES", 10)
    login_as(uid="u1")
    fid = _ready_file(client, storage, "notlar.txt", b"merhaba dunya", "text/plain")
    client.put(f"/files/{fid}/summary", json={"summary": "Özet: kısa", "pdf_base64": _b64(_make_pdf_with_text("x"))})
    (result,) = _ai_root_files(client)
    data = storage.contents[f"{client.get('/me').json()['id']}/{result['id']}"]
    assert "kısa" in _pdf_text(data)


def test_chat_disables_thinking_and_drops_think_blocks(monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "llm_model", "qwen")
    sent = {}

    def sse(text):
        return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield sse("<think>\nönce düşün\n</think>\n\nÖzet")
            yield ""
            yield sse(": kısa.")
            yield "data: [DONE]"

    def fake_stream(method, url, json, headers, timeout):
        sent.update(method=method, url=url, payload=json)
        return FakeStream()

    monkeypatch.setattr(summarize.httpx, "stream", fake_stream)
    assert summarize._chat("sistem", "belge") == "Özet: kısa."
    assert sent["url"] == "http://llm.test/v1/chat/completions"
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["payload"]["stream"] is True


# --- İptal: istemci gidince özetleme durur, süren model çağrısı kesilir ----------------------------------------------


@pytest.fixture
def streaming_llm(monkeypatch):
    """Gerçek bir HTTP sunucusu: her 20 ms'de bir "kelime" akıtır (en çok 500). Kaç kelime yazıldığını ve bağlantının
    ne zaman koptuğunu kaydeder; model sunucusunun (llama-server) davranışının küçük bir taklidi."""
    state = {"sent": 0, "broken": threading.Event(), "requests": 0, "total": 500}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for _ in range(state["total"]):
                    chunk = json.dumps({"choices": [{"delta": {"content": "kelime "}}]})
                    self.wfile.write(f"data: {chunk}\n\n".encode())
                    self.wfile.flush()
                    state["sent"] += 1
                    time.sleep(0.02)
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                state["broken"].set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "llm_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "llm_model", "m")
    yield state
    server.shutdown()


def test_chat_reads_the_streamed_answer(streaming_llm):
    streaming_llm["total"] = 5
    assert summarize._chat("sistem", "belge") == "kelime kelime kelime kelime kelime"


def test_cancelling_stops_the_model_call_mid_generation(streaming_llm):
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with summarize.cancellable(cancel), pytest.raises(summarize.Cancelled):
        summarize._chat("sistem", "belge")
    assert time.monotonic() - started < 3          # 500 kelime 10 sn sürerdi; iptalde hemen çıkar
    assert streaming_llm["broken"].wait(2)          # bağlantı kapandı: sunucu üretimi bırakmak zorunda
    assert streaming_llm["sent"] < 100


def test_cancelled_before_start_never_calls_the_model(streaming_llm):
    cancel = threading.Event()
    cancel.set()
    with summarize.cancellable(cancel), pytest.raises(summarize.Cancelled):
        summarize._chat("sistem", "belge")
    assert streaming_llm["requests"] == 0


def test_cancel_reaches_parallel_chunk_calls_and_stops_later_ones(streaming_llm, monkeypatch):
    monkeypatch.setattr(settings, "llm_concurrency", 2)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    big = ("Bu cümle belgenin bir parçasıdır. " * 40 + "\n") * 40  # birkaç parça
    with summarize.cancellable(cancel), pytest.raises(summarize.Cancelled):
        summarize.summarize_text(big)
    assert streaming_llm["requests"] <= 2          # ilk dalgadan sonra yeni çağrı başlamadı


def test_disconnect_marks_the_attempt_failed(client, storage, login_as, llm, session_factory, monkeypatch):
    login_as(uid="u1")
    fid = _ready_file(client, storage, "a.txt", b"merhaba dunya", "text/plain")

    def cancelled(extracted):
        raise summarize.Cancelled

    monkeypatch.setattr(summarize, "summarize", cancelled)
    r = client.post(f"/files/{fid}/summary")
    assert r.status_code == 499
    (row,) = _ai_rows(session_factory)
    assert row.status == "failed" and "iptal" in row.result["error"]
    assert _file(client, fid)["has_summary"] is False


# --- Ders notu: konu başlıkları ve kod blokları aynen korunur ---------------------------------------------------------

_KOD = 'int i = 1;\nwhile (i <= 100) {\n    System.out.println(i);\n    i++;\n}'


def test_code_blocks_keep_indentation_and_become_one_block():
    from app import summary_pdf

    blocks = summary_pdf.parse_blocks(f"## while\n- Koşul doğruyken çalışır.\n```java\n{_KOD}\n```\nSon cümle.")
    assert blocks == [
        ("heading", "while", ""),
        ("bullet", "Koşul doğruyken çalışır.", "•"),
        ("code", _KOD, ""),
        ("paragraph", "Son cümle.", ""),
    ]
    assert summary_pdf.parse_blocks("```\nbitmeyen kod")[0] == ("code", "bitmeyen kod", "")  # kesilmiş model çıktısı


def test_pdf_renders_code_blocks_with_monospace_text():
    from app import summary_pdf

    pdf = summary_pdf.render("ders.pdf", f"## while\n```\n{_KOD}\n```")
    text = _pdf_text(pdf)
    assert "while (i <= 100) {" in text and "i++;" in text and "```" not in text


def test_lecture_notes_are_joined_as_sections_not_rewritten(monkeypatch, llm):
    """Bölüm özetleri konu başlığı ve kod içeriyorsa model onları yeniden yazmaz; alt alta konur, kod bozulmaz."""
    calls = []

    def fake_chat(system, user, max_tokens=None):
        calls.append(user[:40])
        if "BÖLÜM:" in user:
            n = user.split(". bölümüdür")[0].split()[-1].split("/")[0]
            return f"## Konu {n}\n- Kural {n}.\n```java\n{_KOD}\n```"
        if user.startswith(summarize._DOC_ASK[:30]):
            return "Java döngüleri üzerine ders sunumu."
        return "Önemli noktalar:\n- while'da sayaç artırılmazsa sonsuz döngü olur."

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    text = ("Döngü konusu bir ders cümlesidir. " * 40 + "\n") * 40
    out = summarize.summarize_text(text)
    parts = summarize._chunks(text)
    assert len(parts) > 1
    assert out.startswith("Özet: Java döngüleri üzerine ders sunumu.")
    assert out.count(_KOD) == len(parts)                      # her bölümün kodu aynen duruyor
    assert out.index("## Konu 1") < out.index("## Konu 2")   # sıra korunuyor
    assert out.rstrip().endswith("sonsuz döngü olur.") and "Önemli noktalar:" in out
    assert len(calls) == len(parts) + 2                      # bölümler + (ne olduğu, önemli noktalar); birleştirme yok


def test_repeated_heading_at_a_chunk_boundary_is_written_once():
    out = summarize._join_sections(["## for\n- a", "## For\n- b", "## break\n- c"])
    assert out == "## for\n- a\n\n- b\n\n## break\n- c"


_DERS = (
    "Döngüler\nwhile döngüsü koşul doğru olduğu sürece çalışır.\n"
    "int i = 1;\nwhile (i <= 3) {\nSystem.out.println(i);\ni.+;\n}\n1\n2\n3\n"
    + "Bu satır döngü konusunun açıklamasıdır. " * 400
)


def test_lecture_code_is_copied_by_the_server_not_written_by_the_model(monkeypatch, llm):
    """Model yalnızca [KOD-n] işaretini yazar; özetteki kod metindeki koddur (bozuk "i.+" düzeltilmiş, çıktısıyla)."""
    prompts = []

    def fake_chat(system, user, max_tokens=None):
        prompts.append(user)
        if "BÖLÜM:" in user or user.rstrip().endswith("BELGE:") or "BELGE:" in user[:3000]:
            return "## Döngüler\n- while koşul doğruyken çalışır.\n- Bu örnek 1, 2, 3 yazar.\n[KOD-1]"
        if user.startswith(summarize._DOC_ASK[:30]):
            return "Java döngü dersi."
        return "Önemli noktalar:\n- Sayaç artırılmazsa sonsuz döngü olur."

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    out = summarize.summarize_text(_DERS)
    assert "```\nint i = 1;\nwhile (i <= 3) {\n    System.out.println(i);\n    i++;\n}\n// Çıktı: 1, 2, 3\n```" in out
    assert "[KOD-" not in out and out.count("```") == 2
    assert any("[KOD-1]\nint i = 1;" in p for p in prompts)       # model kodu görür
    assert not any("[KOD-1]" in p and "```" in p for p in prompts)  # ama kod bloğu yazması istenmez


def test_code_the_model_forgot_is_still_in_the_summary(monkeypatch, llm):
    monkeypatch.setattr(summarize, "_chat", lambda system, user, max_tokens=None: "## Konu\n- Yalnızca açıklama.")
    out = summarize.summarize_text("Giriş.\nint a = 1;\nSystem.out.println(a);\n")
    assert "System.out.println(a);" in out and "[KOD" not in out


def test_cut_off_last_key_point_is_dropped():
    assert summarize._drop_cut_item("Önemli noktalar:\n1. Tam madde.\n2. Bir diğeri.\n3. Yarım kal") == (
        "Önemli noktalar:\n1. Tam madde.\n2. Bir diğeri."
    )
    assert summarize._drop_cut_item("Önemli noktalar:\n1. Tam.\n2. Tam da.") == "Önemli noktalar:\n1. Tam.\n2. Tam da."


def test_pdf_inline_code_is_monospace_and_keeps_its_symbols():
    from app import summary_pdf

    assert summary_pdf._inline("yıldız `*` basılır **önemli**") == (
        'yıldız <font name="LibMono">*</font> basılır <b>önemli</b>'
    )
    text = _pdf_text(summary_pdf.render("a.pdf", "- Yıldız (`*`) ve `i++` yazılır."))
    assert "`" not in text and "(*)" in text and "i++" in text


def test_overview_sentence_is_written_from_topic_titles_and_never_cut_mid_sentence(monkeypatch, llm):
    seen = {}

    def fake_chat(system, user, max_tokens=None):
        if "BÖLÜM:" in user:
            n = user.split(". bölümüdür")[0].split()[-1].split("/")[0]
            return f"## Konu {n}\n- Bu örnek şunu gösterir ve çok uzun bir açıklama yazar.\n[KOD-9]"
        if user.startswith(summarize._DOC_ASK[:30]):
            seen["ask"] = user
            return "Java döngü dersidir. Kapsadığı konular arasında dizi ve"
        return "Önemli noktalar:\n- Madde."

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    out = summarize.summarize_text("Ders cümlesi burada geçer. " * 800)
    assert out.startswith("Özet: Java döngü dersidir.\n")        # yarım kalan ikinci cümle atıldı
    assert "- Konu 1" in seen["ask"] and "açıklama" not in seen["ask"]   # modele yalnızca başlıklar verilir


def test_trim_to_sentence_keeps_a_single_unfinished_sentence():
    assert summarize._trim_to_sentence("Tek cümle ama yarım") == "Tek cümle ama yarım"
    assert summarize._trim_to_sentence("Bir. İki. Üçü yarım") == "Bir. İki."
