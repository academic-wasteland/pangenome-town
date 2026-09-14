"""Deliver envelopes into a city's mail (gc mail) so the town's agent sees them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
