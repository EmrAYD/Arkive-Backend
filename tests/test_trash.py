"""Çöp kutusu: yumuşak silme, geri yükleme, kalıcı silme, otomatik temizlik, bekleme süresi ayarı."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.models import File, Folder, User
from app.trash import purge_expired


def _upload(client, storage, name="a.txt", size=1000, **extra):
    r = client.post("/files/init", json={"name": name, "size": size, **extra})
    assert r.status_code == 201, r.text
    meta = r.json()
    storage.objects[meta["upload_url"].removeprefix("https://fake-storage.test/put/")] = size
    done = client.post(f"/files/{meta['file']['id']}/complete")
    assert done.status_code == 200, done.text
    return done.json()


def _folder(client, name, **extra):
    r = client.post("/folders", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _trash(client):
    r = client.get("/trash")
    assert r.status_code == 200, r.text
    return r.json()


def _age_trash(session_factory, days):
    """Çöpteki her şeyin atılma zamanını `days` gün geriye çeker (süre dolmasını taklit eder)."""
    past = datetime.now(timezone.utc) - timedelta(days=days)
    with session_factory() as db:
        db.execute(update(File).where(File.deleted_at.is_not(None)).values(deleted_at=past))
        db.execute(update(Folder).where(Folder.deleted_at.is_not(None)).values(deleted_at=past))
        db.commit()


def test_requires_login(client, storage):
    assert client.get("/trash").status_code == 401
    assert client.post("/trash/restore", json={"file_ids": []}).status_code == 401
    assert client.delete("/trash").status_code == 401


def test_empty_trash_and_default_retention(client, storage, login_as):
    login_as(uid="u1")
    assert _trash(client) == {"retention_days": 30, "files": [], "folders": []}
    assert client.get("/me").json()["trash_retention_days"] == 30


def test_deleted_file_appears_in_trash_with_days_left(client, storage, login_as):
    login_as(uid="u1")
    f = _upload(client, storage, "rapor.pdf", 1234, mime_type="application/pdf")
    client.delete(f"/files/{f['id']}")

    files = _trash(client)["files"]
    assert len(files) == 1
    item = files[0]
    assert item["id"] == f["id"] and item["name"] == "rapor.pdf" and item["size"] == 1234
    assert item["mime_type"] == "application/pdf"
    assert item["days_left"] == 30
    assert item["purge_at"] > item["deleted_at"]


def test_folder_in_trash_hides_its_contents_and_summarises_them(client, storage, login_as):
    login_as(uid="u1")
    top = _folder(client, "Üst", color="#1E88E5")
    sub = _folder(client, "Alt", parent_id=top["id"])
    _upload(client, storage, "a.txt", 1000, folder_id=top["id"])
    _upload(client, storage, "b.txt", 2000, folder_id=sub["id"])
    client.delete(f"/folders/{top['id']}")

    trash = _trash(client)
    assert trash["files"] == []  # klasörün içindekiler ayrıca listelenmez
    assert len(trash["folders"]) == 1
    f = trash["folders"][0]
    assert f["id"] == top["id"] and f["name"] == "Üst" and f["color"] == "#1E88E5"
    assert f["file_count"] == 2 and f["size"] == 3000


def test_restore_file(client, storage, login_as):
    login_as(uid="u1")
    folder = _folder(client, "Belgeler")
    f = _upload(client, storage, "a.txt", folder_id=folder["id"])
    client.delete(f"/files/{f['id']}")
    assert client.get("/files", params={"folder_id": folder["id"]}).json() == []

    assert client.post("/trash/restore", json={"file_ids": [f["id"]]}).json() == {"count": 1}
    assert [x["id"] for x in client.get("/files", params={"folder_id": folder["id"]}).json()] == [f["id"]]
    assert _trash(client)["files"] == []


def test_restore_folder_brings_back_whole_subtree(client, storage, login_as):
    login_as(uid="u1")
    top = _folder(client, "Üst")
    sub = _folder(client, "Alt", parent_id=top["id"])
    _upload(client, storage, "a.txt", folder_id=top["id"])
    _upload(client, storage, "b.txt", folder_id=sub["id"])
    client.delete(f"/folders/{top['id']}")

    assert client.post("/trash/restore", json={"folder_ids": [top["id"]]}).json() == {"count": 1}
    assert [f["name"] for f in client.get("/folders").json()] == ["Üst"]
    assert [f["name"] for f in client.get("/folders", params={"parent_id": top["id"]}).json()] == ["Alt"]
    assert [f["name"] for f in client.get("/files", params={"folder_id": top["id"]}).json()] == ["a.txt"]
    assert [f["name"] for f in client.get("/files", params={"folder_id": sub["id"]}).json()] == ["b.txt"]
    assert _trash(client)["folders"] == []


def test_file_trashed_before_its_folder_stays_in_trash_when_folder_is_restored(client, storage, login_as):
    login_as(uid="u1")
    folder = _folder(client, "Klasör")
    early = _upload(client, storage, "once.txt", folder_id=folder["id"])
    _upload(client, storage, "sonra.txt", folder_id=folder["id"])
    client.delete(f"/files/{early['id']}")        # önce tek başına
    client.delete(f"/folders/{folder['id']}")     # sonra klasör (sonra.txt ile birlikte)

    trash = _trash(client)
    assert [f["id"] for f in trash["files"]] == [early["id"]]  # ayrıca görünür
    assert [f["id"] for f in trash["folders"]] == [folder["id"]]
    assert trash["folders"][0]["file_count"] == 1  # yalnızca birlikte atılan sonra.txt

    client.post("/trash/restore", json={"folder_ids": [folder["id"]]})
    assert [f["name"] for f in client.get("/files", params={"folder_id": folder["id"]}).json()] == ["sonra.txt"]
    assert [f["id"] for f in _trash(client)["files"]] == [early["id"]]

    # Klasör artık yerinde: tek başına geri yüklenen dosya kendi klasörüne döner
    client.post("/trash/restore", json={"file_ids": [early["id"]]})
    names = sorted(f["name"] for f in client.get("/files", params={"folder_id": folder["id"]}).json())
    assert names == ["once.txt", "sonra.txt"]


def test_restore_file_whose_folder_is_still_in_trash_goes_to_top_level(client, storage, login_as):
    login_as(uid="u1")
    folder = _folder(client, "Klasör")
    early = _upload(client, storage, "once.txt", folder_id=folder["id"])
    client.delete(f"/files/{early['id']}")
    client.delete(f"/folders/{folder['id']}")

    client.post("/trash/restore", json={"file_ids": [early["id"]]})
    assert [f["name"] for f in client.get("/files").json()] == ["once.txt"]  # en üst seviyede
    assert [f["id"] for f in _trash(client)["folders"]] == [folder["id"]]


def test_restore_folder_with_name_conflict_is_renamed(client, storage, login_as):
    login_as(uid="u1")
    old = _folder(client, "Belgeler")
    client.delete(f"/folders/{old['id']}")
    _folder(client, "Belgeler")  # çöpteki ad yeni klasörü engellemez

    client.post("/trash/restore", json={"folder_ids": [old["id"]]})
    assert sorted(f["name"] for f in client.get("/folders").json()) == [
        "Belgeler",
        "Belgeler (geri yüklendi)",
    ]


def test_folder_name_can_be_reused_while_old_one_is_in_trash(client, session_factory, login_as):
    login_as(uid="u1")
    parent = _folder(client, "Üst")
    a = _folder(client, "A", parent_id=parent["id"])
    client.delete(f"/folders/{a['id']}")
    assert client.post("/folders", json={"name": "A", "parent_id": parent["id"]}).status_code == 201


def test_cannot_use_a_trashed_folder(client, storage, login_as):
    login_as(uid="u1")
    folder = _folder(client, "Çöpte")
    f = _upload(client, storage, "a.txt")
    client.delete(f"/folders/{folder['id']}")

    assert client.patch(f"/files/{f['id']}", json={"folder_id": folder["id"]}).status_code == 404
    assert client.post("/files/init", json={"name": "b.txt", "size": 10, "folder_id": folder["id"]}).status_code == 404
    assert client.post("/folders", json={"name": "X", "parent_id": folder["id"]}).status_code == 404


def test_trashed_file_can_be_previewed_but_not_modified(client, storage, login_as):
    login_as(uid="u1")
    f = _upload(client, storage)
    client.delete(f"/files/{f['id']}")
    # Çöp kutusunda ön izleme için okuma uçları açık...
    assert client.get(f"/files/{f['id']}").status_code == 200
    assert client.get(f"/files/{f['id']}/download").status_code == 200
    # ...ama değiştirmek / tamamlamak yok.
    assert client.patch(f"/files/{f['id']}", json={"favorite": True}).status_code == 404
    assert client.post(f"/files/{f['id']}/complete").status_code == 404
    # Normal listelerde yine görünmez.
    assert client.get("/files").json() == []


def test_permanently_deleted_file_is_gone_for_preview(client, storage, login_as):
    login_as(uid="u1")
    f = _upload(client, storage)
    client.delete(f"/files/{f['id']}")
    client.post("/trash/delete", json={"file_ids": [f["id"]]})
    assert client.get(f"/files/{f['id']}").status_code == 404


def test_open_trashed_folder_shows_its_contents(client, storage, login_as):
    login_as(uid="u1")
    top = _folder(client, "Üst", color="#1E88E5")
    sub = _folder(client, "Alt", parent_id=top["id"])
    a = _upload(client, storage, "a.txt", 100, folder_id=top["id"])
    b = _upload(client, storage, "b.txt", 200, folder_id=sub["id"])
    client.delete(f"/folders/{top['id']}")

    r = client.get(f"/trash/folders/{top['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["retention_days"] == 30
    assert body["folder"]["name"] == "Üst" and body["folder"]["file_count"] == 2
    assert [f["name"] for f in body["folders"]] == ["Alt"]
    assert body["folders"][0]["file_count"] == 1 and body["folders"][0]["size"] == 200
    assert [f["name"] for f in body["files"]] == ["a.txt"]
    assert body["files"][0]["days_left"] == 30

    # İç içe klasör de açılır ve dosyaları ön izlenebilir.
    inner = client.get(f"/trash/folders/{sub['id']}").json()
    assert [f["name"] for f in inner["files"]] == ["b.txt"]
    assert client.get(f"/files/{b['id']}/download").status_code == 200


def test_open_trashed_folder_hides_items_trashed_separately(client, storage, login_as):
    login_as(uid="u1")
    top = _folder(client, "Üst")
    early = _upload(client, storage, "once.txt", 10, folder_id=top["id"])
    late = _upload(client, storage, "sonra.txt", 20, folder_id=top["id"])
    client.delete(f"/files/{early['id']}")
    client.delete(f"/folders/{top['id']}")
    body = client.get(f"/trash/folders/{top['id']}").json()
    assert [f["name"] for f in body["files"]] == ["sonra.txt"]


def test_open_trashed_folder_rejects_live_missing_and_foreign(client, storage, login_as):
    login_as(uid="u1")
    live = _folder(client, "Canlı")
    gone = _folder(client, "Silik")
    client.delete(f"/folders/{gone['id']}")
    assert client.get(f"/trash/folders/{live['id']}").status_code == 404
    assert client.get("/trash/folders/00000000-0000-0000-0000-000000000000").status_code == 404
    login_as(uid="u2")
    assert client.get(f"/trash/folders/{gone['id']}").status_code == 404


def test_permanent_delete_only_touches_trashed_items(client, storage, login_as):
    login_as(uid="u1")
    live = _upload(client, storage, "canli.txt", 500)
    gone = _upload(client, storage, "silinecek.txt", 700)
    client.delete(f"/files/{gone['id']}")

    r = client.post("/trash/delete", json={"file_ids": [live["id"], gone["id"]]})
    assert r.json() == {"count": 1}  # canlı dosya yok sayıldı
    assert [f["id"] for f in client.get("/files").json()] == [live["id"]]
    assert client.get("/me").json()["used_storage"] == 500
    assert len(storage.deleted) == 1


def test_empty_trash_removes_everything_in_it(client, storage, login_as):
    login_as(uid="u1")
    folder = _folder(client, "K")
    _upload(client, storage, "a.txt", 100, folder_id=folder["id"])
    single = _upload(client, storage, "b.txt", 200)
    keep = _upload(client, storage, "c.txt", 400)
    client.delete(f"/folders/{folder['id']}")
    client.delete(f"/files/{single['id']}")

    assert client.delete("/trash").json() == {"count": 2}
    assert _trash(client)["files"] == [] and _trash(client)["folders"] == []
    assert client.get("/me").json()["used_storage"] == 400
    assert [f["id"] for f in client.get("/files").json()] == [keep["id"]]


def test_unfinished_upload_is_deleted_for_good_not_trashed(client, storage, login_as):
    login_as(uid="u1")
    pending = client.post("/files/init", json={"name": "x.bin", "size": 50}).json()["file"]
    assert client.delete(f"/files/{pending['id']}").status_code == 204
    assert _trash(client)["files"] == []


def test_other_users_trash_is_invisible_and_untouchable(client, storage, login_as):
    login_as(uid="u1")
    f = _upload(client, storage)
    client.delete(f"/files/{f['id']}")

    login_as(uid="u2")
    assert _trash(client)["files"] == []
    assert client.post("/trash/restore", json={"file_ids": [f["id"]]}).json() == {"count": 0}
    assert client.post("/trash/delete", json={"file_ids": [f["id"]]}).json() == {"count": 0}

    login_as(uid="u1")
    assert len(_trash(client)["files"]) == 1


def test_ids_required(client, login_as):
    login_as(uid="u1")
    assert client.post("/trash/restore", json={}).status_code == 422
    assert client.post("/trash/delete", json={"file_ids": [], "folder_ids": []}).status_code == 422


# --- Süre dolunca otomatik silme ------------------------------------------------


def test_listing_trash_purges_expired_items(client, storage, login_as, session_factory):
    login_as(uid="u1")
    f = _upload(client, storage, "eski.txt", 800)
    client.delete(f"/files/{f['id']}")

    _age_trash(session_factory, 29)
    assert len(_trash(client)["files"]) == 1  # 29 gün: henüz süre dolmadı
    assert _trash(client)["files"][0]["days_left"] == 1

    _age_trash(session_factory, 31)
    assert _trash(client)["files"] == []  # 31 gün: kalıcı silindi
    assert client.get("/me").json()["used_storage"] == 0
    assert len(storage.deleted) == 1


def test_expired_folder_is_purged_with_its_contents(client, storage, login_as, session_factory):
    login_as(uid="u1")
    top = _folder(client, "Üst")
    sub = _folder(client, "Alt", parent_id=top["id"])
    _upload(client, storage, "a.txt", 100, folder_id=top["id"])
    _upload(client, storage, "b.txt", 200, folder_id=sub["id"])
    client.delete(f"/folders/{top['id']}")

    _age_trash(session_factory, 31)
    with session_factory() as db:
        user = db.scalar(select(User))
        assert purge_expired(db, storage, user_id=user.id) == 4  # klasör + alt klasör + 2 dosya
    with session_factory() as db:
        assert list(db.scalars(select(Folder))) == []
        assert list(db.scalars(select(File))) == []
    assert client.get("/me").json()["used_storage"] == 0
    assert len(storage.deleted) == 2


def test_retention_setting_changes_expiry(client, storage, login_as, session_factory):
    login_as(uid="u1")
    f = _upload(client, storage, "a.txt", 100)
    client.delete(f"/files/{f['id']}")

    r = client.patch("/me", json={"trash_retention_days": 7})
    assert r.status_code == 200 and r.json()["trash_retention_days"] == 7
    assert client.get("/me").json()["trash_retention_days"] == 7
    assert _trash(client)["retention_days"] == 7
    assert _trash(client)["files"][0]["days_left"] == 7

    _age_trash(session_factory, 8)  # 30 günlük varsayılanda silinmezdi, 7 günlükte silinir
    assert _trash(client)["files"] == []
    assert len(storage.deleted) == 1


def test_retention_validation(client, login_as):
    login_as(uid="u1")
    assert client.patch("/me", json={"trash_retention_days": 0}).status_code == 422
    assert client.patch("/me", json={"trash_retention_days": 366}).status_code == 422
    assert client.patch("/me", json={"trash_retention_days": None}).status_code == 422
    assert client.patch("/me", json={}).status_code == 422
    assert client.patch("/me", json={"trash_retention_days": 365}).json()["trash_retention_days"] == 365


def test_purge_without_storage_skips_files_but_keeps_them(client, storage, login_as, session_factory):
    login_as(uid="u1")
    f = _upload(client, storage, "a.txt", 100)
    client.delete(f"/files/{f['id']}")
    _age_trash(session_factory, 31)

    with session_factory() as db:
        assert purge_expired(db, None) == 0  # depolama yok: nesneler silinemez, kayıt da silinmez
    with session_factory() as db:
        assert len(list(db.scalars(select(File)))) == 1
    with session_factory() as db:
        assert purge_expired(db, storage) == 1


def test_restore_file_with_name_conflict_gets_restored_suffix(client, storage, login_as):
    login_as(uid="u1")
    old = _upload(client, storage, "rapor.pdf")
    client.delete(f"/files/{old['id']}")
    _upload(client, storage, "rapor.pdf")  # aynı ad artık boş: yeni dosya adını aldı

    assert client.post("/trash/restore", json={"file_ids": [old["id"]]}).json() == {"count": 1}
    names = sorted(f["name"] for f in client.get("/files").json())
    assert names == ["rapor (geri yüklendi).pdf", "rapor.pdf"]


def test_restore_several_same_named_files_numbers_the_suffix(client, storage, login_as):
    login_as(uid="u1")
    a = _upload(client, storage, "x.txt")
    client.delete(f"/files/{a['id']}")
    b = _upload(client, storage, "x.txt")
    client.delete(f"/files/{b['id']}")
    _upload(client, storage, "x.txt")

    client.post("/trash/restore", json={"file_ids": [a["id"], b["id"]]})
    names = sorted(f["name"] for f in client.get("/files").json())
    assert names == ["x (geri yüklendi 2).txt", "x (geri yüklendi).txt", "x.txt"]


def test_restore_file_without_conflict_keeps_its_name(client, storage, login_as):
    login_as(uid="u1")
    f = _upload(client, storage, "x.txt")
    client.delete(f"/files/{f['id']}")
    client.post("/trash/restore", json={"file_ids": [f["id"]]})
    assert [x["name"] for x in client.get("/files").json()] == ["x.txt"]
