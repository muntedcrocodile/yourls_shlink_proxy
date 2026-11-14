"""Legacy `?link=` endpoint as used by PrivateBin's browser-side shortener button."""
import json

import responses
from urllib.parse import quote

from conftest import API_KEY, SHLINK_ENDPOINT, shlink_short_url

PASTE = "https://bin.example/?abc123#Decrypt10nKey"


def test_link_returns_short_url_as_text(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 200
    assert resp.get_data(as_text=True) == "https://s.example/abc12"


def test_link_sends_api_key_and_full_url_including_fragment(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    client.get("/?link=" + quote(PASTE, safe=""))
    sent = mocked.calls[0].request
    assert sent.headers["X-Api-Key"] == API_KEY
    body = json.loads(sent.body)
    assert body["longUrl"] == PASTE
    assert body["crawlable"] is False


def _problem(kind, status, title, detail, **extra):
    return {"type": f"https://shlink.io/api/error/{kind}", "status": status,
            "title": title, "detail": detail, **extra}


def test_shlink_validation_error_is_400_not_500(client, mocked):
    # Regression: response_json['shortUrl'] raised KeyError -> HTML 500 page.
    mocked.post(SHLINK_ENDPOINT, status=400, json=_problem(
        "invalid-data", 400, "Invalid data", "Provided data is not valid", invalidElements=["longUrl"]))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 400
    assert resp.mimetype == "text/plain"
    assert "Provided data is not valid" in resp.get_data(as_text=True)


def test_error_body_never_contains_a_url(client, mocked):
    # PrivateBin picks the shortest http(s):// URL out of *any* response body,
    # so an error message that echoes a URL would be shown as the short link.
    mocked.post(SHLINK_ENDPOINT, status=400, json=_problem(
        "invalid-url", 400, "Invalid URL", f"Provided URL {PASTE} is invalid"))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 400
    assert "http://" not in resp.get_data(as_text=True)
    assert "https://" not in resp.get_data(as_text=True)


def test_slug_conflict_is_reported_not_fabricated(client, mocked):
    # Regression for the deployed (uncommitted) variant, which on any Shlink
    # 400 fabricated "<SHLINK host>/<slug>" -- a link that may not exist or may
    # point at somebody else's paste.
    mocked.post(SHLINK_ENDPOINT, status=400, json=_problem(
        "non-unique-slug", 400, "Invalid custom slug", 'Provided slug "abc" is already in use.', customSlug="abc"))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 400
    assert "https://" not in resp.get_data(as_text=True)


def test_v2_style_error_codes_are_understood(client, mocked):
    mocked.post(SHLINK_ENDPOINT, status=400, json={"type": "INVALID_ARGUMENT", "status": 400,
                                                   "title": "Invalid data", "detail": "Provided data is not valid"})
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 400


def test_invalid_api_key_is_502_and_hides_details(client, mocked):
    mocked.post(SHLINK_ENDPOINT, status=401, json=_problem(
        "invalid-api-key", 401, "Invalid API key", "Provided API key does not exist or is invalid."))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 502
    assert API_KEY not in resp.get_data(as_text=True)


def test_non_json_upstream_response_is_502(client, mocked):
    mocked.post(SHLINK_ENDPOINT, status=502, body="<html>Bad Gateway</html>", content_type="text/html")
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 502


def test_upstream_connection_refused_is_502(client, mocked):
    # The live failure: SHLINK_URL pointed at an origin that refuses
    # connections, and every request became an unhandled 500.
    import requests
    mocked.post(SHLINK_ENDPOINT, body=requests.ConnectionError("Connection refused"))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 502
    assert resp.get_data(as_text=True) == "Error: Cannot reach Shlink"


def test_upstream_timeout_is_504(client, mocked):
    import requests
    mocked.post(SHLINK_ENDPOINT, body=requests.ReadTimeout("slow"))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 504


def test_upstream_request_has_a_timeout(client, monkeypatch):
    import app as app_module
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs)
        raise app_module.requests.ConnectionError()

    monkeypatch.setattr(app_module.requests, "request", fake_request)
    client.get("/?link=" + quote(PASTE, safe=""))
    assert 0 < seen["timeout"] < 10


import pytest


@pytest.mark.parametrize("query", ["/", "/?link=", "/?link=%20%20"])
def test_missing_link_is_400_without_calling_shlink(client, mocked, query):
    resp = client.get(query)
    assert resp.status_code == 400
    assert len(mocked.calls) == 0


@pytest.mark.parametrize("bad", [
    "javascript:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "//no-scheme.example/x",
    "not a url",
    "https://",
    "https://exa mple.com/",
])
def test_unsafe_or_malformed_urls_are_rejected(client, mocked, bad):
    # Shlink 5.1.8 accepts javascript: URLs; the proxy must not forward them.
    resp = client.get("/?link=" + quote(bad, safe=""))
    assert resp.status_code == 400
    assert len(mocked.calls) == 0


def test_overlong_url_is_rejected(client, mocked):
    resp = client.get("/?link=" + quote("https://bin.example/?" + "a" * 9000, safe=""))
    assert resp.status_code == 400
    assert len(mocked.calls) == 0


def test_unicode_and_encoded_characters_survive_round_trip(client, mocked):
    url = "https://例え.テスト/パス?q=ü&r=a%2Fb&s=1+2#Kéy/+="
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    resp = client.get("/?link=" + quote(url, safe=""))
    assert resp.status_code == 200
    assert json.loads(mocked.calls[0].request.body)["longUrl"] == url


def test_surrounding_whitespace_is_trimmed(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    client.get("/?link=" + quote("  " + PASTE + "\n", safe=""))
    assert json.loads(mocked.calls[0].request.body)["longUrl"] == PASTE


def test_paste_key_is_never_logged(client, mocked, caplog):
    # Regression: logging.info(response_json) wrote the whole Shlink reply,
    # including longUrl with the PrivateBin decryption key, to the logs.
    import logging
    import requests
    caplog.set_level(logging.DEBUG)
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    client.get("/?link=" + quote(PASTE, safe=""))
    mocked.replace(responses.POST, SHLINK_ENDPOINT, body=requests.ConnectionError(f"failed for {PASTE}"))
    client.get("/?link=" + quote(PASTE, safe=""))
    mocked.replace(responses.POST, SHLINK_ENDPOINT, status=400, json=_problem(
        "invalid-url", 400, "Invalid URL", f"Provided URL {PASTE} is invalid"))
    client.get("/?link=" + quote(PASTE, safe=""))
    assert "abc12" in caplog.text
    assert "Decrypt10nKey" not in caplog.text
    assert API_KEY not in caplog.text


def test_short_urls_are_not_limited_to_100_visits_by_default(client, mocked):
    # Regression: maxVisits was hard-coded to 100, so every short link
    # silently started returning 404 after its 100th visit (bots included).
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    client.get("/?link=" + quote(PASTE, safe=""))
    body = json.loads(mocked.calls[0].request.body)
    assert "maxVisits" not in body
    assert body["tags"] == ["privatebin"]
    assert "domain" not in body


def test_short_url_options_are_configurable(make_client, mocked):
    client = make_client(SHLINK_MAX_VISITS="100", SHLINK_TAGS="PrivateBin, paste",
                         SHLINK_DOMAIN="s.example", SHLINK_CRAWLABLE="true")
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    client.get("/?link=" + quote(PASTE, safe=""))
    body = json.loads(mocked.calls[0].request.body)
    assert body["maxVisits"] == 100
    assert body["tags"] == ["PrivateBin", "paste"]
    assert body["domain"] == "s.example"
    assert body["crawlable"] is True
