"""SHLINK_TIMEOUT must bound the whole request (PrivateBin aborts after 10s)."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

import app as app_module

PASTE = "https://bin.example/?abc#key"


class Trickle(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = b'{"shortUrl": "https://s.example/abc", "shortCode": "abc"}'
        try:
            for b in body:            # one byte every 0.2s: no single read times out
                self.wfile.write(bytes([b]))
                self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def trickle_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_slow_dripping_upstream_hits_total_deadline(trickle_server):
    # Regression: requests' timeout is per socket read, so this used to take
    # ~12s and succeed, long after the browser had given up.
    config = app_module.Config.from_env({"SHLINK_URL": trickle_server, "SHLINK_API_KEY": "k",
                                         "SHLINK_TIMEOUT": "1"})
    client = app_module.create_app(config).test_client()
    start = time.monotonic()
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    elapsed = time.monotonic() - start
    assert resp.status_code == 504
    assert elapsed < 2


def test_budget_is_shared_between_calls(make_client, mocked, monkeypatch):
    # The exhausted-duplicate path makes several Shlink calls; together they
    # must stay within one SHLINK_TIMEOUT.
    from conftest import SHLINK_ENDPOINT, shlink_short_url
    clock = {"t": 1000.0}
    monkeypatch.setattr(app_module.time, "monotonic", lambda: clock["t"])
    dead = {**shlink_short_url("dead", PASTE), "meta": {"maxVisits": 1}, "visitsSummary": {"total": 1}}

    def slow_find(request):
        clock["t"] += 4.5             # first call eats most of the 5s budget
        return (200, {}, __import__("json").dumps(dead))

    mocked.add_callback("POST", SHLINK_ENDPOINT, callback=slow_find)
    mocked.add_callback("GET", SHLINK_ENDPOINT, callback=slow_find)
    resp = make_client(SHLINK_MAX_VISITS="1").get("/?link=" + quote(PASTE, safe=""))
    assert resp.status_code == 504
    assert mocked.calls[0].request.req_kwargs["timeout"] == 5
    if len(mocked.calls) > 1:
        assert mocked.calls[1].request.req_kwargs["timeout"] <= 0.5
