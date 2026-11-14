"""YOURLS API emulation: actions, parameters and output formats."""
import json
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import pytest
import requests

from conftest import SHLINK_BASE, SHLINK_ENDPOINT, shlink_short_url

PASTE = "https://bin.example/?abc123#Decrypt10nKey"


def problem(kind, status, title, detail, **extra):
    return {"type": f"https://shlink.io/api/error/{kind}", "status": status,
            "title": title, "detail": detail, **extra}


def api(client, method="get", path="/yourls-api.php", **params):
    if method == "get":
        return client.get(path + "?" + urlencode(params))
    return client.post(path, data=params)


# ---- shorturl ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["/yourls-api.php", "/"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_shorturl_json(client, mocked, path, method):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    resp = api(client, method, path, action="shorturl", format="json", url=PASTE)
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    data = resp.get_json()
    assert data["status"] == "success"
    assert data["statusCode"] == 200
    assert data["shorturl"] == "https://s.example/abc12"
    assert data["url"]["keyword"] == "abc12"
    assert data["url"]["url"] == PASTE
    assert data["url"]["date"] == "2026-11-14 13:13:05"
    assert "simple" not in data
    assert json.loads(mocked.calls[0].request.body)["longUrl"] == PASTE


def test_privatebin_yourls_proxy_request_shape(client, mocked):
    # PrivateBin's server-side YourlsProxy POSTs form data and accepts the
    # result iff (int)statusCode === 200 and 'shorturl' is set.
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    resp = client.post("/yourls-api.php", data={"signature": "", "format": "json",
                                                 "action": "shorturl", "url": PASTE})
    data = resp.get_json()
    assert int(data["statusCode"]) == 200 and data["shorturl"] == "https://s.example/abc12"


def test_shorturl_message_does_not_echo_long_url(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    data = api(client, action="shorturl", format="json", url=PASTE).get_json()
    assert PASTE not in data["message"]


def test_shorturl_simple(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    resp = api(client, action="shorturl", format="simple", url=PASTE)
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "https://s.example/abc12"


def test_shorturl_default_format_is_xml(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    resp = api(client, action="shorturl", url=PASTE)
    assert resp.mimetype == "application/xml"
    root = ET.fromstring(resp.get_data())
    assert root.tag == "result"
    assert root.findtext("shorturl") == "https://s.example/abc12"
    assert root.findtext("url/url") == PASTE
    assert root.findtext("statusCode") == "200"


def test_xml_escapes_special_characters(client, mocked):
    long_url = "https://bin.example/?a=1&b=<2>"
    mocked.post(SHLINK_ENDPOINT, json={**shlink_short_url("abc12", long_url), "title": 'T & "Q" <x>'})
    resp = api(client, action="shorturl", url=long_url)
    root = ET.fromstring(resp.get_data())
    assert root.findtext("url/url") == long_url
    assert root.findtext("title") == 'T & "Q" <x>'


def test_jsonp(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("abc12", PASTE))
    resp = api(client, action="shorturl", format="jsonp", callback="cb_1", url=PASTE)
    assert resp.mimetype == "application/javascript"
    body = resp.get_data(as_text=True)
    assert body.startswith("/**/cb_1(") and body.endswith(");")
    assert json.loads(body[len("/**/cb_1("):-2])["shorturl"] == "https://s.example/abc12"


@pytest.mark.parametrize("cb", ["", "alert(1)//", "a b", "x;y", "<script>"])
def test_jsonp_rejects_unsafe_callbacks(client, cb):
    resp = api(client, action="version", format="jsonp", callback=cb)
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"


def test_shorturl_with_keyword_and_title(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json={**shlink_short_url("mine", PASTE), "title": "Hello"})
    data = api(client, action="shorturl", format="json", url=PASTE, keyword="mine", title="Hello").get_json()
    sent = json.loads(mocked.calls[0].request.body)
    assert sent["customSlug"] == "mine" and sent["title"] == "Hello"
    assert data["url"]["keyword"] == "mine" and data["title"] == "Hello"


def test_keyword_taken_is_error_keyword(client, mocked):
    mocked.post(SHLINK_ENDPOINT, status=400, json=problem(
        "non-unique-slug", 400, "Invalid custom slug", 'Provided slug "mine" is already in use.', customSlug="mine"))
    resp = api(client, action="shorturl", format="json", url=PASTE, keyword="mine")
    data = resp.get_json()
    assert data["status"] == "fail" and data["code"] == "error:keyword"
    assert "shorturl" not in data   # never fabricate a short URL
    assert resp.status_code == 200  # YOURLS: a valid request with a failed result


@pytest.mark.parametrize("kw", ["a/b", "../x", "a b", "ü", "x" * 101, "a+b"])
def test_invalid_keyword_rejected_before_shlink(client, mocked, kw):
    # Shlink silently rewrites some slugs (e.g. 'a/b' -> 'a-b' on 5.1.8), so the
    # caller would get a different keyword than requested.
    resp = api(client, action="shorturl", format="json", url=PASTE, keyword=kw)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "error:keyword"
    assert len(mocked.calls) == 0


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "notaurl"])
def test_shorturl_bad_url(client, mocked, url):
    resp = api(client, action="shorturl", format="json", url=url)
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["status"] == "fail" and data["code"] == "error:nourl"
    assert len(mocked.calls) == 0


def test_shorturl_ignores_legacy_link_param(client, mocked):
    # The deployed variant read only ?link= and answered YOURLS clients with
    # "400: No link provided".
    resp = api(client, action="shorturl", format="json", link=PASTE)
    assert resp.get_json()["code"] == "error:nourl"


def test_shlink_unreachable_json_error(client, mocked):
    mocked.post(SHLINK_ENDPOINT, body=requests.ConnectionError("refused"))
    resp = api(client, action="shorturl", format="json", url=PASTE)
    assert resp.status_code == 502
    data = resp.get_json()
    assert data["statusCode"] == 502 and data["message"] == "Cannot reach Shlink"
    assert "shorturl" not in data


def test_shlink_timeout_simple_error(client, mocked):
    mocked.post(SHLINK_ENDPOINT, body=requests.ReadTimeout())
    resp = api(client, action="shorturl", format="simple", url=PASTE)
    assert resp.status_code == 504
    assert resp.get_data(as_text=True) == "Timed out waiting for Shlink"


# ---- expand / url-stats -----------------------------------------------------

def test_expand_keyword(client, mocked):
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls/abc12", json=shlink_short_url("abc12", PASTE))
    data = api(client, action="expand", format="json", shorturl="abc12").get_json()
    assert data == {"keyword": "abc12", "shorturl": "https://s.example/abc12", "longurl": PASTE,
                    "title": "", "message": "success", "statusCode": 200}


def test_expand_full_short_url_passes_domain(client, mocked):
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls/abc12", json=shlink_short_url("abc12", PASTE))
    resp = api(client, action="expand", format="simple", shorturl="https://s.example/abc12")
    assert resp.get_data(as_text=True) == PASTE
    assert "domain=s.example" in mocked.calls[0].request.url


def test_expand_encodes_keyword_in_path(client, mocked):
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls/..%2Fvisits", status=404, json=problem(
        "short-url-not-found", 404, "Short URL not found", "nope"))
    resp = api(client, action="expand", format="json", shorturl="../visits")
    assert resp.status_code == 404
    assert mocked.calls[0].request.url.startswith(SHLINK_BASE + "/rest/v3/short-urls/..%2Fvisits")


@pytest.mark.parametrize("err", [
    problem("short-url-not-found", 404, "Short URL not found", "No URL found"),
    {"type": "INVALID_SHORTCODE", "status": 404, "title": "x", "detail": "y"},   # Shlink v2
])
def test_expand_not_found(client, mocked, err):
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls/nope", status=404, json=err)
    resp = api(client, action="expand", format="json", shorturl="nope")
    assert resp.status_code == 404
    data = resp.get_json()
    assert data["message"] == "Error: could not find keyword" and data["errorCode"] == "404"


def test_expand_missing_shorturl(client, mocked):
    resp = api(client, action="expand", format="json")
    assert resp.status_code == 404
    assert len(mocked.calls) == 0


def test_url_stats(client, mocked):
    rec = {**shlink_short_url("abc12", PASTE), "visitsSummary": {"total": 7, "nonBots": 5, "bots": 2}}
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls/abc12", json=rec)
    data = api(client, action="url-stats", format="json", shorturl="abc12").get_json()
    assert data["statusCode"] == 200
    assert data["link"]["clicks"] == 7 and data["link"]["url"] == PASTE


def test_url_stats_shlink_v2_visits_count(client, mocked):
    rec = shlink_short_url("abc12", PASTE)
    del rec["visitsSummary"]
    rec["visitsCount"] = 3
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls/abc12", json=rec)
    data = api(client, action="url-stats", format="json", shorturl="abc12").get_json()
    assert data["link"]["clicks"] == 3


# ---- misc -------------------------------------------------------------------

def test_version(client):
    data = api(client, action="version", format="json").get_json()
    assert data["statusCode"] == 200 and data["version"]


@pytest.mark.parametrize("params", [{}, {"action": "nope"}, {"action": ""}])
def test_unknown_action(client, params):
    resp = api(client, format="json", **params)
    assert resp.status_code == 400
    assert resp.get_json()["message"] == 'Unknown or missing "action" parameter'


def test_unknown_format_falls_back_to_simple(client):
    resp = api(client, action="version", format="yaml")
    assert resp.mimetype == "text/plain"


@pytest.mark.parametrize("params", [
    {"action": "expand", "shorturl": "http://[x/abc"},       # urlsplit ValueError
    {"action": "url-stats", "shorturl": "https://[::1/abc"},
])
def test_malformed_shorturl_is_404_not_500(client, mocked, params):
    resp = api(client, format="json", **params)
    assert resp.status_code == 404
    assert len(mocked.calls) == 0


@pytest.mark.parametrize("url", ["https://bin.example/?a\x01b", "https://bin.example/?a\x7fb",
                                 "https://bin.example/?a\x00b"])
def test_control_characters_in_url_rejected(client, mocked, url):
    resp = api(client, action="shorturl", format="json", url=url)
    assert resp.status_code == 400
    assert len(mocked.calls) == 0


def test_xml_output_stays_well_formed(client, mocked):
    # Regression: a control character from Shlink (e.g. in a page title)
    # produced XML that clients could not parse.
    mocked.post(SHLINK_ENDPOINT, json={**shlink_short_url("abc12", PASTE), "title": "bad\x01\x1ftitle￾"})
    resp = api(client, action="shorturl", url=PASTE)
    root = ET.fromstring(resp.get_data())
    assert root.findtext("title") == "bad��title�"
