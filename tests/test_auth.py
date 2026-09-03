"""HTTP basic authentication."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def guarded(tmp_path, watch_config, provider, prices, sample_transfers):
    provider.transfers = sample_transfers
    settings = make_settings(tmp_path, auth_user="rofl", auth_password="hunter2")
    app = create_app(settings=settings, config=watch_config, provider=provider,
                     price_source=prices, run_indexer=False)
    with TestClient(app) as client:
        yield client


def test_without_credentials_nothing_is_served(guarded):
    for path in ("/", "/activity", "/wallets", "/summary", "/api/activity", "/static/app.css"):
        response = guarded.get(path)
        assert response.status_code == 401, path
        assert "Basic" in response.headers["www-authenticate"]


def test_correct_credentials_pass(guarded):
    assert guarded.get("/", headers=header("rofl", "hunter2")).status_code == 200
    assert guarded.get("/api/status", headers=header("rofl", "hunter2")).status_code == 200


@pytest.mark.parametrize(
    "user,password",
    [("rofl", "wrong"), ("wrong", "hunter2"), ("", ""), ("rofl", "hunter2 ")],
)
def test_wrong_credentials_are_refused(guarded, user, password):
    assert guarded.get("/", headers=header(user, password)).status_code == 401


def test_a_malformed_header_is_refused_not_crashed(guarded):
    for value in ("Basic", "Basic !!!!", "Bearer abc", "Basic " + "a" * 5, ""):
        assert guarded.get("/", headers={"Authorization": value}).status_code == 401


def test_writes_are_guarded_too(guarded):
    """The add-wallet route changes state; it must not be the way in."""
    response = guarded.post(
        "/wallets",
        data={"name": "Evil", "address": "0x" + "a" * 40, "chain": "ethereum"},
        headers={"sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 401


def test_the_healthcheck_stays_reachable_but_says_nothing(guarded):
    """The container healthcheck runs without credentials, so this one path is
    exempt — and therefore must not describe the instance."""
    response = guarded.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    for leaked in ("head_block", "activities", "sync_state"):
        assert leaked not in response.text


def test_an_open_instance_says_so(tmp_path, watch_config, provider, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="blocktail"):
        create_app(settings=make_settings(tmp_path), config=watch_config,
                   provider=provider, run_indexer=False)

    assert any("open to anyone" in record.message for record in caplog.records)
