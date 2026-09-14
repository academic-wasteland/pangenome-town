"""Minimal A2A (Agent2Agent) JSON-RPC surface over the RCP pipeline.

Implements `message/send`, `tasks/get`, and `tasks/cancel`. A task arrives as a
`data` part whose `data` is the RCP JSON-LD ResearchTask; the contribution is
returned as a task artifact `data` part. Streaming and push notifications are
not supported (the Agent Card says so).
"""

from __future__ import annotations

import json
from typing import Any

from .pipeline import Node, TaskRecord

TASK_PROFILE = 'application/ld+json;profile="https://w3id.org/research-commons/v0.1/task"'
CONTRIBUTION_PROFILE = 'application/ld+json;profile="https://w3id.org/research-commons/v0.1/contribution"'
REPORT_PROFILE = 'application/json;profile="https://w3id.org/research-commons/v0.1/semantic-validation-report"'
PRESENTATION_PROFILE = 'application/json;profile="https://w3id.org/academic-wasteland/credentials/v0.1/presentation"'

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, UNSUPPORTED_OPERATION, CONTENT_TYPE_NOT_SUPPORTED = -32001, -32004, -32005


def error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        body["error"]["data"] = data
    return body


def result(request_id: Any, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def task_view(record: TaskRecord) -> dict[str, Any]:
    artifacts = []
    if record.contribution is not None:
        artifacts.append({"artifactId": f"{record.id}#contribution", "name": "contribution.jsonld",
                          "parts": [{"kind": "data", "data": record.contribution, "metadata": {"mediaType": CONTRIBUTION_PROFILE}}]})
    if record.report is not None:
        artifacts.append({"artifactId": f"{record.id}#report", "name": "semantic-validation-report.json",
                          "parts": [{"kind": "data", "data": record.report, "metadata": {"mediaType": REPORT_PROFILE}}]})
    for artifact in record.artifacts:
        if artifact.get("name") == "contribution.jsonld":
            continue
        artifacts.append({"artifactId": f"{record.id}#{artifact['name']}", "name": artifact["name"],
                          "parts": [{"kind": "file", "file": {"name": artifact["name"], "mimeType": artifact.get("mediaType", "application/json"), "uri": artifact["path"]}}]})
    status: dict[str, Any] = {"state": record.state}
    if record.message:
        status["message"] = {"role": "agent", "messageId": f"{record.id}#status", "parts": [{"kind": "text", "text": record.message}]}
    return {"id": record.id, "contextId": record.context_id or record.id, "kind": "task", "status": status,
            "artifacts": artifacts,
            "metadata": {"verdict": record.verdict, "gates": getattr(record, "gates", None), "refusal": getattr(record, "refusal", None),
                         "authority": getattr(record, "authority", None), "sites": getattr(record, "sites", None)}}


def _is_presentation(part: dict[str, Any], data: dict[str, Any]) -> bool:
    media = (part.get("metadata") or {}).get("mediaType") if isinstance(part.get("metadata"), dict) else None
    return media == PRESENTATION_PROFILE or data.get("type") == "Presentation"


def _message_parts(params: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Return (task, presentation, problem): the first task data part and at most one credential presentation."""
    message = params.get("message")
    if not isinstance(message, dict):
        return None, None, "params.message is required"
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        return None, None, "params.message.parts must be a non-empty list"
    task: dict[str, Any] | None = None
    presentations: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        data: Any = None
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            data = part["data"]
        elif part.get("kind") == "text" and isinstance(part.get("text"), str):
            try:
                data = json.loads(part["text"])
            except json.JSONDecodeError:
                continue
        if not isinstance(data, dict):
            continue
        if _is_presentation(part, data):
            presentations.append(data)
        elif task is None:
            task = data
    if task is None:
        return None, None, "no data part carrying a Research Commons task was found"
    if len(presentations) > 1:
        return None, None, "at most one credential presentation part is allowed"
    return task, (presentations[0] if presentations else None), None


def handle(node: Node, request: Any, *, requester_hint: str | None = None) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "method" not in request:
        return error(request.get("id") if isinstance(request, dict) else None, INVALID_REQUEST, "invalid JSON-RPC 2.0 request")
    request_id = request.get("id")
    method = request["method"]
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if method == "message/send":
        document, presentation, problem = _message_parts(params)
        if problem:
            return error(request_id, INVALID_PARAMS, problem)
        context_id = params["message"].get("contextId") if isinstance(params.get("message"), dict) else None
        hint = requester_hint or (params["message"].get("metadata") or {}).get("town") if isinstance(params.get("message"), dict) else requester_hint
        record = node.submit(document, context_id=context_id, requester_hint=hint, presentation=presentation)
        return result(request_id, task_view(record))
    if method == "tasks/get":
        task_id = params.get("id")
        record = node.get(task_id) if isinstance(task_id, str) else None
        if record is None:
            return error(request_id, TASK_NOT_FOUND, "task not found")
        return result(request_id, task_view(record))
    if method == "tasks/cancel":
        task_id = params.get("id")
        record = node.cancel(task_id) if isinstance(task_id, str) else None
        if record is None:
            return error(request_id, TASK_NOT_FOUND, "task not found")
        return result(request_id, task_view(record))
    if method in {"message/stream", "tasks/resubscribe", "tasks/pushNotificationConfig/set"}:
        return error(request_id, UNSUPPORTED_OPERATION, f"{method} is not supported by this node")
    return error(request_id, METHOD_NOT_FOUND, f"unknown method {method}")
