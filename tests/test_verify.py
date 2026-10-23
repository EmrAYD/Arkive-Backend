"""Firebase ID token doğrulamasını GERÇEK google-auth koduyla, çevrimdışı test eder.

Google'ın sertifika indirmesi taklit edilir; token'lar test anahtarıyla gerçekten imzalanır.
Böylece imza, süre, audience ve issuer kontrolleri gerçekten çalışmış olur.
"""
import datetime
import time

import google.auth.exceptions
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt
from google.oauth2 import id_token as google_id_token

PROJECT = "arkive-test"  # conftest.py'deki FIREBASE_PROJECT_ID ile aynı
KID = "test-key-1"


def _make_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "arkive-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
def keys():
    return {"trusted": _make_key(), "attacker": _make_key()}


@pytest.fixture(autouse=True)
def fake_google_certs(monkeypatch, keys):
    """Google'ın yayınladığı sertifikalar yerine güvenilen test sertifikasını sun."""
    _, cert_pem = keys["trusted"]
    monkeypatch.setattr(google_id_token, "_fetch_certs", lambda request, url: {KID: cert_pem})


def make_token(key_pem, **overrides):
    now = int(time.time())
    payload = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "firebase-uid-1",
        "auth_time": now - 60,
        "iat": now - 60,
        "exp": now + 3600,
        "email": "ali@example.com",
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    signer = crypt.RSASigner.from_string(key_pem, key_id=KID)
    return jwt.encode(signer, payload).decode()


def call_me(client, token):
    return client.get("/me", headers={"Authorization": f"Bearer {token}"})


def test_valid_token_logs_in_and_creates_user(client, keys):
    r = call_me(client, make_token(keys["trusted"][0]))

    assert r.status_code == 200
    assert r.json()["email"] == "ali@example.com"
    assert "name" not in r.json()


def test_garbage_token_is_401(client):
    r = call_me(client, "bu-bir-token-degil")
    assert r.status_code == 401
    assert r.json()["detail"] == "Geçersiz veya süresi dolmuş token"


def test_expired_token_is_401(client, keys):
    now = int(time.time())
    token = make_token(keys["trusted"][0], iat=now - 7200, auth_time=now - 7200, exp=now - 3600)
    assert call_me(client, token).status_code == 401


def test_token_signed_by_attacker_key_is_401(client, keys):
    token = make_token(keys["attacker"][0])
    assert call_me(client, token).status_code == 401


def test_wrong_audience_is_401(client, keys):
    token = make_token(keys["trusted"][0], aud="baska-bir-firebase-projesi")
    assert call_me(client, token).status_code == 401


def test_wrong_issuer_is_401(client, keys):
    token = make_token(keys["trusted"][0], iss="https://securetoken.google.com/baska-proje")
    assert call_me(client, token).status_code == 401


def test_missing_sub_is_401(client, keys):
    token = make_token(keys["trusted"][0], sub=None)
    assert call_me(client, token).status_code == 401


def test_auth_time_in_future_is_401(client, keys):
    token = make_token(keys["trusted"][0], auth_time=int(time.time()) + 3600)
    assert call_me(client, token).status_code == 401


def test_cert_fetch_failure_is_503_not_401(client, keys, monkeypatch):
    def boom(request, url):
        raise google.auth.exceptions.TransportError("Google'a ulaşılamıyor")

    monkeypatch.setattr(google_id_token, "_fetch_certs", boom)
    r = call_me(client, make_token(keys["trusted"][0]))
    assert r.status_code == 503
