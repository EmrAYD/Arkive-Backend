import uuid

from sqlalchemy import func, select

from app.models import Folder


def _create(client, name, **extra):
    return client.post("/folders", json={"name": name, **extra})


def test_requires_login(client):
    assert client.get("/folders").status_code == 401
    assert _create(client, "A").status_code == 401


def test_create_root_folder_defaults_to_home(client, login_as):
    login_as(uid="u1", email="a@example.com")
    r = _create(client, "Belgeler")
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Belgeler"
    assert body["parent_id"] is None
    assert body["location"] == "home"


def test_create_root_folder_in_ai_results(client, login_as):
    login_as(uid="u1")
    r = _create(client, "Özetler", location="ai_results")
    assert r.status_code == 201
    assert r.json()["location"] == "ai_results"


def test_child_inherits_parent_location(client, login_as):
    login_as(uid="u1")
    parent = _create(client, "Özetler", location="ai_results").json()
    child = _create(client, "Ekim", parent_id=parent["id"])
    assert child.status_code == 201
    assert child.json()["location"] == "ai_results"
    assert child.json()["parent_id"] == parent["id"]


def test_child_with_conflicting_location_is_400(client, login_as):
    login_as(uid="u1")
    parent = _create(client, "Belgeler").json()  # home
    r = _create(client, "Alt", parent_id=parent["id"], location="ai_results")
    assert r.status_code == 400


def test_name_is_trimmed_and_validated(client, login_as):
    login_as(uid="u1")
    assert _create(client, "  Fotoğraflar  ").json()["name"] == "Fotoğraflar"
    assert _create(client, "   ").status_code == 422
    assert _create(client, "").status_code == 422
    assert _create(client, "a/b").status_code == 422
    assert _create(client, "a\\b").status_code == 422
    assert _create(client, "x" * 256).status_code == 422


def test_duplicate_name_same_place_is_409_case_insensitive(client, login_as):
    login_as(uid="u1")
    assert _create(client, "Belgeler").status_code == 201
    assert _create(client, "Belgeler").status_code == 409
    assert _create(client, "belgeler").status_code == 409


def test_same_name_allowed_in_other_places(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    b = _create(client, "B").json()
    assert _create(client, "Ortak", parent_id=a["id"]).status_code == 201
    assert _create(client, "Ortak", parent_id=b["id"]).status_code == 201  # başka üst klasör
    assert _create(client, "Ortak").status_code == 201  # kökte
    assert _create(client, "Ortak", location="ai_results").status_code == 201  # başka sayfa
    assert _create(client, "Ortak").status_code == 409  # kökte ikincisi


def test_same_name_allowed_for_different_users(client, login_as):
    login_as(uid="u1")
    assert _create(client, "Belgeler").status_code == 201
    login_as(uid="u2")
    assert _create(client, "Belgeler").status_code == 201


def test_list_roots_by_location(client, login_as):
    login_as(uid="u1")
    _create(client, "b-home")
    _create(client, "A-home")
    _create(client, "ai-1", location="ai_results")

    home = client.get("/folders").json()  # varsayılan: home
    assert [f["name"] for f in home] == ["A-home", "b-home"]  # harf farkı yok sayılarak sıralı

    ai = client.get("/folders", params={"location": "ai_results"}).json()
    assert [f["name"] for f in ai] == ["ai-1"]


def test_list_children_only_one_level(client, login_as):
    login_as(uid="u1")
    root = _create(client, "Kök").json()
    child = _create(client, "Çocuk", parent_id=root["id"]).json()
    _create(client, "Torun", parent_id=child["id"])

    kids = client.get("/folders", params={"parent_id": root["id"]}).json()
    assert [f["name"] for f in kids] == ["Çocuk"]
    assert [f["name"] for f in client.get("/folders").json()] == ["Kök"]


def test_get_one_folder(client, login_as):
    login_as(uid="u1")
    f = _create(client, "A").json()
    assert client.get(f"/folders/{f['id']}").json()["name"] == "A"
    assert client.get(f"/folders/{uuid.uuid4()}").status_code == 404


def test_cannot_touch_other_users_folders(client, login_as):
    login_as(uid="u1")
    f = _create(client, "Gizli").json()

    login_as(uid="u2")
    assert client.get(f"/folders/{f['id']}").status_code == 404
    assert client.patch(f"/folders/{f['id']}", json={"name": "X"}).status_code == 404
    assert client.delete(f"/folders/{f['id']}").status_code == 404
    assert client.get("/folders", params={"parent_id": f["id"]}).status_code == 404
    assert _create(client, "Sızma", parent_id=f["id"]).status_code == 404
    assert client.get("/folders").json() == []

    login_as(uid="u1")  # asıl sahibin klasörü bozulmadı
    assert client.get(f"/folders/{f['id']}").json()["name"] == "Gizli"


def test_rename(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    _create(client, "B")

    r = client.patch(f"/folders/{a['id']}", json={"name": " Yeni "})
    assert r.status_code == 200 and r.json()["name"] == "Yeni"
    assert client.patch(f"/folders/{a['id']}", json={"name": "b"}).status_code == 409
    # Kendi adını (harf farkıyla) vermek çakışma sayılmaz.
    assert client.patch(f"/folders/{a['id']}", json={"name": "YENİ"}).status_code == 200
    assert client.patch(f"/folders/{a['id']}", json={"name": ""}).status_code == 422


def test_delete_moves_subtree_to_trash_and_trash_removes_it(client, login_as, session_factory):
    login_as(uid="u1")
    root = _create(client, "Kök").json()
    child = _create(client, "Çocuk", parent_id=root["id"]).json()
    _create(client, "Torun", parent_id=child["id"])
    other = _create(client, "Dokunulmaz").json()

    assert client.delete(f"/folders/{root['id']}").status_code == 204

    # Listelerden ve tek tek erişimden kayboldu...
    assert [f["name"] for f in client.get("/folders").json()] == ["Dokunulmaz"]
    assert client.get(f"/folders/{root['id']}").status_code == 404
    assert client.get("/folders", params={"parent_id": child["id"]}).status_code == 404
    assert client.get(f"/folders/{other['id']}").status_code == 200
    assert client.delete(f"/folders/{root['id']}").status_code == 404
    # ...ama veritabanında duruyor (çöpte)
    with session_factory() as db:
        assert len(list(db.scalars(select(Folder.name)))) == 4

    assert client.delete("/trash").json() == {"count": 1}
    with session_factory() as db:
        assert list(db.scalars(select(Folder.name))) == ["Dokunulmaz"]


def test_db_unique_index_blocks_duplicates_even_without_app_check(client, login_as, session_factory):
    """Uygulama kontrolü atlansa bile veritabanı çift kaydı reddetmeli (yarış durumu güvencesi)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    login_as(uid="u1")
    client.get("/me")  # kullanıcıyı oluştur
    root = _create(client, "Kök").json()
    _create(client, "Alt", parent_id=root["id"])

    with session_factory() as db:
        existing = db.scalar(select(Folder).where(Folder.name == "Alt"))
        db.add(Folder(user_id=existing.user_id, parent_id=existing.parent_id,
                      name="Alt", location="home"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        r = db.scalar(select(Folder).where(Folder.name == "Kök"))
        db.add(Folder(user_id=r.user_id, parent_id=None, name="Kök", location="home"))
        with pytest.raises(IntegrityError):
            db.commit()


# --- Renk ve favori ------------------------------------------------------------


def test_color_and_favorite_defaults_and_create(client, login_as):
    login_as(uid="u1")
    plain = _create(client, "Düz").json()
    assert plain["color"] is None and plain["favorite"] is False

    fancy = _create(client, "Renkli", color="#1e88e5", favorite=True).json()
    assert fancy["color"] == "#1E88E5"  # büyük harfe çevrilir
    assert fancy["favorite"] is True


def test_invalid_color_is_422(client, login_as):
    login_as(uid="u1")
    for bad in ["red", "#12345", "#GGGGGG", "1E88E5", "#1E88E5FF"]:
        assert _create(client, "X", color=bad).status_code == 422, bad


def test_patch_only_changes_sent_fields(client, login_as):
    login_as(uid="u1")
    f = _create(client, "A", color="#112233").json()

    r = client.patch(f"/folders/{f['id']}", json={"favorite": True}).json()
    assert r["favorite"] is True and r["color"] == "#112233" and r["name"] == "A"

    r = client.patch(f"/folders/{f['id']}", json={"color": None}).json()  # rengi sil
    assert r["color"] is None and r["favorite"] is True

    r = client.patch(f"/folders/{f['id']}", json={"color": "#abcdef", "name": "B"}).json()
    assert r["color"] == "#ABCDEF" and r["name"] == "B"


def test_patch_rejects_empty_or_null_required_fields(client, login_as):
    login_as(uid="u1")
    f = _create(client, "A").json()
    assert client.patch(f"/folders/{f['id']}", json={}).status_code == 422
    assert client.patch(f"/folders/{f['id']}", json={"name": None}).status_code == 422
    assert client.patch(f"/folders/{f['id']}", json={"favorite": None}).status_code == 422


# --- Klasör taşıma (PATCH parent_id) ---------------------------------------


def _move(client, folder_id, parent_id):
    return client.patch(f"/folders/{folder_id}", json={"parent_id": parent_id})


def test_move_folder_into_another_and_back_to_root(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    b = _create(client, "B").json()

    r = _move(client, b["id"], a["id"])
    assert r.status_code == 200
    assert r.json()["parent_id"] == a["id"] and r.json()["location"] == "home"
    assert [f["id"] for f in client.get("/folders", params={"parent_id": a["id"]}).json()] == [b["id"]]

    r = _move(client, b["id"], None)  # null = ana sayfanın en üstü
    assert r.status_code == 200 and r.json()["parent_id"] is None


def test_move_keeps_subtree_and_omitted_parent_keeps_place(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    b = _create(client, "B").json()
    c = _create(client, "C", parent_id=b["id"]).json()

    assert _move(client, b["id"], a["id"]).status_code == 200
    # parent_id gönderilmeyen güncelleme klasörü yerinde bırakır
    r = client.patch(f"/folders/{b['id']}", json={"favorite": True}).json()
    assert r["parent_id"] == a["id"]
    # alt klasör hâlâ B'nin içinde
    assert client.get(f"/folders/{c['id']}").json()["parent_id"] == b["id"]


def test_cannot_move_folder_into_itself_or_its_descendant(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    b = _create(client, "B", parent_id=a["id"]).json()
    c = _create(client, "C", parent_id=b["id"]).json()

    assert _move(client, a["id"], a["id"]).status_code == 400
    assert _move(client, a["id"], b["id"]).status_code == 400
    assert _move(client, a["id"], c["id"]).status_code == 400


def test_move_folder_name_conflict_is_409(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    _create(client, "Aynı", parent_id=a["id"])
    other = _create(client, "aynı").json()  # kökte; büyük/küçük harf farkı yok sayılır
    assert _move(client, other["id"], a["id"]).status_code == 409


def test_move_and_rename_together_checks_new_name_at_destination(client, login_as):
    login_as(uid="u1")
    a = _create(client, "A").json()
    _create(client, "Aynı", parent_id=a["id"])
    other = _create(client, "Aynı").json()
    assert _move(client, other["id"], a["id"]).status_code == 409
    r = client.patch(f"/folders/{other['id']}", json={"parent_id": a["id"], "name": "Başka"})
    assert r.status_code == 200 and r.json()["name"] == "Başka"


def test_ai_results_folders_cannot_move_or_receive(client, login_as):
    login_as(uid="u1")
    ai = _create(client, "Özetler", location="ai_results").json()
    home = _create(client, "Belgeler").json()
    assert _move(client, home["id"], ai["id"]).status_code == 400   # hedef AI sonuçları
    assert _move(client, ai["id"], home["id"]).status_code == 400   # taşınan AI sonuçları


def test_move_folder_to_unknown_or_other_users_parent_is_404(client, login_as):
    import uuid

    login_as(uid="u1")
    mine = _create(client, "Benim").json()
    assert _move(client, mine["id"], str(uuid.uuid4())).status_code == 404

    login_as(uid="u2")
    theirs = _create(client, "Onun").json()
    login_as(uid="u1")
    assert _move(client, mine["id"], theirs["id"]).status_code == 404
