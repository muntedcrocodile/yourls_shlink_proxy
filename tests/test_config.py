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


@pytest.mark.parametrize("env", [{"SHLINK_MAX_VISITS": "0"}, {"SHLINK_MAX_VISITS": "x"},
                                 {"SHLINK_CRAWLABLE": "maybe"}])
def test_invalid_short_url_options(env):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, "SHLINK_URL": "https://s.example", **env})


def test_scheme_and_length_settings_are_read():
    # Regression: a refactor read the literal keys 'cfg().allowed_url_schemes'
    # and 'cfg().max_url_length', silently ignoring the documented variables.
    c = Config.from_env({**BASE_ENV, "SHLINK_URL": "https://s.example",
                         "ALLOWED_URL_SCHEMES": "https", "MAX_URL_LENGTH": "100"})
    assert c.allowed_url_schemes == frozenset({"https"})
    assert c.max_url_length == 100


def test_scheme_and_length_settings_are_enforced(make_client, mocked):
    client = make_client(ALLOWED_URL_SCHEMES="https", MAX_URL_LENGTH="40")
    assert client.get("/?link=http%3A%2F%2Fbin.example%2F%3Fa").status_code == 400
    assert client.get("/?link=https%3A%2F%2Fbin.example%2F%3F" + "a" * 40).status_code == 400
    assert len(mocked.calls) == 0


@pytest.mark.parametrize("env, msg", [
    ({"YOURLS_SIGNATURE": "x" * 16, "YOURLS_SIGNATURE_TTL": "0"}, "YOURLS_SIGNATURE_TTL"),
    ({"YOURLS_SIGNATURE": "x" * 16, "YOURLS_SIGNATURE_TTL": "-5"}, "YOURLS_SIGNATURE_TTL"),
    ({"MAX_URL_LENGTH": "0"}, "MAX_URL_LENGTH"),
    ({"ALLOWED_URL_SCHEMES": " , "}, "ALLOWED_URL_SCHEMES"),
    ({"SHLINK_URL": "https://user:pw@s.example"}, "credentials"),
])
def test_more_invalid_settings(env, msg):
    with pytest.raises(ConfigError, match=msg):
        Config.from_env({**BASE_ENV, "SHLINK_URL": "https://s.example", **env})
