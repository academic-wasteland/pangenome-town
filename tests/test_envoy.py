import http.client
import json
import socket
import threading

import pytest

from pangenome_town import envoy
from pangenome_town.exchange import Envelope, ExchangeLog


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


@pytest.fixture
def running_envoy(towns, tmp_path):
    town = towns["yamatai"]
    log = ExchangeLog(towns["db"])
    socket_path = str(tmp_path / "envoy.sock")
    state = envoy.EnvoyState(town, log, deliver=False)
    server = envoy.ThreadingUnixHTTPServer(socket_path, envoy.make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield socket_path, log
    server.shutdown()
    server.server_close()
    log.close()


def _request(socket_path, method, path, body=None):
    connection = UnixHTTPConnection(socket_path)
    payload = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=payload, headers={"Content-Type": "application/json"} if payload else {})
    response = connection.getresponse()
    data = json.loads(response.read().decode())
    connection.close()
    return response.status, data


def test_health_and_town(running_envoy):
    socket_path, _ = running_envoy
    assert _request(socket_path, "GET", "/healthz")[0] == 200
    status, town = _request(socket_path, "GET", "/v0/town")
    assert status == 200 and town["name"] == "yamatai" and town["peers"] == ["ubar"]


def test_receive_question_and_read_back(running_envoy):
    socket_path, log = running_envoy
    envelope = Envelope.new("question", "ubar", "yamatai", {"text": "how many?", "region": "GRCh38:chr1:0-20"})
    status, data = _request(socket_path, "POST", "/v0/messages", envelope.to_dict())
    assert status == 202 and data["id"] == envelope.id
    status, one = _request(socket_path, "GET", f"/v0/messages/{envelope.id}")
    assert status == 200 and one["envelope"]["body"]["text"] == "how many?"
    assert [e["kind"] for e in one["events"]] == ["received"]
    assert [m["id"] for m in log.pending_questions("yamatai")] == [envelope.id]
    status, listing = _request(socket_path, "GET", "/v0/messages?limit=5")
    assert status == 200 and listing["messages"][0]["id"] == envelope.id


def test_rejects_wrong_recipient_and_unknown_peer(running_envoy):
    socket_path, _ = running_envoy
    wrong = Envelope.new("question", "ubar", "elsewhere", {"text": "x"}).to_dict()
    assert _request(socket_path, "POST", "/v0/messages", wrong)[0] == 422
    stranger = Envelope.new("question", "atlantis", "yamatai", {"text": "x"}).to_dict()
    assert _request(socket_path, "POST", "/v0/messages", stranger)[0] == 403
    assert _request(socket_path, "POST", "/v0/messages", {"garbage": True})[0] == 400
