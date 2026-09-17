"""GA4GH Task Execution Service (TES) v1.1 schema mapping for RCP tasks."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from ..exchange import canonical


def _extract_inputs(
    rcp_task: dict[str, Any],
    dataset_storage_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract input file URIs from an RCP ResearchTask and map to TES inputs.

    Resolves datasets using an explicit dataset_storage_map or downloadUrl/url.
    Does not treat arbitrary semantic IRIs (@id) as download locations unless mapped.
    """
    inputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    storage_map = dataset_storage_map or {}

    def add_input(url: str, path: str | None = None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        # Determine container path: /container/input/<filename>
        basename = os.path.basename(url.split("?")[0].split("#")[0]) or "input_file"
        target_path = path or f"/container/input/{basename}"
        inputs.append({
            "url": url,
            "path": target_path,
        })

    # 1. usesDataset can contain entities with url, downloadUrl, or mapped storage
    datasets = rcp_task.get("usesDataset")
    if isinstance(datasets, list):
        for ds in datasets:
            if isinstance(ds, dict):
                # Check for explicit url/downloadUrl or mapped storage
                ds_id = ds.get("@id")
                url = (
                    ds.get("url")
                    or ds.get("downloadUrl")
                    or (storage_map.get(ds_id) if ds_id else None)
                )
                path = ds.get("path")
                if isinstance(url, str) and url.startswith(("http://", "https://", "s3://", "file://")):
                    add_input(url, path)
            elif isinstance(ds, str):
                url = storage_map.get(ds) or (ds if ds.startswith(("http://", "https://", "s3://", "file://")) else None)
                if url:
                    add_input(url)

    # 2. Check explicit "inputs" in rcp_task if present
    explicit_inputs = rcp_task.get("inputs")
    if isinstance(explicit_inputs, list):
        for item in explicit_inputs:
            if isinstance(item, dict):
                url = item.get("url") or item.get("downloadUrl")
                target = item.get("path")
                if not url and target and dataset_storage_map:
                    url = dataset_storage_map.get(target)
                if url:
                    add_input(url, target)
            elif isinstance(item, str):
                add_input(item)

    return inputs


def _extract_outputs(
    rcp_task: dict[str, Any],
    output_url_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Extract expected outputs from an RCP ResearchTask and map to TES outputs.

    GA4GH TES v1.1 requires both `path` and `url` for output files/directories.
    Requires an explicit destination URL (`output_url_prefix`, task-level `outputBaseUrl`,
    or per-output `url`/`downloadUrl`).
    """
    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()
    base_url = (
        output_url_prefix
        or rcp_task.get("outputBaseUrl")
        or rcp_task.get("output_url_prefix")
    )

    def add_output(path: str, url: str | None = None) -> None:
        if not path or path in seen:
            return
        seen.add(path)
        if not url:
            if base_url:
                filename = os.path.basename(path)
                url = f"{base_url.rstrip('/')}/{filename}"
            else:
                raise ValueError(
                    f"TES output '{path}' requires an explicit remote output destination url, "
                    "output_url_prefix, or outputBaseUrl"
                )
        out: dict[str, Any] = {"path": path, "url": url}
        outputs.append(out)

    # Check "outputs" or "hasOutput" in rcp_task
    task_outputs = rcp_task.get("outputs") or rcp_task.get("hasOutput")
    if isinstance(task_outputs, list):
        for item in task_outputs:
            if isinstance(item, dict):
                path = item.get("path") or f"/container/output/{item.get('name', 'output')}"
                url = item.get("url") or item.get("downloadUrl")
                add_output(path, url)
            elif isinstance(item, str):
                path = item if item.startswith("/") else f"/container/output/{item}"
                add_output(path)

    return outputs


def build_tes_task(
    rcp_task: dict[str, Any],
    executor_image: str,
    command: list[str],
    output_url_prefix: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    dataset_storage_map: dict[str, str] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
    resources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map an RCP ResearchTask JSON-LD document to a GA4GH TES v1.1 task dictionary.

    Extracts inputs, outputs, executor specification, resources, and preserves rcp_id and rcp_digest in tags.
    """
    extracted_inputs = _extract_inputs(rcp_task, dataset_storage_map=dataset_storage_map)
    if inputs:
        existing_urls = {item.get("url") for item in extracted_inputs}
        for item in inputs:
            url = item.get("url")
            existing = next((entry for entry in extracted_inputs if entry.get("url") == url), None)
            if existing is not None:
                existing.update(item)
            else:
                extracted_inputs.append(item)
                existing_urls.add(url)
    extracted_outputs = _extract_outputs(rcp_task, output_url_prefix=output_url_prefix)
    if outputs:
        existing_paths = {item.get("path") for item in extracted_outputs}
        for item in outputs:
            path = item.get("path")
            existing = next((entry for entry in extracted_outputs if entry.get("path") == path), None)
            if existing is not None:
                existing.update(item)
            else:
                extracted_outputs.append(item)
                existing_paths.add(path)

    executor: dict[str, Any] = {
        "image": executor_image,
        "command": list(command),
    }
    if stdout:
        executor["stdout"] = stdout
    if stderr:
        executor["stderr"] = stderr

    executors = [executor]

    rcp_id = str(rcp_task.get("@id", ""))
    canonical_json = canonical(rcp_task)
    rcp_digest = "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    tags = {
        "rcp_id": rcp_id,
        "rcp_digest": rcp_digest,
        "rcp_source_jsonld": canonical_json,
    }

    # Pass through existing tags if present
    if isinstance(rcp_task.get("tags"), dict):
        for k, v in rcp_task["tags"].items():
            if k not in tags:
                tags[k] = str(v)

    task_payload: dict[str, Any] = {
        "name": rcp_task.get("name") or rcp_id or "rcp-task",
        "description": rcp_task.get("description", ""),
        "inputs": extracted_inputs,
        "outputs": extracted_outputs,
        "executors": executors,
        "tags": tags,
    }

    if resources:
        tes_res: dict[str, Any] = {}
        if "cpus" in resources:
            tes_res["cpuCores"] = int(resources["cpus"])
        if "mem_gb" in resources:
            tes_res["ramGb"] = float(resources["mem_gb"])
        if tes_res:
            task_payload["resources"] = tes_res

    return task_payload
