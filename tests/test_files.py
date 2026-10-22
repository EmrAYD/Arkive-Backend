import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import File, SubscriptionPlan, User

MB = 1024**2


def _init(client, name="rapor.pdf", size=1000, **extra):
    return client.post("/files/init", json={"name": name, "size": size, **extra})


def _upload_and_complete(client, storage, name="rapor.pdf", size=1000, **extra):
    """Başarılı yükleme akışının tamamı: init -> (istemci RustFS'e yükler) -> complete."""
    r = _init(client, name, size, **extra)
    assert r.status_code == 201, r.text
    meta = r.json()
    storage.objects[f"{_key_of(meta)}"] = size  # istemcinin yüklemesi
    done = client.post(f"/files/{meta['file']['id']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


def _key_of(meta):
    return meta["upload_url"].removeprefix("https://fake-storage.test/put/")


def _used(session_factory, uid_claim_email=None):
    with session_factory() as db:
        return db.scalar(select(User.used_storage))


# --- init ----------------------------------------------------------------------


def test_requires_login(client, storage):
    assert _init(client).status_code == 401
    assert client.get("/files").status_code == 401


def test_init_creates_uploading_record_and_presigned_url(client, storage, login_as, session_factory):
    login_as(uid="u1")
    r = _init(client, "Rapor.pdf", 1234, mime_type="Application/PDF")
    assert r.status_code == 201
    body = r.json()
    assert body["file"]["status"] == "uploading"
    assert body["file"]["name"] == "Rapor.pdf"
    assert body["upload_headers"] == {"Content-Type": "application/pdf"}  # küçük harfe çevrilir
    assert body["expires_in"] == 900
    # İmza, tür ve boyuta göre atılmış olmalı
    assert storage.last_put["content_type"] == "application/pdf"
    assert storage.last_put["size"] == 1234
    # Nesne adı kullanıcı dosya adından bağımsız: "<user_id>/<file_id>"
    with session_factory() as db:
        f = db.scalar(select(File))
        assert f.storage_key == f"{f.user_id}/{f.id}"
        assert "Rapor" not in f.storage_key


def test_unknown_mime_defaults_to_octet_stream(client, storage, login_as):
    login_as(uid="u1")
    assert _init(client).json()["upload_headers"] == {"Content-Type": "application/octet-stream"}
    assert _init(client, mime_type="  ").json()["upload_headers"] == {
        "Content-Type": "application/octet-stream"
    }


def test_init_validation(client, storage, login_as):
    login_as(uid="u1")
    assert _init(client, size=0).status_code == 422
    assert _init(client, size=-5).status_code == 422
    assert _init(client, name="  ").status_code == 422
    assert _init(client, name="a/b.pdf").status_code == 422
    assert _init(client, name="x" * 256).status_code == 422
    assert _init(client, mime_type="not a mime").status_code == 422


def test_free_plan_max_file_size_10mb(client, storage, login_as):
    login_as(uid="u1")
    assert _init(client, size=10 * MB).status_code == 201
    r = _init(client, size=10 * MB + 1)
    assert r.status_code == 413
    assert "10 MB" in r.json()["detail"]


def test_quota_counts_used_and_pending_uploads(client, storage, login_as, session_factory):
    login_as(uid="u1")
    client.get("/me")
    with session_factory() as db:  # sınırı küçült: 25 MB
        db.add(SubscriptionPlan(name="Free", price=0, storage_limit=25 * MB))
        db.commit()
    assert _init(client, "a", 10 * MB).status_code == 201  # bekleyen: 10
    assert _init(client, "b", 10 * MB).status_code == 201  # bekleyen: 20
    r = _init(client, "c", 10 * MB)  # 30 > 25, ikisi daha tamamlanmadığı halde sayılmalı
    assert r.status_code == 413 and "yetersiz" in r.json()["detail"]
    assert _init(client, "d", 5 * MB).status_code == 201  # 25 == sınır, olur


def test_expired_pending_uploads_do_not_count(client, storage, login_as, session_factory):
    login_as(uid="u1")
    client.get("/me")
    with session_factory() as db:
        db.add(SubscriptionPlan(name="Free", price=0, storage_limit=15 * MB))
        db.commit()
    first = _init(client, "eski", 10 * MB).json()["file"]["id"]
    with session_factory() as db:  # bu yüklemeyi "çok eski" yap
        f = db.get(File, uuid.UUID(first))
        f.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    assert _init(client, "yeni", 10 * MB).status_code == 201


def test_init_into_folder_rules(client, storage, login_as):
    login_as(uid="u1")
    home = client.post("/folders", json={"name": "Belgeler"}).json()
    ai = client.post("/folders", json={"name": "Özetler", "location": "ai_results"}).json()
    assert _init(client, folder_id=home["id"]).status_code == 201
    assert _init(client, folder_id=ai["id"]).status_code == 400
    assert _init(client, folder_id=str(uuid.uuid4())).status_code == 404

    login_as(uid="u2")
    assert _init(client, folder_id=home["id"]).status_code == 404  # başkasının klasörü


# --- complete ------------------------------------------------------------------


def test_complete_marks_ready_and_counts_usage(client, storage, login_as, session_factory):
    login_as(uid="u1")
    done = _upload_and_complete(client, storage, size=2500)
    assert done["status"] == "ready"
    assert client.get("/me").json()["used_storage"] == 2500


def test_complete_is_idempotent(client, storage, login_as):
    login_as(uid="u1")
    meta = _init(client, size=700).json()
    storage.objects[_key_of(meta)] = 700
    fid = meta["file"]["id"]
    assert client.post(f"/files/{fid}/complete").status_code == 200
    assert client.post(f"/files/{fid}/complete").status_code == 200
    assert client.get("/me").json()["used_storage"] == 700  # iki kez sayılmadı


def test_complete_without_upload_is_409_and_can_retry(client, storage, login_as):
    login_as(uid="u1")
    meta = _init(client, size=500).json()
    fid = meta["file"]["id"]
    assert client.post(f"/files/{fid}/complete").status_code == 409  # nesne henüz yok
    storage.objects[_key_of(meta)] = 500  # sonra yükleme tamamlandı
    assert client.post(f"/files/{fid}/complete").status_code == 200


def test_complete_with_wrong_size_fails_and_removes_object(client, storage, login_as, session_factory):
    login_as(uid="u1")
    meta = _init(client, size=500).json()
    key = _key_of(meta)
    storage.objects[key] = 999  # bildirilenden farklı
    r = client.post(f"/files/{meta['file']['id']}/complete")
    assert r.status_code == 409
    assert key in storage.deleted and key not in storage.objects
    with session_factory() as db:
        assert db.scalar(select(File.status)) == "failed"
    assert client.get("/me").json()["used_storage"] == 0
    # failed bir yükleme yeniden tamamlanamaz
    assert client.post(f"/files/{meta['file']['id']}/complete").status_code == 409


def test_complete_other_users_file_is_404(client, storage, login_as):
    login_as(uid="u1")
    meta = _init(client).json()
    storage.objects[_key_of(meta)] = 1000
    login_as(uid="u2")
    assert client.post(f"/files/{meta['file']['id']}/complete").status_code == 404


# --- list / download -------------------------------------------------------------


def test_list_shows_only_ready_files_per_level(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "F"}).json()
    _upload_and_complete(client, storage, "b-kok.txt", 10)
    _upload_and_complete(client, storage, "A-kok.txt", 10)
    _upload_and_complete(client, storage, "icerde.txt", 10, folder_id=folder["id"])
    _init(client, "bitmemis.txt", 10)  # uploading: listede görünmemeli

    root = client.get("/files").json()
    assert [f["name"] for f in root] == ["A-kok.txt", "b-kok.txt"]  # harf farkı yok sayılarak sıralı
    inside = client.get("/files", params={"folder_id": folder["id"]}).json()
    assert [f["name"] for f in inside] == ["icerde.txt"]


def test_list_is_isolated_between_users(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "F"}).json()
    _upload_and_complete(client, storage, "gizli.txt", 10, folder_id=folder["id"])
    login_as(uid="u2")
    assert client.get("/files").json() == []
    assert client.get("/files", params={"folder_id": folder["id"]}).status_code == 404


def test_download_url(client, storage, login_as):
    login_as(uid="u1")
    done = _upload_and_complete(client, storage, "rapor.pdf", 10)
    r = client.get(f"/files/{done['id']}/download")
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://fake-storage.test/get/") and "rapor.pdf" in r.json()["url"]
    assert r.json()["expires_in"] == 3600

    pending = _init(client, "x.txt", 10).json()["file"]["id"]
    assert client.get(f"/files/{pending}/download").status_code == 409

    login_as(uid="u2")
    assert client.get(f"/files/{done['id']}/download").status_code == 404


# --- klasör silme dosyaları da temizler ----------------------------------------------


def test_deleting_folder_moves_to_trash_then_trash_frees_usage(client, storage, login_as):
    login_as(uid="u1")
    top = client.post("/folders", json={"name": "Üst"}).json()
    sub = client.post("/folders", json={"name": "Alt", "parent_id": top["id"]}).json()
    keep = _upload_and_complete(client, storage, "kalsin.txt", 100)  # klasör dışında
    _upload_and_complete(client, storage, "a.txt", 1000, folder_id=top["id"])
    _upload_and_complete(client, storage, "b.txt", 2000, folder_id=sub["id"])
    assert client.get("/me").json()["used_storage"] == 3100

    assert client.delete(f"/folders/{top['id']}").status_code == 204

    # Çöpe atıldı: listelerden kayboldu ama hiçbir şey silinmedi, kota da aynı
    assert [f["id"] for f in client.get("/files").json()] == [keep["id"]]
    assert client.get("/me").json()["used_storage"] == 3100
    assert storage.deleted == [] and len(storage.objects) == 3

    # Çöpten kalıcı silince nesneler gider, kullanım düşer
    assert client.delete("/trash").status_code == 200
    assert client.get("/me").json()["used_storage"] == 100
    assert len(storage.deleted) == 2  # alt klasördeki dosya da dahil
    assert len(storage.objects) == 1  # yalnızca klasör dışındaki kaldı


def test_deleting_empty_folder_works_without_storage_configured(client, login_as):
    """Depolama yapılandırılmamış olsa bile dosyasız klasör silinebilmeli."""
    login_as(uid="u1")
    f = client.post("/folders", json={"name": "Bos"}).json()
    assert client.delete(f"/folders/{f['id']}").status_code == 204


def test_me_reports_max_file_size(client, login_as):
    login_as(uid="u1")
    assert client.get("/me").json()["max_file_size"] == 10 * MB


# --- PATCH (ad / favori) -------------------------------------------------------


def test_patch_rename_and_favorite(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage, "eski.pdf", 500)
    r = client.patch(f"/files/{f['id']}", json={"name": "  yeni.pdf "})
    assert r.status_code == 200 and r.json()["name"] == "yeni.pdf" and r.json()["favorite"] is False
    r = client.patch(f"/files/{f['id']}", json={"favorite": True})
    assert r.json()["favorite"] is True and r.json()["name"] == "yeni.pdf"  # ad değişmedi


def test_patch_validation(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage)
    url = f"/files/{f['id']}"
    assert client.patch(url, json={}).status_code == 422
    assert client.patch(url, json={"name": None}).status_code == 422
    assert client.patch(url, json={"name": "a/b"}).status_code == 422
    assert client.patch(url, json={"favorite": None}).status_code == 422


def test_patch_other_users_file_404_and_uploading_409(client, storage, login_as):
    login_as(uid="u1")
    ready = _upload_and_complete(client, storage)
    pending = _init(client, "bekleyen.pdf").json()["file"]
    assert client.patch(f"/files/{pending['id']}", json={"favorite": True}).status_code == 409
    login_as(uid="u2")
    assert client.patch(f"/files/{ready['id']}", json={"favorite": True}).status_code == 404


# --- DELETE --------------------------------------------------------------------


def test_delete_ready_file_goes_to_trash_and_permanent_delete_frees_usage(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage, size=3000)
    key = next(iter(storage.objects))
    assert client.get("/me").json()["used_storage"] == 3000
    assert client.delete(f"/files/{f['id']}").status_code == 204
    assert client.get("/files").json() == []
    assert client.get(f"/files/{f['id']}").status_code == 200  # çöpte ön izlenebilir
    assert client.delete(f"/files/{f['id']}").status_code == 404
    # Çöpte: nesne ve kota yerinde
    assert client.get("/me").json()["used_storage"] == 3000
    assert key in storage.objects and key not in storage.deleted

    assert client.post("/trash/delete", json={"file_ids": [f["id"]]}).json() == {"count": 1}
    assert client.get("/me").json()["used_storage"] == 0
    assert key in storage.deleted and key not in storage.objects


def test_delete_uploading_file_does_not_touch_usage(client, storage, login_as):
    login_as(uid="u1")
    kept = _upload_and_complete(client, storage, "kalan.pdf", 1000)
    pending = _init(client, "iptal.pdf", 4000).json()["file"]
    assert client.delete(f"/files/{pending['id']}").status_code == 204
    assert client.get("/me").json()["used_storage"] == 1000  # yalnızca tamamlanan sayılmıştı
    assert [x["id"] for x in client.get("/files").json()] == [kept["id"]]


def test_delete_other_users_file_404(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage)
    login_as(uid="u2")
    assert client.delete(f"/files/{f['id']}").status_code == 404
    login_as(uid="u1")
    assert len(client.get("/files").json()) == 1


# --- favori filtreleri ---------------------------------------------------------


def test_favorite_filter_lists_across_folders(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "Belgeler"}).json()
    a = _upload_and_complete(client, storage, "a.pdf", 10)
    b = _upload_and_complete(client, storage, "b.pdf", 10, folder_id=folder["id"])
    _upload_and_complete(client, storage, "c.pdf", 10)
    client.patch(f"/files/{a['id']}", json={"favorite": True})
    client.patch(f"/files/{b['id']}", json={"favorite": True})
    names = [x["name"] for x in client.get("/files", params={"favorite": "true"}).json()]
    assert names == ["a.pdf", "b.pdf"]  # farklı klasörlerden, ada göre sıralı

    login_as(uid="u2")
    assert client.get("/files", params={"favorite": "true"}).json() == []


def test_favorite_folders_filter_includes_nested(client, login_as):
    login_as(uid="u1")
    top = client.post("/folders", json={"name": "Üst"}).json()
    inner = client.post("/folders", json={"name": "İç", "parent_id": top["id"], "favorite": True}).json()
    client.post("/folders", json={"name": "Sıradan"})
    got = client.get("/folders", params={"favorite": "true"}).json()
    assert [f["id"] for f in got] == [inner["id"]]


# --- tek dosya ------------------------------------------------------------------


def test_get_single_file(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage, "rapor.pdf", 321)
    got = client.get(f"/files/{f['id']}")
    assert got.status_code == 200
    assert got.json()["name"] == "rapor.pdf" and got.json()["size"] == 321
    assert client.get(f"/files/{uuid.uuid4()}").status_code == 404
    login_as(uid="u2")
    assert client.get(f"/files/{f['id']}").status_code == 404  # başkasının dosyası


def test_get_uploading_file_409(client, storage, login_as):
    login_as(uid="u1")
    pending = _init(client).json()["file"]
    assert client.get(f"/files/{pending['id']}").status_code == 409


# --- taşıma (PATCH folder_id) -----------------------------------------------------


def test_move_file_into_folder_and_back_to_root(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "Belgeler"}).json()
    f = _upload_and_complete(client, storage, "a.pdf", 10)
    assert f["folder_id"] is None

    r = client.patch(f"/files/{f['id']}", json={"folder_id": folder["id"]})
    assert r.status_code == 200 and r.json()["folder_id"] == folder["id"]
    assert client.get("/files").json() == []  # kökte artık yok
    assert [x["id"] for x in client.get("/files", params={"folder_id": folder["id"]}).json()] == [f["id"]]

    r = client.patch(f"/files/{f['id']}", json={"folder_id": None})  # null = en üst seviye
    assert r.status_code == 200 and r.json()["folder_id"] is None
    assert [x["id"] for x in client.get("/files").json()] == [f["id"]]


def test_patch_without_folder_id_keeps_folder(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "Belgeler"}).json()
    f = _upload_and_complete(client, storage, "a.pdf", 10, folder_id=folder["id"])
    r = client.patch(f"/files/{f['id']}", json={"favorite": True})
    assert r.json()["folder_id"] == folder["id"]  # gönderilmeyen alan değişmez


def test_move_rules(client, storage, login_as):
    login_as(uid="u1")
    mine = client.post("/folders", json={"name": "Benim"}).json()
    ai = client.post("/folders", json={"name": "Özetler", "location": "ai_results"}).json()
    f = _upload_and_complete(client, storage, "a.pdf", 10)
    url = f"/files/{f['id']}"
    assert client.patch(url, json={"folder_id": ai["id"]}).status_code == 400
    assert client.patch(url, json={"folder_id": str(uuid.uuid4())}).status_code == 404

    login_as(uid="u2")
    other = client.post("/folders", json={"name": "Başkası"}).json()
    login_as(uid="u1")
    assert client.patch(url, json={"folder_id": other["id"]}).status_code == 404  # başkasının klasörü
    assert client.get(url).json()["folder_id"] is None  # hata sonrası dosya yerinde

    pending = _init(client, "bekleyen.pdf").json()["file"]
    assert client.patch(f"/files/{pending['id']}", json={"folder_id": mine["id"]}).status_code == 409


def test_move_does_not_change_usage(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "Belgeler"}).json()
    f = _upload_and_complete(client, storage, "a.pdf", 777)
    client.patch(f"/files/{f['id']}", json={"folder_id": folder["id"]})
    assert client.get("/me").json()["used_storage"] == 777


# --- aynı ad çakışması: otomatik "(1)", "(2)" ------------------------------------


def _names(client, **params):
    return [f["name"] for f in client.get("/files", params=params).json()]


def test_same_name_upload_gets_numbered_before_extension(client, storage, login_as):
    login_as(uid="u1")
    assert _upload_and_complete(client, storage, "rapor.pdf")["name"] == "rapor.pdf"
    assert _upload_and_complete(client, storage, "rapor.pdf")["name"] == "rapor (1).pdf"
    assert _upload_and_complete(client, storage, "rapor.pdf")["name"] == "rapor (2).pdf"
    assert _names(client) == ["rapor (1).pdf", "rapor (2).pdf", "rapor.pdf"]


def test_uploading_an_already_numbered_name_continues_the_sequence(client, storage, login_as):
    login_as(uid="u1")
    _upload_and_complete(client, storage, "rapor.pdf")
    _upload_and_complete(client, storage, "rapor (1).pdf")
    # "rapor (1).pdf" zaten var: "rapor (1) (1).pdf" değil "rapor (2).pdf"
    assert _upload_and_complete(client, storage, "rapor (1).pdf")["name"] == "rapor (2).pdf"


def test_name_conflict_is_case_insensitive_and_handles_no_extension(client, storage, login_as):
    login_as(uid="u1")
    _upload_and_complete(client, storage, "Notlar")
    assert _upload_and_complete(client, storage, "notlar")["name"] == "notlar (1)"
    _upload_and_complete(client, storage, ".gitignore")
    assert _upload_and_complete(client, storage, ".gitignore")["name"] == ".gitignore (1)"


def test_same_name_in_different_folders_is_not_a_conflict(client, storage, login_as):
    login_as(uid="u1")
    a = client.post("/folders", json={"name": "A"}).json()
    _upload_and_complete(client, storage, "x.txt")
    assert _upload_and_complete(client, storage, "x.txt", folder_id=a["id"])["name"] == "x.txt"


def test_same_name_is_per_user(client, storage, login_as):
    login_as(uid="u1")
    _upload_and_complete(client, storage, "x.txt")
    login_as(uid="u2")
    assert _upload_and_complete(client, storage, "x.txt")["name"] == "x.txt"


def test_two_concurrent_uploads_of_same_name_get_different_names(client, storage, login_as):
    login_as(uid="u1")
    first = _init(client, "x.txt").json()["file"]["name"]
    second = _init(client, "x.txt").json()["file"]["name"]  # ilki henüz tamamlanmadı (uploading)
    assert (first, second) == ("x.txt", "x (1).txt")


def test_trashed_file_does_not_block_its_name(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage, "x.txt")
    client.delete(f"/files/{f['id']}")
    assert _upload_and_complete(client, storage, "x.txt")["name"] == "x.txt"


def test_rename_to_existing_name_is_numbered(client, storage, login_as):
    login_as(uid="u1")
    _upload_and_complete(client, storage, "a.txt")
    b = _upload_and_complete(client, storage, "b.txt")
    r = client.patch(f"/files/{b['id']}", json={"name": "a.txt"})
    assert r.status_code == 200 and r.json()["name"] == "a (1).txt"
    c = _upload_and_complete(client, storage, "c.txt")
    assert client.patch(f"/files/{c['id']}", json={"name": "a.txt"}).json()["name"] == "a (2).txt"


def test_rename_to_own_name_or_case_change_is_not_a_conflict(client, storage, login_as):
    login_as(uid="u1")
    f = _upload_and_complete(client, storage, "a.txt")
    assert client.patch(f"/files/{f['id']}", json={"name": "a.txt"}).json()["name"] == "a.txt"
    assert client.patch(f"/files/{f['id']}", json={"name": "A.txt"}).json()["name"] == "A.txt"


def test_move_into_folder_with_same_name_is_numbered(client, storage, login_as):
    login_as(uid="u1")
    folder = client.post("/folders", json={"name": "A"}).json()
    _upload_and_complete(client, storage, "x.txt", folder_id=folder["id"])
    top = _upload_and_complete(client, storage, "x.txt")
    r = client.patch(f"/files/{top['id']}", json={"folder_id": folder["id"]})
    assert r.json()["name"] == "x (1).txt" and r.json()["folder_id"] == folder["id"]


def test_long_name_stays_within_limit(client, storage, login_as):
    login_as(uid="u1")
    name = "a" * 251 + ".txt"  # 255 karakter
    _upload_and_complete(client, storage, name)
    second = _upload_and_complete(client, storage, name)["name"]
    assert len(second) <= 255 and second.endswith(" (1).txt")
