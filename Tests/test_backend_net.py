"""Outbound-proxy settings (/api/net/proxy) — store, mask, apply, clear.

The proxy URL lives in the encrypted credential store (it may embed Basic-auth
credentials) and is exported into the worker's environment so every `requests`
call inherits it. These tests isolate both: the store via
ARCHVALIDATOR_CONFIG_DIR, the environment via monkeypatch.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath("src"))

from fastapi.testclient import TestClient

from backend import net
from backend.app import create_app

TOKEN = "arch-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHVALIDATOR_CONFIG_DIR", str(tmp_path))
    # A host machine's own proxy env must not leak into assertions, and the
    # module-level "applied" marker must reset between tests.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(net, "_applied_url", None)
    with TestClient(create_app(token=TOKEN)) as c:
        yield c


def test_default_is_not_manual(client):
    r = client.get("/api/net/proxy", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["manual_configured"] is False
    assert body["source"] in ("system", "direct")


def test_requires_token(client):
    assert client.get("/api/net/proxy").status_code in (401, 403)


def test_rejects_invalid_urls(client):
    for bad in ("", "proxy.company.com:8080", "ftp://proxy:21", "http://"):
        r = client.put("/api/net/proxy", json={"url": bad}, headers=AUTH)
        assert r.status_code == 422, bad


def test_set_masks_password_and_exports_env(client):
    r = client.put(
        "/api/net/proxy",
        json={"url": "http://alice:s3cret@proxy.corp:8080"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "manual"
    assert body["manual_configured"] is True
    assert "s3cret" not in body["proxy_url"]
    assert "alice" in body["proxy_url"]

    # Applied live to the worker environment (what requests will use) — with
    # the real credentials, and loopback exempted.
    assert os.environ["HTTPS_PROXY"] == "http://alice:s3cret@proxy.corp:8080"
    assert "127.0.0.1" in os.environ["NO_PROXY"]

    # GET must not leak the password either.
    r = client.get("/api/net/proxy", headers=AUTH)
    assert "s3cret" not in r.json()["proxy_url"]


def test_clear_restores_system(client):
    client.put("/api/net/proxy", json={"url": "http://proxy.corp:8080"}, headers=AUTH)
    assert os.environ.get("HTTPS_PROXY") == "http://proxy.corp:8080"

    r = client.delete("/api/net/proxy", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["manual_configured"] is False
    assert os.environ.get("HTTPS_PROXY") is None


def test_environment_proxy_wins(client, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:3128")
    monkeypatch.setattr(net, "_applied_url", None)
    r = client.get("/api/net/proxy", headers=AUTH)
    body = r.json()
    assert body["source"] == "environment"
    assert body["proxy_url"] == "http://env-proxy:3128"
