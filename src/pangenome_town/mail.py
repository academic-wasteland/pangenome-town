"""Deliver envelopes into a city's mail (gc mail) so the town's agent sees them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import TownConfig
from .exchange import Envelope

SUBJECT_PREFIX = "peer:"


class MailError(RuntimeError):
    pass


def gc_binary() -> str:
    path = os.environ.get("PT_GC_BIN") or shutil.which("gc")
    if not path:
        raise MailError("gc binary not found on PATH (set PT_GC_BIN)")
    return path


def subject_for(envelope: Envelope) -> str:
    return f"{SUBJECT_PREFIX}{envelope.sender}:{envelope.kind}:{envelope.id}"


def body_for(envelope: Envelope) -> str:
    lines = [
        f"Peer message from town '{envelope.sender}' (kind: {envelope.kind}).",
        f"Message id: {envelope.id}",
    ]
    if envelope.in_reply_to:
        lines.append(f"In reply to: {envelope.in_reply_to}")
    region = envelope.body.get("region")
    if region:
        lines.append(f"Region: {region}")
    lines.append("")
    lines.append(envelope.text or "(no text)")
    if envelope.attachments:
        lines.append("")
        lines.append("Attachments:")
        for attachment in envelope.attachments:
            lines.append(f"  - {attachment.name} {attachment.sha256} {attachment.path or ''}".rstrip())
    lines.append("")
    lines.append(f"Inspect with: pangenome-town messages --id {envelope.id}")
    if envelope.kind == "question":
        lines.append(f"Answer with:  pangenome-town answer --message {envelope.id} --kind <summary|haplotypes|variants|subgraph> --region <assembly:chrom:start-end> --text \"...\"")
    return "\n".join(lines)


def send(town: TownConfig, envelope: Envelope, *, notify: bool = True, dry_run: bool = False) -> dict[str, Any]:
    command = [
        gc_binary(), "mail", "send", "--city", str(town.city_root), "--from", "human",
        "--to", town.mail_recipient, "-s", subject_for(envelope), "-m", body_for(envelope), "--json",
    ]
    if notify:
        command.append("--notify")
    if dry_run:
        return {"dry_run": True, "command": command}
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise MailError(f"gc mail send failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    payload: dict[str, Any] = {"stdout": result.stdout.strip()}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload.update(json.loads(line))
            except json.JSONDecodeError:
                pass
    return payload


def send_to_resident(town: TownConfig, envelope: Envelope, resident: str) -> dict[str, Any]:
    """Deliver named-resident mail and require a real Gas City receipt (gc also names a Graphviz tool)."""
    body = body_for(envelope)
    if resident in {'q', 'bloodninja'}:
        body = '\n'.join(line for line in body.splitlines() if not line.startswith('Answer with:'))
        body += (f'\nReply with: pangenome-town send --to {envelope.sender} --reply-to {envelope.id}'
                 ' --text "your answer"\nThe gc mail ID is only the local delivery wrapper.\n')
    result = subprocess.run([gc_binary(), 'mail', 'send', '--city', str(town.city_root), '--from', 'human',
                             '--to', resident, '-s', subject_for(envelope), '-m', body, '--notify', '--json'],
                            capture_output=True, text=True, timeout=60, check=False)
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise MailError('Gas City did not return a JSON delivery receipt; check PT_GC_BIN') from None
    if result.returncode or not isinstance(receipt, dict) or receipt.get('ok') is not True or not receipt.get('id'):
        raise MailError('Gas City did not confirm resident delivery')
    if resident in {"q", "bloodninja"}:
        # ACP connections belong to the supervisor process. A standalone gc
        # notification can queue mail without waking an otherwise idle agent.
        url = (town.supervisor_url.rstrip("/") + "/v0/city/"
               + urllib.parse.quote(town.name, safe="") + "/session/"
               + urllib.parse.quote(resident, safe="") + "/submit")
        request = urllib.request.Request(url, data=json.dumps({
            "message": "You have new mail. Run gc mail check, read the unread message, and reply using its instructions.",
            "intent": "default",
        }).encode(), headers={"Content-Type": "application/json", "X-GC-Request": "resident-mail"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                receipt["wake_requested"] = response.status == 202
        except (urllib.error.URLError, OSError, TimeoutError):
            # Mail is already durable. Do not ask the bridge to redeliver it.
            receipt["wake_requested"] = False
    return receipt
