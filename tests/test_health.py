import requests

from conftest import SHLINK_BASE


def test_liveness_does_not_touch_shlink(client, mocked):
    assert client.get("/healthz").get_data(as_text=True) == "ok"
    assert len(mocked.calls) == 0


def test_deep_health_ok(client, mocked):
    mocked.get(SHLINK_BASE + "/rest/health", json={"status": "pass", "version": "5.1.8"})
    assert client.get("/healthz?deep=1").status_code == 200


def test_deep_health_shlink_down(client, mocked):
    mocked.get(SHLINK_BASE + "/rest/health", body=requests.ConnectionError())
    resp = client.get("/healthz?deep=1")
    assert resp.status_code == 503
    assert resp.get_data(as_text=True) == "shlink unavailable"
