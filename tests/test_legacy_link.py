"""Legacy `?link=` endpoint as used by PrivateBin's browser-side shortener button."""
import json
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
