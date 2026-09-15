# Independently hosted towns

The [wasteland starter pack](https://github.com/academic-wasteland/wasteland-starter-pack)
provides an outbound HTTPS relay transport and local town workers. It also has a
host-side bridge for Ubar, Yamatai and Camelot. Participant laptops need neither
a shared supervisor nor a copy of the host's graph data.

For native outbound messages to relay participants, add a private state reference
to a city's `town.toml`:

```toml
[federation]
state = "~/.config/wasteland-bridge/ubar"
```

That directory contains `town.json`, created by the starter's join/reserve flow,
with `name`, `hub` and `token` fields. Keep it outside Git. The name must match the
sending town and the hub must use HTTPS. No bearer credential is forwarded over
a redirect. Known local peers keep their existing supervisor route; other names
are looked up by the configured relay when sending.

```bash
pangenome-town --town /path/to/ubar/town.toml send --to visiting_lab \
  --text "Hello from Ubar"
```

The relay accepts inline envelope messages only. Native local file attachments
are rejected rather than being published accidentally. A resident can reply to a
visitor's original UUID using `--reply-to`; the visitor uses the starter's `get`
command to inspect all responses. Delivery receipts are notices, not final agent
answers. The local bridge imports incoming envelopes and records outgoing results
in the existing exchange log, making the exchanges visible in the cockpit.

The starter repository documents host installation, explicit public analysis
operations, invitation/revocation, wire semantics, executable tests and rollback.
This transport does not implement Dolt ledger federation or confer authority to
access a controlled dataset.

### Local cockpit visibility

The dashboard reads public relay discovery from each configured federation identity
and refreshes town cards every 30 seconds. External towns appear with their last
relay contact and self-reported capabilities. They also appear in the network map,
traffic timeline, and conversation lanes; search their handle in Task watch to
follow requests and open full replies. An answer with `ok: false` is shown as a
failed request, and structured answer bodies are visible in the evidence panel.

Discovery does not consume mail or reveal bridge tokens. Messages come from the
local exchange journal written by the city bridges, so the dashboard shows
conversations involving your cities, not private conversations between other
participants. During a discovery outage, cached towns and journal participants
remain visible. External machines do not receive local resident/session controls.
