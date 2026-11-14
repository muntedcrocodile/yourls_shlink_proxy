import pytest

import app as app_module
from app import Config, ConfigError

BASE_ENV = {"SHLINK_API_KEY": "k"}


@pytest.mark.parametrize("url, base, version", [
    ("https://s.example", "https://s.example", "3"),
    ("https://s.example/", "https://s.example", "3"),
    ("http://shlink:8080", "http://shlink:8080", "3"),
    # Original README form: full endpoint. Live config uses /rest/v2/.
    ("https://s.example/rest/v2/short-urls", "https://s.example", "2"),
    ("https://s.example/rest/v3/short-urls/", "https://s.example", "3"),
    ("https://s.example/rest/v3", "https://s.example", "3"),
    ("https://example.com/shlink/rest/v3/short-urls", "https://example.com/shlink", "3"),
])
def test_shlink_url_forms(url, base, version):
    c = Config.from_env({**BASE_ENV, "SHLINK_URL": url})
    assert (c.shlink_base, c.shlink_api_version) == (base, version)
    assert c.short_urls_endpoint == f"{base}/rest/v{version}/short-urls"


def test_explicit_api_version_wins():
    c = Config.from_env({**BASE_ENV, "SHLINK_URL": "https://s.example/rest/v2/short-urls",
                         "SHLINK_API_VERSION": "v3"})
    assert c.shlink_api_version == "3"


@pytest.mark.parametrize("env, msg", [
    ({}, "SHLINK_URL, SHLINK_API_KEY"),
    ({"SHLINK_URL": "https://s.example"}, "SHLINK_API_KEY"),
    ({"SHLINK_API_KEY": "k"}, "SHLINK_URL"),
    ({"SHLINK_URL": "  ", "SHLINK_API_KEY": "k"}, "SHLINK_URL"),
    ({"SHLINK_URL": "s.example", "SHLINK_API_KEY": "k"}, "http(s) URL"),
    ({"SHLINK_URL": "https://s.example?x=1", "SHLINK_API_KEY": "k"}, "query"),
    ({"SHLINK_URL": "https://s.example", "SHLINK_API_KEY": "k", "SHLINK_TIMEOUT": "soon"}, "SHLINK_TIMEOUT"),
    ({"SHLINK_URL": "https://s.example", "SHLINK_API_KEY": "k", "SHLINK_TIMEOUT": "0"}, "SHLINK_TIMEOUT"),
])
def test_invalid_config_fails_fast(env, msg):
    # Regression: with SHLINK_URL unset the app started fine and then 500'd
    # on every request (requests.exceptions.MissingSchema).
    with pytest.raises(ConfigError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        Config.from_env(env)


def test_gunicorn_app_attribute_fails_fast_without_config(monkeypatch):
    monkeypatch.delenv("SHLINK_URL", raising=False)
    monkeypatch.delenv("SHLINK_API_KEY", raising=False)
    monkeypatch.delitem(app_module.__dict__, "app", raising=False)
    with pytest.raises(ConfigError):
        app_module.app


def test_gunicorn_app_attribute_builds_from_env(monkeypatch):
    monkeypatch.setenv("SHLINK_URL", "https://s.example/rest/v2/short-urls")
    monkeypatch.setenv("SHLINK_API_KEY", "k")
    monkeypatch.delitem(app_module.__dict__, "app", raising=False)
    flask_app = app_module.app
    assert flask_app.config["PROXY"].short_urls_endpoint == "https://s.example/rest/v2/short-urls"
    monkeypatch.delitem(app_module.__dict__, "app")
