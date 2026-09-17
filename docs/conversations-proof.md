# Named-person conversations and live agent workflow

*2026-09-17T07:19:52Z by Showboat 0.6.1*
<!-- showboat-id: 137609a6-8b07-4027-9e6b-d9e52f3d18b2 -->

The browser proof runs against this operator laptop: Robert is registered as owner of Ubar and Yamatai, their bridges are running, and the installed synthetic diagnostic services are available. The tests inspect actual messages and a generated PDF; profiles are operator attribution, not independent login credentials.

```bash
.venv/bin/python examples/conversations_live_check.py --screenshot /tmp/wasteland-conversations.png
```

```output
PASS: Robert’s stable person identity and ownership of Ubar/Yamatai appear in the UI.
PASS: local town contact returns a real directory response in the conversation.
PASS: task-level diagnostic request executes live with paired HPO terms and a private VCF reference.
PASS: agent messages, payloads, causal links and final PDF are inspectable; history survives reload.
```

```bash
.venv/bin/python examples/conversations_mail_check.py --existing
```

```output
PASS: named person → local Themis mailbox → real agent reply in the same conversation.
```
