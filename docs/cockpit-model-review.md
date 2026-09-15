# Cockpit and execution model review

14 September 2026. Scope: the local pangenome-town implementation and the
configured Ubar, Yamatai and Camelot residents. This is an implementation review,
not a benchmark of the configured language models.

## What changed

The cockpit opens on a task watchboard: filter by state, search town/task/region/ID,
select a request, inspect its recorded route, gate decisions, answers and recent
events, and open the underlying evidence without leaving the view. Other views
keep the network, mail/residents, governance and system information accessible.
Pause freezes the watchboard while work continues. The snapshot timestamp and SSE
connection state are separate; an unavailable snapshot is reported explicitly.

The monitor now reads the newest events, rather than the oldest 1,000. Task event
trails are fetched independently so a busy town cannot evict another task's entire
history from the selected view. SSE reconnects respect Last-Event-ID, and startup
begins after the snapshot cursor instead of replaying the database into the UI.

Submission is persisted and mirrored before validation. Inline execution emits a
start event. Compute emits one after site selection and workflow validation,
immediately before invoking its driver, carrying the selected site, driver and
scheduler. These are driver handoffs, not proof that a Slurm job has begun running.
The dashboard distinguishes recorded queue messages from execution, retains the
latest explanation, and exposes task document and context identifiers.

Existing records are not rewritten. Old tasks may lack execution details. A site
configured in town.toml is not an assignment; nearby agent transcripts are not
proof of task ownership. Counts cover the bounded snapshot (400 messages and 300
events), not all historical tasks. Each selected trail contains up to 80 events.

## Recommended changes, in priority order

### 1. Make a distributed request reconstructible

Evidence: `rcp/pipeline.py:Node.submit` allocates a receiver task ID distinct from
`document['@id']`. `cli.py:_rcp` records requester-side submission, stamps and
referral events against the document ID; receiver events use the allocated ID.
`dashboard.py:exchange` associates transcripts by directory and time window.
`cli.py` dispatch records carry free text rather than a structured session ID.

Introduce explicit `request_id`, `task_id`, `parent_task_id`, `attempt_id`,
`session_id`, and `job_id` fields on execution events. Preserve the root request ID
across referrals; model a retry as another attempt and delegation as a child task.
Record the assigning actor, receiving actor and reason for every handoff. Shared
A2A context identifies related work but must not be interpreted as parenthood.

Acceptance: one request sent through two towns with two concurrent workers and a
retry produces an unambiguous branching route. Every displayed edge links to its
event; unrelated work in the same directory cannot become part of that route.
Keep compatibility by making the new fields optional for old records.

### 2. Separate execution phase from the A2A status

Evidence: `_execute` uses `working` both for queued work and actual execution.
`Node.cancel` updates the status file without canceling a driver or mirroring an
event. `resume` temporarily mutates node-wide dispatch/spec fields. There is no
explicit per-task execution claim preventing two callers from resuming it.
The new cockpit projection disambiguates existing queue explanations, but that
text should not be the long-term state contract.

Add a local phase (`validating`, `queued`, `assigned`, `scheduler-pending`,
`running`, `validating-output`, `completed`) while preserving compatible A2A
states. Give each attempt an atomic execution claim and heartbeat. Record
`cancel-requested` separately; declare cancellation complete only after the
worker/driver acknowledges it. Use request-local execution options instead of
mutating shared Node fields, and publish state changes atomically.

Acceptance: duplicate resume executes once; cancellation cannot silently be
overwritten by completion; interruption can be distinguished from an idle queue;
Slurm pending time is not reported as running time. Test these races before
adding dashboard cancellation controls.

### 3. Keep conformance, permission and scientific reliability distinct

The four-gate design is a useful boundary: semantics determines admissibility;
verified credentials support authority; standing allocates scarce resources;
workflow validation and drivers enforce capability. Preserve that separation.

Evidence: `cli.py:_stamp_peer_result` validates structure and output digests and
uses the peer's reported validation status for a stamp. `rcp/commons.py` turns
stamps into standing; `rcp/reputation.py` uses standing to establish compute
eligibility. A conforming, correctly hashed artifact need not contain a correct
scientific answer. README already acknowledges missing requester-side semantic
revalidation and independent federation.

Represent conformance, delivery reliability and independently checked scientific
results as distinct evidence. Independently validate contributions against the
pinned peer contract, then separately record replication results. Bound the
influence of repeated stamps by the same source, retain provenance for the
stamper, and test collusion/bootstrap scenarios before treating a single score as
an allocation policy. Show which evidence justified eligibility in the cockpit.

Acceptance: a structurally valid but scientifically incorrect result does not
automatically earn an independent-validation claim. Repeated transactions cannot
masquerade as many independent endorsers. No reputation change bypasses authority.

### 4. Evaluate model roles before replacing model names

The residents endpoint reports townsfolk using “GLM 5.3 Flash via OpenRouter” and
local residents/rigger/authority advisers using “Qwen3.8 27B on unimatrix01 (local
vLLM)”. These are configuration labels, not verified capability or pricing claims.
No model performance comparison was run and no model settings were changed.

Keep deterministic graph queries, credential verification, semantic reasoning,
workflow validation and credential issuance decisions outside discretionary LLM
judgment. Use language models for interpreting requests, planning within a
validated template set, explaining results and making advisory recommendations.
Record actual provider/model IDs with each execution attempt, not just a display
label on a resident card.

Build a small held-out suite from real tasks: region interpretation, valid query
planning, out-of-scope credentials, missing compute, referral choice, tool failure
recovery, and a result containing misleading instructions. Compare completion
correctness, unsupported claims, invalid tool calls, wall time, tokens/cost and
human intervention by role. Route failures to an explicitly configured reviewer
or human only after measuring that escalation's benefit; avoid unmeasured model
swaps based on parameter counts or product names.

Acceptance: a proposed model/router change beats the existing role-specific
baseline on the agreed quality threshold and reports its cost/latency tradeoff.

## Validation and limits

Python tests cover newest-event retention after more than 1,000 entries, queued
versus executing projection, reply completion, and visibility before processing
and during execution. Chromium checks exercise the live local dashboard, search,
empty results, evidence open/close, navigation, pause/resume and mobile overflow.
Existing controlled-query and compute tests exercise the added execution hook.
No research task, credential decision, mail or remote compute job was initiated
for the UI checks. Lifecycle instrumentation is used by newly started processes;
already running envoys must reload the package to emit the new events.
