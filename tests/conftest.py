import pytest
import responses

import app as app_module

SHLINK_BASE = "https://shlink.test"
SHLINK_ENDPOINT = SHLINK_BASE + "/rest/v3/short-urls"
API_KEY = "test-api-key-0000"


@pytest.fixture
def mocked():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


def make_config(**overrides):
    env = {"SHLINK_URL": SHLINK_BASE, "SHLINK_API_KEY": API_KEY}
    env.update(overrides)
    return app_module.Config.from_env(env)


@pytest.fixture
def make_client():
    def factory(**env):
        return app_module.create_app(make_config(**env)).test_client()
    return factory


@pytest.fixture
def client(make_client):
    return make_client()


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
