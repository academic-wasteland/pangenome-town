# Conversations with people, towns and agents

Open **Conversations** from the cockpit, or visit
http://localhost:8393/conversations on the operator laptop.
A default cockpit uses port 8390; this installation uses 8393.
Starter-pack operators get the same interface at
http://localhost:8394/conversations after starting their dashboard.

Choose a person, sending town, destination town and agent. General contact
introduces services; named agents receive the request in their mailbox.
Search town names, agent roles and advertised capabilities. The FAIRhaven link
provides the fuller service catalogue.

## People and memberships

A person has a generated UUID URN, a display name, and zero or more town
memberships with owner/member/guest roles. Editing a name retains the identifier.
A person can own multiple towns. Robert Hoehndorf is configured as owner of both
Ubar and Yamatai on this laptop. New installations ask you to add a person in the UI;
no identity is inferred from a town or shared under the label “Human”.

These are **local operator profiles**, not authenticated user accounts. The
loopback-only cockpit and its existing request token protect administrative
actions. Its operator can create and select profiles and record memberships for
locally managed towns. Remote recipients authenticate the carrying town; person
attribution is asserted by that operator, not an independent credential. Membership
labels do not grant compute, resource, model or certification permissions.

Gas City currently permits session identities or its built-in operator mailbox
as native senders. Local personal messages therefore use that mailbox internally,
with the person's stable ID, name and conversation context in the stored envelope
and agent's message. Replies are assigned to the recorded person and conversation
by mail thread, not by name matching. The conversation UI does not present the
mailbox alias as the person's name. All local operator profiles share the operator's
access to this cockpit; this is not a multi-tenant public login system.

## Conversation and trace

Messages preserve human-readable text and structured payloads. Expand a message
to inspect its data and identifiers. A live route map and timeline link the actual
participants. Delivery receipts remain distinct from agent replies. The UI never
infers an agent's hidden reasoning or fabricates remote execution progress.

The relay body may contain an optional `_conversation` object:

- `id`: conversation UUID URN;
- `parent`: the causal message UUID URN, or null;
- `actor`: `id`, `display`, `kind: person`;
- `origin`: the sending town.

This is correlation and attribution metadata, not authorization. Updated workers
validate it, preserve it in their durable received record, remove it before
calling service input validators, and propagate it on responses. Handler-specific
access checks still run. Older peers can answer without this extension; ordinary
`in_reply_to` links still work.

Named residents receive instructions to delegate using:

```sh
pangenome-town send --conversation-parent RECEIVED_MESSAGE_ID \
  --to yamatai --resident bob --text 'Please investigate this part of the task'
```

Optional `--payload request.json` includes structured data. The CLI copies
conversation attribution from the locally recorded parent; it does not accept a
new actor from the payload. Replies inherit context using `--reply-to`.
Only explicit causal links are grouped. The cockpit checks that a responding town
matches the parent recipient. Town identity does not independently authenticate a
claimed individual agent.

Local delegated envelopes in the exchange log are visible. Remote towns' private
internal work is **not observable** unless they send linked replies/notices.
The first release does not export every remote agent's internal event stream.
A relay delivery receipt must never be labelled completion.

## Resources and the live diagnostic workflow

Attach references selected from the local resource catalogue. The composer
sends approved metadata, not file contents, and does not grant access.
Phenotypes must use label–ID pairs such as **Ectopia lentis (HP:0001083)**.
The installed diagnostic service validates label–ID agreement before inference.

For the working example, select **via Yamatai → Ubar → General contact**, attach
the synthetic patient VCF reference, and enter:

```text
Help diagnose this patient using their phenotypes and available VCF.
Explain the evidence and respect the data restrictions.
```

with:

```text
Ectopia lentis (HP:0001083)
Arachnodactyly (HP:0001166)
Aortic root aneurysm (HP:0002616)
```

The bounded diagnostic planner discovers advertised operations and respects the
owner-controlled data policy. It runs INDIGENA, prioritizes variants at Yamatai
and obtains Ubar/Themis's sourced PDF. The conversation records the actual service
messages, local observations and report. This is a synthetic research example,
not a general diagnostic system or unrestricted natural-language planner.

Each conversation supports one built-in diagnostic run; start a new conversation
for another run, while ordinary messages can continue in the original.
Resource/phenotype attachments clear after sending, so an ordinary follow-up
does not accidentally launch another analysis.

## Stop, persistence and scope

“Request stop” is a request, not a claim that a remote job was cancelled.
Named agents receive a message asking them to stop and confirm. The built-in
diagnostic coordinator checks cancellation at operation boundaries: an already
submitted operation may finish, but no later service is submitted. The transcript
distinguishes stop requested, stop pending and stopped.

Profiles, conversation history and reports persist locally. Delivery failure
remains visible. Work interrupted by a cockpit restart is not automatically
replayed; diagnostic conversations are marked for review. Trace retention
currently follows the existing exchange-log window (1000 messages) and local
mail window (300 messages); very late replies outside those windows require
operator inspection. Relay polls retrieve all available replies to known requests.

## Verification

```sh
.venv/bin/pytest -q tests/test_conversations.py
.venv/bin/python examples/conversations_live_check.py --screenshot /tmp/conversations.png
.venv/bin/python examples/conversations_mail_check.py
```

The last two commands contact this installation's real services. The browser
check runs the synthetic investigation; the mail check asks Themis for a brief
acknowledgement without requesting analysis. Tests also cover stable identity,
multiple town memberships, invalid ownership claims, causal delegation, unrelated
reply rejection, persistence, and stopping before the next operation.
