"""The Hermes Desktop Gadgets page's backend: the desk request queue, its routes, and the screen
activity the gateway records for the page's live mirrors.

Everything here runs without Hermes. The routes need FastAPI; pairing uses a stand-in for Hermes's
pairing store.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import types

import pytest

from conftest import REPO

from hermes_gadget_plugin import desk as deskmod
from hermes_gadget_plugin import status as live_status
from hermes_gadget_plugin.desk import Desk
from hermes_gadget_plugin.store import DeviceStore

DASHBOARD = REPO / "plugin" / "dashboard"
DEVICE = "hg-0123456789abcdef"


# -- the request queue --------------------------------------------------------------------------


def test_a_card_goes_through_the_inbox_and_comes_back_done(tmp_path):
    desk = Desk(tmp_path)
    req = desk.post(DEVICE, "card", title="  Shopping ", text="Milk, eggs", seconds="15")
    assert desk.result(req) is None
    [(got_id, got)] = desk.pending()
    assert got_id == req
    assert {k: got[k] for k in ("device", "kind", "title", "text", "seconds")} == {
        "device": DEVICE, "kind": "card", "title": "Shopping", "text": "Milk, eggs", "seconds": 15}

    desk.finish(req, {"ok": True, "held": False})
    assert desk.pending() == []
    assert desk.result(req)["ok"] is True
    assert desk.wait(req, 1.0)["held"] is False
    assert not list((tmp_path / "desk").rglob("*.tmp"))


@pytest.mark.parametrize("device, kind, fields, message", [
    ("hg-nope", "card", {"text": "x"}, "unknown device"),
    ("../../etc", "card", {"text": "x"}, "unknown device"),
    (DEVICE, "reboot", {}, "unknown request"),
    (DEVICE, "card", {"title": " ", "text": ""}, "write something to show"),
    (DEVICE, "action", {"action": "light set"}, "unknown action"),
    (DEVICE, "action", {"action": "../x"}, "unknown action"),
    (DEVICE, "action", {"action": "light.set", "args": ["#fff"]}, "small object"),
    (DEVICE, "action", {"action": "light.set", "args": {"x": "y" * 3000}}, "small object"),
])
def test_bad_requests_are_refused_with_a_sentence(tmp_path, device, kind, fields, message):
    with pytest.raises(ValueError, match=message):
        Desk(tmp_path).post(device, kind, **fields)
    assert not list((tmp_path / "desk").rglob("*.json"))


def test_card_text_and_time_are_bounded(tmp_path):
    desk = Desk(tmp_path)
    req = desk.post(DEVICE, "card", title="t" * 500, text="x" * 5000, seconds=10**9)
    [(_, got)] = desk.pending()
    assert len(got["title"]) == deskmod.MAX_TITLE and len(got["text"]) == deskmod.MAX_TEXT
    assert got["seconds"] == 3600
    desk.finish(req, {"ok": True})
    desk.post(DEVICE, "card", text="x", seconds="soon")
    assert desk.pending()[0][1]["seconds"] == 20


def test_a_full_inbox_says_the_gateway_may_be_down(tmp_path, monkeypatch):
    monkeypatch.setattr(deskmod, "MAX_PENDING", 2)
    desk = Desk(tmp_path)
    desk.post(DEVICE, "card", text="a")
    desk.post(DEVICE, "card", text="b")
    with pytest.raises(ValueError, match="gateway running"):
        desk.post(DEVICE, "card", text="c")


def test_stale_and_malformed_requests_are_dropped_not_run(tmp_path):
    desk = Desk(tmp_path)
    old = desk.post(DEVICE, "card", text="old")
    fresh = desk.post(DEVICE, "action", action="light.set", args={"color": "#ff0000"})
    path = desk.inbox / f"{old}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "at": time.time() - deskmod.EXPIRE_S - 1}))
    (desk.inbox / "not-a-request.json").write_text("{}")
    (desk.inbox / f"{'e' * 32}.json").write_text("[1, 2]")
    (desk.inbox / f"{'f' * 32}.json").write_text(json.dumps({"kind": "shell", "at": time.time()}))

    assert [r for r, _ in desk.pending()] == [fresh]
    assert sorted(p.name for p in desk.inbox.iterdir()) == [f"{fresh}.json"]


def test_the_gateway_skips_a_request_the_page_withdrew(tmp_path):
    # The gateway loads a batch, and an action ahead of a card can take 15 s; by then the card's
    # page may have given up and deleted it, and the card must not run late.
    desk = Desk(tmp_path)
    req = desk.post(DEVICE, "card", text="late")
    assert desk.waiting(req)
    desk.wait(req, 0.05, poll=0.02)  # gives up and withdraws it
    assert not desk.waiting(req)
    assert not desk.waiting("../../devices")


def test_a_request_nobody_answers_is_withdrawn(tmp_path):
    desk = Desk(tmp_path)
    req = desk.post(DEVICE, "card", text="hello")
    got = desk.wait(req, 0.2, poll=0.05)
    assert got["ok"] is False and "gateway" in got["error"]
    assert desk.pending() == []  # a gateway that starts later won't show it


def test_results_only_by_their_own_id_and_old_ones_are_cleared(tmp_path):
    desk = Desk(tmp_path)
    assert desk.result("../../devices") is None
    a = desk.post(DEVICE, "card", text="a")
    desk.finish(a, {"ok": True})
    old = desk.done / f"{a}.json"
    then = time.time() - deskmod.RESULT_KEEP_S - 5
    os.utime(old, (then, then))
    b = desk.post(DEVICE, "card", text="b")
    desk.finish(b, {"ok": True})
    assert not old.exists() and desk.result(b) is not None


# -- the routes --------------------------------------------------------------------------------


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_gadget_desktop_test",
                                                  DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Approvals:
    """Hermes's pairing store, as far as the routes use it."""

    def __init__(self):
        self.approved: set[str] = set()
        self.codes: dict[str, str] = {}

    def is_approved(self, platform, user_id):
        return platform == "gadget" and user_id in self.approved

    def approve_code(self, platform, code):
        user = self.codes.pop(code, None)
        if platform != "gadget" or user is None:
            return None
        self.approved.add(user)
        return {"user_id": user}


@pytest.fixture
def api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    module = _load_plugin_api()
    monkeypatch.setattr(module, "_data_dir", lambda: tmp_path)
    approvals = _Approvals()
    gateway = types.ModuleType("gateway")
    pairing = types.ModuleType("gateway.pairing")
    pairing.PairingStore = lambda: approvals
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.pairing", pairing)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/gadget")
    # Hermes Desktop on the gateway's own computer; `remote` is one on another computer.
    return types.SimpleNamespace(client=TestClient(app, client=("127.0.0.1", 50000)),
                                 remote=TestClient(app, client=("192.168.1.30", 50000)),
                                 dir=tmp_path, module=module, approvals=approvals)


def _enroll(directory, device_id=DEVICE, name="Desk", paired=True):
    store = DeviceStore(directory)
    store.enroll(device_id, b"k" * 32, name=name, board="hermes-desktop", address="127.0.0.1")
    if paired:
        store.confirm(device_id)
    return store


def test_pairing_lists_waiting_codes_and_approves_one(api):
    store = _enroll(api.dir, paired=False)
    assert api.client.get("/api/plugins/gadget/pairing").json() == {"ok": True, "waiting": []}

    store.remember_pairing(DEVICE, "NK3QFN5N", "hermes pairing approve gadget NK3QFN5N", 300)
    api.approvals.codes["NK3QFN5N"] = DEVICE
    waiting = api.client.get("/api/plugins/gadget/pairing").json()["waiting"]
    assert waiting == [{"id": DEVICE, "name": "Desk", "board": "hermes-desktop", "code": "NK3QFN5N"}]

    assert api.client.post(f"/api/plugins/gadget/pairing/{DEVICE}/approve").json() == {"ok": True, "approved": DEVICE}
    assert api.approvals.is_approved("gadget", DEVICE)
    assert api.client.get("/api/plugins/gadget/pairing").json()["waiting"] == []
    again = api.client.post(f"/api/plugins/gadget/pairing/{DEVICE}/approve").json()
    assert again["ok"] is False and again["status"] == 404


def test_approving_an_expired_code_says_so(api):
    store = _enroll(api.dir, paired=False)
    store.remember_pairing(DEVICE, "OLDCODE1", "x", 300)  # Hermes no longer knows this code
    got = api.client.post(f"/api/plugins/gadget/pairing/{DEVICE}/approve").json()
    assert got["ok"] is False and got["status"] == 409 and "expired" in got["error"]
    assert not api.approvals.approved


def _answer(directory, reply):
    """A gateway: answers the first request that arrives."""
    desk = Desk(directory)
    seen = []

    def run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for req_id, req in desk.pending():
                seen.append(req)
                desk.finish(req_id, reply(req))
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, seen


def test_card_and_action_routes_return_the_gateways_answer(api):
    thread, seen = _answer(api.dir, lambda req: {"ok": True, "held": False})
    got = api.client.post("/api/plugins/gadget/devices/" + DEVICE + "/card",
                          params={"title": "Shopping", "text": "Milk", "seconds": "20"}).json()
    thread.join()
    assert got["ok"] is True and got["held"] is False
    assert seen[0]["title"] == "Shopping" and seen[0]["seconds"] == 20

    thread, seen = _answer(api.dir, lambda req: {"ok": True, "result": {"light": req["args"]["color"]}})
    got = api.client.post("/api/plugins/gadget/devices/" + DEVICE + "/action",
                          params={"name": "light.set", "args": json.dumps({"color": "#7c5cff"})}).json()
    thread.join()
    assert got["ok"] is True and got["result"] == {"light": "#7c5cff"}


def test_card_and_action_routes_refuse_bad_input_without_queueing(api):
    base = "/api/plugins/gadget/devices/"
    assert api.client.post(base + DEVICE + "/action", params={"name": "light.set", "args": "{nope"}).json() == {
        "ok": False, "error": "args must be JSON", "status": 400}
    assert api.client.post(base + "hg-bad/card", params={"text": "x"}).json()["error"] == "unknown device"
    assert api.client.post(base + DEVICE + "/card").json()["error"] == "write something to show"
    assert not list((api.dir / "desk").rglob("*.json"))


def test_a_card_with_no_gateway_running_fails_quickly(api, monkeypatch):
    real_wait = Desk.wait
    monkeypatch.setattr(api.module.Desk, "wait", lambda self, req, timeout: real_wait(self, req, 0.2, poll=0.05))
    got = api.client.post(f"/api/plugins/gadget/devices/{DEVICE}/card", params={"text": "hi"}).json()
    assert got["ok"] is False and "gateway" in got["error"]
    assert not list((api.dir / "desk" / "inbox").glob("*.json"))


def test_devices_carry_the_screen_activity_cleaned(api):
    _enroll(api.dir)
    live_status.write(api.dir, {DEVICE: {
        "online": True, "paired": True, "actions": ["light.set", 7, "timer.start"],
        "activity": {"state": "rm -rf", "heard": "h" * 400, "reply": None, "status": "Looking", "at": 12.5,
                     "secret": "x"}}})
    [device] = api.client.get("/api/plugins/gadget/devices").json()["devices"]
    assert device["paired"] is True and device["actions"] == ["light.set", "timer.start"]
    assert device["activity"] == {"state": "ready", "heard": "h" * 280, "reply": "", "status": "Looking", "at": 12.5}


def test_info_says_where_a_local_gadget_connects(api, monkeypatch):
    cli = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"platforms": {"gadget": {"enabled": True, "extra": {"port": "9000", "path": "dev/"}}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.delenv("GADGET_ACCESS_TOKEN", raising=False)
    got = api.client.get("/api/plugins/gadget/info").json()
    assert got["enabled"] is True and got["running"] is False
    assert got["url"] == "ws://127.0.0.1:9000/dev" and got["token_required"] is False
    assert got["url_source"] == "local"

    config.load_config = lambda: {"platforms": {"gadget": {"extra": {"port": "nope"}}}}
    monkeypatch.setenv("GADGET_ACCESS_TOKEN", "t")
    got = api.client.get("/api/plugins/gadget/info").json()
    assert got["enabled"] is False and got["url"] == "ws://127.0.0.1:8765/gadget" and got["token_required"] is True


@pytest.mark.parametrize("host, url", [
    ("0.0.0.0", "ws://127.0.0.1:8765/gadget"),       # a wildcard bind includes loopback
    ("::", "ws://127.0.0.1:8765/gadget"),
    ("192.168.1.20", "ws://192.168.1.20:8765/gadget"),  # a specific address does not
    ("::1", "ws://[::1]:8765/gadget"),
])
def test_info_connects_where_the_gateway_listens(api, monkeypatch, host, url):
    cli = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"platforms": {"gadget": {"enabled": True, "extra": {"host": host}}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    assert api.client.get("/api/plugins/gadget/info").json()["url"] == url


def test_info_sees_a_token_set_in_the_config(api, monkeypatch):
    cli = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"platforms": {"gadget": {"enabled": True, "extra": {"access_token": "s3cret"}}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.delenv("GADGET_ACCESS_TOKEN", raising=False)
    got = api.client.get("/api/plugins/gadget/info").json()
    assert got["token_required"] is True and "s3cret" not in str(got)



def _config(monkeypatch, extra):
    cli = types.ModuleType("hermes_cli")
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"platforms": {"gadget": {"enabled": True, "extra": extra}}}
    monkeypatch.setitem(sys.modules, "hermes_cli", cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)


def _reached(directory, *urls, now=None):
    now = time.time() if now is None else now
    live_status.write(directory, {}, now=now, reached=[{"url": u, "at": now - i} for i, u in enumerate(urls)])


def test_info_on_the_gateways_computer_stays_local(api, monkeypatch):
    # Desktop next to the gateway: loopback, whatever devices and the config say about elsewhere.
    _config(monkeypatch, {"public_url": "wss://gadget.example.com/gadget"})
    _reached(api.dir, "wss://seen.example.com/gadget")
    got = api.client.get("/api/plugins/gadget/info").json()
    assert (got["url"], got["url_source"]) == ("ws://127.0.0.1:8765/gadget", "local")


def test_info_for_another_computer_offers_where_a_paired_gadget_connected(api, monkeypatch):
    _config(monkeypatch, {})
    _reached(api.dir, "wss://gadget.home.example.com/gadget", "ws://192.168.1.20:8765/gadget")
    got = api.remote.get("/api/plugins/gadget/info", headers={"Host": "hermes.home.example.com"}).json()
    assert (got["url"], got["url_source"]) == ("wss://gadget.home.example.com/gadget", "seen")


def test_info_behind_a_proxy_on_the_gateways_computer_is_not_local(api, monkeypatch):
    # A reverse proxy on the same computer makes every request come from loopback; its forwarding
    # headers say the user is elsewhere.
    _config(monkeypatch, {})
    _reached(api.dir, "wss://gadget.home.example.com/gadget")
    got = api.client.get("/api/plugins/gadget/info",
                         headers={"X-Forwarded-For": "203.0.113.9", "Host": "hermes.example.com"}).json()
    assert (got["url"], got["url_source"]) == ("wss://gadget.home.example.com/gadget", "seen")


def test_info_config_public_url_wins_over_what_was_seen(api, monkeypatch):
    _config(monkeypatch, {"public_url": " wss://gadget.example.com:8443/gadget "})
    _reached(api.dir, "wss://seen.example.com/gadget")
    got = api.remote.get("/api/plugins/gadget/info").json()
    assert (got["url"], got["url_source"]) == ("wss://gadget.example.com:8443/gadget", "config")
    # A public_url that isn't a ws:// or wss:// URL is ignored, not passed to the page.
    _config(monkeypatch, {"public_url": "javascript:alert(1)"})
    assert api.remote.get("/api/plugins/gadget/info").json()["url_source"] == "seen"


@pytest.mark.parametrize("headers, extra, url", [
    ({"Host": "hermes.lan:9119"}, {}, "ws://hermes.lan:8765/gadget"),
    ({"Host": "hermes.example.com", "X-Forwarded-Proto": "https"}, {}, "wss://hermes.example.com:8765/gadget"),
    ({"Host": "internal:9119", "Forwarded": 'for=1.2.3.4;proto=https;host="hermes.example.com"'}, {},
     "wss://hermes.example.com:8765/gadget"),
    ({"Host": "10.0.0.5:9119"}, {"port": 9000, "path": "/dev", "tls_cert": "/c.pem"}, "wss://10.0.0.5:9000/dev"),
])
def test_info_with_nothing_seen_guesses_from_the_dashboards_address(api, monkeypatch, headers, extra, url):
    _config(monkeypatch, extra)
    got = api.remote.get("/api/plugins/gadget/info", headers=headers).json()
    assert (got["url"], got["url_source"]) == (url, "dashboard")


def test_info_ignores_a_malformed_seen_url(api, monkeypatch):
    _config(monkeypatch, {})
    live_status.write(api.dir, {}, reached=[{"url": "wss://evil.example.com/gadget?x=<script>", "at": 5},
                                           {"url": "https://x/gadget", "at": 6}, "junk"])
    got = api.remote.get("/api/plugins/gadget/info", headers={"Host": "hermes.lan"}).json()
    assert (got["url"], got["url_source"]) == ("ws://hermes.lan:8765/gadget", "dashboard")


# -- where a gadget reached the gateway ----------------------------------------------------------


@pytest.mark.parametrize("headers, path, tls, url", [
    ({"Host": "gadget.home.example.com"}, "/gadget", False, "ws://gadget.home.example.com/gadget"),
    ({"Host": "192.168.1.20:8765"}, "/gadget?x=1", False, "ws://192.168.1.20:8765/gadget"),
    ({"Host": "hermes.lan:8765"}, "/gadget", True, "wss://hermes.lan:8765/gadget"),
    # Traefik, Caddy, nginx: the scheme, and maybe the host and port, the client used.
    ({"Host": "gadget.home.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "gadget.home.example.com",
      "X-Forwarded-Port": "443"}, "/gadget", False, "wss://gadget.home.example.com/gadget"),
    ({"Host": "10.0.0.2:8765", "X-Forwarded-Proto": "https,http", "X-Forwarded-Host": "Gadget.Example.com, inner"},
     "/gadget", False, "wss://gadget.example.com/gadget"),
    ({"Host": "10.0.0.2:8765", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "gadget.example.com",
      "X-Forwarded-Port": "8443"}, "/gadget", False, "wss://gadget.example.com:8443/gadget"),
    ({"Host": "10.0.0.2:8765", "Forwarded": 'for=192.0.2.60;proto=https;host="gadget.example.com:8443", for=10.0.0.1'},
     "/gadget", False, "wss://gadget.example.com:8443/gadget"),
    ({"Host": "[2001:db8::5]:8765"}, "/gadget", False, "ws://[2001:db8::5]:8765/gadget"),
    ({"Host": "gadget.example.com:80"}, "/gadget", False, "ws://gadget.example.com/gadget"),
])
def test_the_url_a_device_used_comes_from_its_request(headers, path, tls, url):
    from hermes_gadget_plugin import public_url

    assert public_url.from_headers(headers, path, tls=tls) == url


@pytest.mark.parametrize("headers", [
    {},
    {"Host": "127.0.0.1:8765"},                  # loopback: no use to another computer
    {"Host": "localhost:8765"},
    {"Host": "[::1]:8765"},
    {"Host": "evil.com/x?y=1"},                  # never more than a host and a port
    {"Host": "a b"},
    {"Host": "user@evil.com"},
    {"Host": "evil.com:99999"},
    {"Host": "[not-ipv6]:8765"},
    {"Host": "gadget.example.com", "X-Forwarded-Host": "<script>"},
])
def test_a_request_that_doesnt_say_gives_no_url(headers):
    from hermes_gadget_plugin import public_url

    assert public_url.from_headers(headers, "/gadget") is None


def test_stored_urls_are_checked_when_read_back():
    from hermes_gadget_plugin import public_url

    assert public_url.valid("wss://gadget.example.com/gadget") == "wss://gadget.example.com/gadget"
    assert public_url.valid("wss://gadget.example.com:443/gadget") == "wss://gadget.example.com/gadget"
    for bad in (None, 5, "", "https://x/gadget", "wss://x/gadget?q", "wss://x", "wss://u@x/g", "wss://x/" + "a" * 400):
        assert public_url.valid(bad) is None, bad
    assert public_url.latest([{"url": "ws://a.lan/g", "at": 1}, {"url": "ws://b.lan/g", "at": 3},
                              {"url": "bad", "at": 9}, {"url": "ws://c.lan/g", "at": 10 ** 400}, "x"]) == "ws://b.lan/g"
    assert public_url.latest([]) is None


class _Request:
    def __init__(self, headers, path="/gadget"):
        self.headers, self.path = headers, path


def test_the_hub_remembers_only_where_paired_devices_connected():
    from hermes_gadget_plugin import hub as hubmod
    from hermes_gadget_plugin.hub import DeviceHub, DeviceSession

    hub = DeviceHub(store=None, delegate=None)

    def session(host, device=DEVICE, paired=True):
        ws = _Wire()
        ws.request = _Request({"Host": host, "X-Forwarded-Proto": "https"})
        s = DeviceSession(hub, ws, {"device_id": device})
        s.reached_url, s.paired = hub.reached_by(ws), paired
        return s

    hub.note_reached(session("stranger.example.com", paired=False))  # authenticated, not approved
    assert hub.reached_urls() == []
    hub.note_reached(session("gadget.example.com"))
    hub.note_reached(session("127.0.0.1:8765"))  # loopback: nothing to remember
    assert [r["url"] for r in hub.reached_urls()] == ["wss://gadget.example.com/gadget"]
    for i in range(hubmod.MAX_REACHED + 2):
        hub.note_reached(session(f"g{i}.example.com"))
    hub.note_reached(session("gadget.example.com"))
    urls = [r["url"] for r in hub.reached_urls()]
    assert len(urls) == hubmod.MAX_REACHED and urls[0] == "wss://gadget.example.com/gadget"
    assert hub.reached_by(_Wire()) is None  # a connection with no request to read


def test_approving_a_waiting_device_remembers_where_it_connected(tmp_path):
    from hermes_gadget_plugin.hub import DeviceHub, DeviceSession

    hub = DeviceHub(store=_enroll(tmp_path, paired=False), delegate=None)
    ws = _Wire()
    ws.request = _Request({"Host": "gadget.example.com:8765"})
    s = DeviceSession(hub, ws, {"device_id": DEVICE})
    s.reached_url = hub.reached_by(ws)
    hub.note_reached(s)
    assert hub.reached_urls() == []
    asyncio.run(s.set_paired(True))
    assert [r["url"] for r in hub.reached_urls()] == ["ws://gadget.example.com:8765/gadget"]


def test_status_keeps_where_devices_connected_even_once_stale(tmp_path):
    live_status.write(tmp_path, {}, now=1000.0, reached=[{"url": "wss://g.example.com/gadget", "at": 999.0}])
    got = live_status.read(tmp_path, now=1000.0 + live_status.STALE_AFTER_S + 60)
    assert got["running"] is False and got["reached"] == [{"url": "wss://g.example.com/gadget", "at": 999.0}]
    (tmp_path / live_status.FILENAME).write_text('{"running": true, "updated_at": 1, "devices": {}, "reached": {"x": 1}}')
    assert live_status.read(tmp_path)["reached"] == []

# -- what the screen shows ---------------------------------------------------------------------


class _Wire:
    remote_address = ("127.0.0.1", 1)

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_a_turn_moves_the_mirror_through_thinking_to_ready():
    from hermes_gadget_plugin.hub import DeviceSession

    hub = types.SimpleNamespace(_held={})
    session = DeviceSession(hub, _Wire(), {"device_id": DEVICE})
    assert session.activity["state"] == "ready"

    async def run():
        await session.send_transcript("what time is it")
        await session.turn_start("t1")
        assert session.activity["state"] == "thinking" and session.activity["heard"] == "what time is it"
        await session.send_status("Checking the clock")
        await session.send_delta("It's")
        await session.send_reply("It's ten past " + "x" * 400)
        await session.send_reply("ignored", interim=True)
        await session.turn_end("t1")

    asyncio.run(run())
    activity = session.activity
    assert activity["state"] == "ready" and activity["status"] == ""
    assert activity["reply"].startswith("It's ten past") and len(activity["reply"]) == 280
    assert isinstance(activity["at"], float)
