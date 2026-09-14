"""Cockpit write routes: loopback and token guards, argument lists for gc, and read views."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pangenome_town import dashboard


@pytest.fixture
def cockpit(towns):
    state = dashboard.DashboardState([towns["ubar"], towns["yamatai"]])
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "id": "ub-1"}\n', stderr="")

    state.runner = runner
    state.supervisor = lambda path, timeout=5.0: {"items": [{"id": "m2", "from": "sam", "to": "human", "subject": "hi", "body": "b", "created_at": "2026-09-14T10:00:00Z", "read": False},
                                                          {"id": "m1", "from": "human", "to": "sam", "subject": "yo", "body": "a", "created_at": "2026-09-14T09:00:00Z", "read": True}]} if "/mail" in path else {"items": []}
    state.agentsview_url = "http://127.0.0.1:1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}", calls
    server.shutdown()


def request(url, *, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null") if "json" in response.headers.get("Content-Type", "") else response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_page_carries_token_and_post_requires_it(cockpit):
    state, base, calls = cockpit
    status, page = request(base + "/")
    assert status == 200 and state.token in page and "__COCKPIT_TOKEN__" not in page
    body = {"town": "ubar", "to": "sam", "subject": "s", "body": "hello"}
    assert request(base + "/api/mail/send", body=body)[0] == 403
    assert request(base + "/api/mail/send", body=body, headers={"X-Cockpit-Token": "wrong"})[0] == 403
    assert not calls


def test_foreign_host_or_origin_is_refused(cockpit):
    state, base, _ = cockpit
    assert request(base + "/api/towns", headers={"Host": "evil.example"})[0] == 403
    assert request(base + "/api/mail/send", body={"town": "ubar"}, headers={"X-Cockpit-Token": state.token, "Origin": "https://evil.example"})[0] == 403


def test_mail_send_reply_mark_build_argument_lists(cockpit):
    state, base, calls = cockpit
    token = {"X-Cockpit-Token": state.token}
    status, result = request(base + "/api/mail/send", body={"town": "ubar", "to": "sam", "subject": "Question", "body": "--city /etc; rm -rf /", "notify": True}, headers=token)
    assert status == 200 and result["ok"]
    argv = calls[-1]
    assert argv[1:3] == ["mail", "send"] and argv[argv.index("--city") + 1] == str(state.towns["ubar"].city_root)
    assert argv[argv.index("-m") + 1] == "--city /etc; rm -rf /" and "--notify" in argv
    request(base + "/api/mail/reply", body={"town": "ubar", "id": "m2", "body": "thanks"}, headers=token)
    assert calls[-1][1:3] == ["mail", "reply"] and calls[-1][-2:] == ["--", "m2"]
    request(base + "/api/mail/mark", body={"town": "ubar", "id": "m1", "unread": True}, headers=token)
    assert calls[-1][1:3] == ["mail", "mark-unread"]


def test_session_actions_keep_messages_positional(cockpit):
    state, base, calls = cockpit
    token = {"X-Cockpit-Token": state.token}
    request(base + "/api/sessions/new", body={"town": "yamatai", "template": "bob", "alias": "bob"}, headers=token)
    assert calls[-1][1:3] == ["session", "new"] and "--no-attach" in calls[-1] and calls[-1][-1] == "bob"
    request(base + "/api/sessions/nudge", body={"town": "yamatai", "session": "ym-1", "message": "--delivery immediate check mail"}, headers=token)
    assert calls[-1][-3:] == ["--", "ym-1", "--delivery immediate check mail"]
    status, _ = request(base + "/api/sessions/wake", body={"town": "yamatai", "session": "bad name; ls"}, headers=token)
    assert status == 400
    status, _ = request(base + "/api/mail/send", body={"town": "atlantis", "to": "x", "subject": "s", "body": "b"}, headers=token)
    assert status == 400


def test_read_views(cockpit):
    state, base, _ = cockpit
    status, mail = request(base + "/api/mail?town=ubar")
    assert status == 200 and [item["id"] for item in mail["messages"]] == ["m2", "m1"]
    status, monitor = request(base + "/api/monitor")
    assert status == 200 and {node["id"] for node in monitor["nodes"]} == {"ubar", "yamatai"} and "messages" in monitor
    status, agents = request(base + "/api/agentsview")
    assert status == 200 and agents["available"] is False
    assert json.dumps(monitor).count(state.token) == 0
