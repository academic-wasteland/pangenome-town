"""Send envelopes to peer towns through the Gas City supervisor's service proxy."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import TownConfig
from .exchange import Envelope, ExchangeLog, canonical

SERVICE_NAME = "envoy"
TIMEOUT_SECONDS = 30


class PeerError(RuntimeError):
    pass


def envoy_url(town: TownConfig, peer_city: str, path: str = "/v0/messages") -> str:
    return f"{town.supervisor_url.rstrip('/')}/v0/city/{peer_city}/svc/{SERVICE_NAME}{path}"


def send(town: TownConfig, envelope: Envelope, log: ExchangeLog | None = None) -> dict[str, Any]:
    if envelope.visibility == "public":
        if not (town.extra.get("federation") or {}).get("state"):
            raise PeerError("Public messages require a configured wasteland relay.")
        return _send_federated(town, envelope, log)
    if envelope.recipient not in town.peers and (town.extra.get("federation") or {}).get("state"):
        return _send_federated(town, envelope, log)
    peer_city = town.peer_city(envelope.recipient)
    url = envoy_url(town, peer_city)
    data = canonical(envelope.to_dict()).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-GC-Request": "pangenome-town", "Accept": "application/json"},
    )
    if log is not None:
        log.record(envelope, town=town.name, direction="sent", status="sending")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            status_code = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        if log is not None:
            log.set_status(envelope.id, "failed")
            log.event(town.name, "send_failed", envelope.id, {"http_status": error.code, "body": body[:2000], "url": url})
        raise PeerError(f"peer {envelope.recipient} rejected the envelope ({error.code}): {body[:500]}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        if log is not None:
            log.set_status(envelope.id, "failed")
            log.event(town.name, "send_failed", envelope.id, {"error": str(error), "url": url})
        raise PeerError(f"could not reach peer {envelope.recipient} at {url}: {error}") from error
    if log is not None:
        log.set_status(envelope.id, "sent")
        log.event(town.name, "sent_ok", envelope.id, {"http_status": status_code, "url": url, "response": payload})
    return {"http_status": status_code, "url": url, "response": payload}


def _send_federated(town: TownConfig, envelope: Envelope, log: ExchangeLog | None) -> dict[str, Any]:
    """Unknown local peers may be independently hosted towns in an explicitly configured relay.

    Never publish local attachment paths or forward a bearer token across a redirect.
    """
    if envelope.attachments:
        raise PeerError("federation relay accepts inline messages only; do not send local file attachments")
    directory = Path(town.extra["federation"]["state"]).expanduser()
    if not directory.is_absolute():
        directory = town.city_root / directory
    settings = json.loads((directory / "town.json").read_text())
    if settings["name"] != town.name or not settings["hub"].startswith("https://"):
        raise PeerError("federation credentials must match this town and use an HTTPS relay")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    url = settings["hub"].rstrip("/") + "/v1/messages"
    request = urllib.request.Request(url, data=canonical(envelope.to_dict()).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings["token"]})
    if log is not None:
        log.record(envelope, town=town.name, direction="sent", status="sending")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as response:
            result = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as error:
        if log is not None:
            log.set_status(envelope.id, "failed")
        raise PeerError(f"federation relay send failed: {type(error).__name__}") from error
    if log is not None:
        log.set_status(envelope.id, "sent")
        log.event(town.name, "sent_ok", envelope.id, {"transport": "federation", "url": url})
    return {"http_status": 200, "url": url, "response": result}


def town_info(town: TownConfig, peer: str) -> dict[str, Any]:
    if peer not in town.peers and (town.extra.get("federation") or {}).get("state"):
        directory = Path(town.extra['federation']['state']).expanduser()
        if not directory.is_absolute():
            directory = town.city_root / directory
        settings = json.loads((directory / 'town.json').read_text())
        hub = settings['hub'].rstrip('/')
        if not hub.startswith('https://'):
            raise PeerError('external discovery requires an HTTPS relay')
        request = urllib.request.Request(hub + '/.well-known/wasteland.json', headers={'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                entries = json.load(response).get('towns', [])
        except (OSError, ValueError) as error:
            raise PeerError(f'could not discover external town {peer}') from error
        for item in entries:
            if item.get('name') == peer:
                return {**item, 'transport': 'federation', 'relay': hub,
                        'discovery': 'Advertised capabilities; use describe to request inputs and access conditions',
                        'contact': {'town': peer, 'operation': 'describe',
                                    'agents': [c.removeprefix('resident:') for c in item.get('capabilities', [])
                                               if c.startswith('resident:')]}}
        raise PeerError(f'town {peer} is not advertised by this relay')
    url = envoy_url(town, town.peer_city(peer), "/v0/town")
    request = urllib.request.Request(url, headers={"X-GC-Request": "pangenome-town", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PeerError(f"could not reach peer {peer} at {url}: {error}") from error
