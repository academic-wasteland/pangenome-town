You are an expert developer. Please implement the following four sequential issues for the pangenome-town codebase. Follow the exact Acceptance Criteria and respect the ADR 0001 semantic gating rules.

# Issue 1: Enforce Semantic Gating (ADR 0001) for Compute Dispatch
Target Files: src/pangenome_town/compute/delegation.py, src/pangenome_town/compute/runner.py, tests/test_compute.py
Context: Per ADR 0001, execution belongs strictly to the Policy/state layer and must be gated by SROIQ reasoning. The `research_commons.km` module acts as the reference checker. The protocol defines a strict five-valued semantic validation result: `entailed`, `contradicted`, `unknown`, `invalid`, or `indeterminate`. Only `entailed` under a locally trusted manifest satisfies an execution gate. Open-world absence (`unknown`) is explicitly never permission.
Tasks:
1. In `delegation.py` (or `runner.py`), implement a `SemanticGate` class or decorator that takes an RCP `ResearchTask` JSON-LD payload.
2. Invoke the local SROIQ reasoner via the `research_commons.km` wrapper against the town's locally trusted contract manifest.
3. Evaluate the return value. If it is exactly "entailed", allow the execution dispatch to proceed.
4. If the value is "contradicted", "unknown", "invalid", or "indeterminate", raise a `SemanticPolicyError` and immediately halt the execution chain.
5. In `test_compute.py`, add a parametrized unit test covering all five enum values. Assert that four of them correctly raise `SemanticPolicyError` and only `entailed` passes.

# Issue 2: Map RCP `ResearchTask` to GA4GH TES v1.1 Schema
Target Files: src/pangenome_town/compute/tes_schema.py (New File), tests/test_compute.py
Context: We are targeting the standard GA4GH Task Execution Service (TES) v1.1 API. A TES payload requires `inputs`, `outputs`, and `executors` (with `image` and `command`). To comply with ADR 0002, the orchestrator/ledger must carry the RCP message but does not define it. The JSON-LD document and its canonical digest must be preserved verbatim. We will pass the task ID and digest in the TES payload tags to ensure recoverability.
Tasks:
1. Create a new module `tes_schema.py`.
2. Implement a pure function: `def build_tes_task(rcp_task: dict, executor_image: str, command: list[str]) -> dict:`
3. The function must parse the JSON-LD `ResearchTask`, extract input file URIs, and map them to the TES `inputs` array (format: `{"url": "...", "path": "/container/input/..."}`).
4. Map the expected outputs to the TES `outputs` array.
5. Populate the `executors` array with a single executor block using the provided `image` and `command`.
6. Embed the `rcp_task["@id"]` and the calculated SHA-256 canonical digest of the JSON-LD into the TES `tags` dictionary (e.g., `{"rcp_id": "...", "rcp_digest": "..."}`).
7. Write a unit test verifying the dictionary output structurally matches the expected GA4GH TES `/v1/tasks` JSON payload.

# Issue 3: Implement Async `TESComputeRunner` Client
Target Files: src/pangenome_town/compute/runner.py
Context: The `pangenome-town` package needs to talk to a remote GA4GH TES endpoint instead of running local bash scripts.
Tasks:
1. In `runner.py`, create `class TESComputeRunner(ComputeRunner):`.
2. Implement an `__init__(self, endpoint_url: str, bearer_token: str)` method.
3. Implement `async def dispatch(self, tes_task_payload: dict) -> str:` which sends an HTTP POST to `{endpoint_url}/v1/tasks` with standard Bearer Authorization headers. It must return the returned TES task `id` string.
4. Implement `async def poll_status(self, task_id: str) -> str:` which sends an HTTP GET to `{endpoint_url}/v1/tasks/{task_id}`.
5. Map the returned TES states (`QUEUED`, `INITIALIZING`, `RUNNING`, `COMPLETE`, `SYSTEM_ERROR`, `CANCELED`, `EXECUTOR_ERROR`) into the town's internal `ComputeState` enums.
6. Use `httpx` (or `aiohttp`) for non-blocking HTTP requests. Add standard error handling for HTTP 4xx/5xx responses.

# Issue 4: E2E Integration Testing with the `vg` Toolkit
Target Files: src/pangenome_town/tools/graph.py, tests/test_compute.py
Context: `pangenome-town` relies on the `vg` toolkit for operations like sub-graph extraction. We need an end-to-end integration test that proves the pipeline can map a `vg` graph operation to a TES task, pass the SROIQ semantic gate, and dispatch successfully.
Tasks:
1. In `test_compute.py`, create an async test function `test_vg_chunk_tes_dispatch`.
2. Mock the `research_commons.km` reasoner to strictly return `"entailed"`.
3. Use a mock HTTP library (like `respx` or `aioresponses`) to intercept the POST request to `/v1/tasks` (returning `{"id": "mock-tes-id"}`) and the GET request to `/v1/tasks/mock-tes-id` (returning `{"state": "COMPLETE"}`).
4. Invoke the compute runner using a mock RCP task requesting a `vg` chunk extraction.
5. Assert that the `build_tes_task` output generated an `executors` block using `image: "quay.io/vgteam/vg:latest"` and `command: ["vg", "chunk", ...]`.
6. Assert that the POST request was successfully fired and the final compute state is mapped to Complete.
