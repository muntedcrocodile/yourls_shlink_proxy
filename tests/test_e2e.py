"""End-to-end tests against a real, throwaway Shlink container.

Run with `make e2e` (or `pytest -m e2e`). Needs Docker. Everything binds to
127.0.0.1 and is removed afterwards. Override the Shlink image with
SHLINK_IMAGE (default shlinkio/shlink:stable; production runs 5.1.8).
"""
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest
import requests

import app as app_module

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
SHLINK_IMAGE = os.environ.get("SHLINK_IMAGE", "shlinkio/shlink:stable")
API_KEY = "e2e-" + uuid.uuid4().hex
PASTE = "https://bin.example/?e2e" + uuid.uuid4().hex[:8] + "#Decrypt10nKey/+="


def docker(*args, check=True, timeout=600):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=timeout)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not become ready")


@pytest.fixture(scope="session")
def shlink():
    if not shutil.which("docker") or docker("info", check=False).returncode != 0:
        pytest.skip("docker not available")
    tag = uuid.uuid4().hex[:8]
    net, name, port = f"yslp-e2e-{tag}", f"yslp-e2e-shlink-{tag}", free_port()
    docker("network", "create", net)
    try:
        docker("run", "-d", "--name", name, "--network", net, "--network-alias", "shlink",
               "-p", f"127.0.0.1:{port}:8080",
               "-e", f"DEFAULT_DOMAIN=127.0.0.1:{port}", "-e", "IS_HTTPS_ENABLED=false",
               "-e", f"INITIAL_API_KEY={API_KEY}", "-e", "DISABLE_TRACKING=false",
               "-e", "AUTO_RESOLVE_TITLES=false",
               SHLINK_IMAGE)
        base = f"http://127.0.0.1:{port}"
        wait_for(base + "/rest/health")
        version = requests.get(base + "/rest/health", timeout=5).json().get("version")
        print(f"\nShlink {version} ({SHLINK_IMAGE}) at {base}")
        yield {"base": base, "network": net, "version": version}
    finally:
        docker("rm", "-f", name, check=False)
        docker("network", "rm", net, check=False)


@pytest.fixture
def make_client(shlink):
    def factory(**env):
        env = {"SHLINK_URL": shlink["base"], "SHLINK_API_KEY": API_KEY, **env}
        return app_module.create_app(app_module.Config.from_env(env)).test_client()
    return factory


def follow_once(short_url):
    return requests.get(short_url, allow_redirects=False, timeout=5)


def test_legacy_link_creates_working_short_url(make_client):
    client = make_client()
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    short = resp.get_data(as_text=True)
    hop = follow_once(short)
    assert hop.status_code in (301, 302)
    # The fragment (PrivateBin's key) must survive the round trip.
    assert hop.headers["Location"] == PASTE


def test_full_v2_endpoint_config_still_works(make_client, shlink):
    # The live deployment configures SHLINK_URL=.../rest/v2/short-urls.
    client = make_client(SHLINK_URL=shlink["base"] + "/rest/v2/short-urls")
    resp = client.get("/?link=" + quote(PASTE + "v2", safe=""))
    assert resp.status_code == 200
    assert follow_once(resp.get_data(as_text=True)).headers["Location"] == PASTE + "v2"


def test_duplicate_returns_same_working_link(make_client):
    client = make_client()
    url = PASTE + "dup"
    a = client.get("/?link=" + quote(url, safe="")).get_data(as_text=True)
    b = client.get("/?link=" + quote(url, safe="")).get_data(as_text=True)
    assert a == b
    assert follow_once(b).headers["Location"] == url


def test_yourls_shorturl_expand_and_stats(make_client):
    client = make_client()
    kw = "e2e" + uuid.uuid4().hex[:6]
    url = "https://例え.テスト/パス?q=ü&r=a%2Fb#frag"
    created = client.post("/yourls-api.php", data={"action": "shorturl", "format": "json",
                                                   "url": url, "keyword": kw, "title": "Tïtle"}).get_json()
    assert created["status"] == "success", created
    assert created["url"]["keyword"] == kw
    assert created["shorturl"].endswith("/" + kw)
    assert created["title"] == "Tïtle"

    for short in (kw, created["shorturl"]):
        expanded = client.get("/yourls-api.php?" + urlencode(
            {"action": "expand", "format": "json", "shorturl": short})).get_json()
        assert expanded["longurl"] == url, expanded

    follow_once(created["shorturl"])
    stats = client.get("/yourls-api.php?" + urlencode(
        {"action": "url-stats", "format": "json", "shorturl": kw})).get_json()
    assert stats["link"]["url"] == url
    assert stats["link"]["clicks"] >= 1

    taken = client.get("/yourls-api.php?" + urlencode(
        {"action": "shorturl", "format": "json", "url": url + "x", "keyword": kw})).get_json()
    assert taken["code"] == "error:keyword" and "shorturl" not in taken


def test_expand_unknown(make_client):
    resp = make_client().get("/yourls-api.php?action=expand&format=json&shorturl=doesnotexist")
    assert resp.status_code == 404


def test_signed_db_stats_and_stats(make_client):
    secret = "e2e-signature-" + uuid.uuid4().hex
    client = make_client(YOURLS_SIGNATURE=secret)
    assert client.get("/yourls-api.php?action=db-stats&format=json").status_code == 403
    client.get("/yourls-api.php?" + urlencode({"action": "shorturl", "url": PASTE + "s", "signature": secret}))
    data = client.get(f"/yourls-api.php?action=db-stats&format=json&signature={secret}").get_json()
    assert data["db-stats"]["total_links"] >= 1
    data = client.get(f"/yourls-api.php?action=stats&filter=last&limit=1&format=json&signature={secret}").get_json()
    assert data["statusCode"] == 200 and len(data["links"]) == 1


def test_exhausted_link_is_not_reused(make_client):
    client = make_client(SHLINK_MAX_VISITS="1")
    url = PASTE + "max"
    first = client.get("/?link=" + quote(url, safe="")).get_data(as_text=True)
    assert follow_once(first).status_code in (301, 302)
    assert follow_once(first).status_code == 404          # max visits reached
    second = client.get("/?link=" + quote(url, safe="")).get_data(as_text=True)
    assert second != first
    # Shlink's findIfExists keeps returning the exhausted *oldest* link; the
    # proxy must find and reuse the live replacement instead of minting more.
    third = client.get("/?link=" + quote(url, safe="")).get_data(as_text=True)
    assert third == second
    assert follow_once(second).headers["Location"] == url


def test_javascript_url_rejected(make_client):
    assert make_client().get("/?link=" + quote("javascript:alert(1)", safe="")).status_code == 400


def test_wrong_api_key_is_502(make_client):
    resp = make_client(SHLINK_API_KEY="wrong").get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 502


def test_unreachable_shlink_is_502(make_client):
    # What production does today (SHLINK_URL -> origin refusing connections).
    resp = make_client(SHLINK_URL=f"http://127.0.0.1:{free_port()}").get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 502
    assert resp.get_data(as_text=True) == "Error: Cannot reach Shlink"


def test_deep_health(make_client):
    assert make_client().get("/healthz?deep=1").status_code == 200


def test_docker_image(shlink):
    """Build the real image and drive it over HTTP on the Shlink network."""
    tag = f"yourls_shlink_proxy:e2e-{uuid.uuid4().hex[:8]}"
    name, port = f"yslp-e2e-proxy-{uuid.uuid4().hex[:8]}", free_port()
    docker("build", "-q", "-t", tag, str(ROOT))
    try:
        docker("run", "-d", "--name", name, "--network", shlink["network"],
               "-p", f"127.0.0.1:{port}:8080", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true",
               "-e", "SHLINK_URL=http://shlink:8080", "-e", f"SHLINK_API_KEY={API_KEY}",
               "-e", "ALLOWED_URL_PREFIXES=https://bin.example/?", tag)
        base = f"http://127.0.0.1:{port}"
        wait_for(base + "/healthz", timeout=30)
        assert requests.get(base + "/healthz?deep=1", timeout=10).status_code == 200

        r = requests.get(base + "/?link=" + quote(PASTE, safe=""),
                         headers={"Origin": "https://bin.example"}, timeout=10)
        assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == "*"
        assert follow_once(r.text).headers["Location"] == PASTE

        r = requests.post(base + "/yourls-api.php", timeout=10, data={
            "action": "shorturl", "format": "json", "url": PASTE, "signature": ""})
        assert r.json()["statusCode"] == 200 and r.json()["shorturl"]

        assert requests.get(base + "/?link=" + quote("https://evil.example/", safe=""), timeout=10).status_code == 400

        files = docker("exec", name, "sh", "-c", "ls -A /app; id -u").stdout.split()
        assert ".git" not in files and ".env" not in files and files[-1] != "0"

        logs = docker("logs", name).stdout + docker("logs", name).stderr
        assert "Decrypt10nKey" not in logs and API_KEY not in logs
        assert '"GET /"' in logs        # access log, without the query string
    finally:
        docker("rm", "-f", name, check=False)
        docker("rmi", "-f", tag, check=False)
