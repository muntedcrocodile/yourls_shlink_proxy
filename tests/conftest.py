import pytest
import responses

import app as app_module

SHLINK_ENDPOINT = "https://shlink.test/rest/v3/short-urls"
API_KEY = "test-api-key-0000"


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "SHLINK_URL", SHLINK_ENDPOINT)
    monkeypatch.setattr(app_module, "SHLINK_API_KEY", API_KEY)
    app_module.app.config.update(TESTING=False)
    return app_module.app.test_client()


def shlink_short_url(code="abc12", long_url="https://bin.example/?x#key"):
    return {
        "shortUrl": f"https://s.example/{code}",
        "shortCode": code,
        "longUrl": long_url,
        "dateCreated": "2026-11-14T13:13:05+00:00",
        "tags": ["privatebin"],
        "meta": {"validSince": None, "validUntil": None, "maxVisits": None},
        "domain": None,
        "title": None,
        "crawlable": False,
        "forwardQuery": True,
        "visitsSummary": {"total": 0, "nonBots": 0, "bots": 0},
    }
