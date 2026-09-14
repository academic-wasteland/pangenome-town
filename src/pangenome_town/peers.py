"""Send envelopes to peer towns through the Gas City supervisor's service proxy."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
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


def town_info(town: TownConfig, peer: str) -> dict[str, Any]:
    url = envoy_url(town, town.peer_city(peer), "/v0/town")
    request = urllib.request.Request(url, headers={"X-GC-Request": "pangenome-town", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PeerError(f"could not reach peer {peer} at {url}: {error}") from error
