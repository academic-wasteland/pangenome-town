# Review credential applications in the cockpit

Open **Decisions & trust** at http://localhost:8393/#decisions on the operator
laptop. Each pending application has **Review & approve** and **Deny** buttons.
You do not need a terminal or a town configuration path.

For approval:

1. Read the holder, issuer, requested claim and full application purpose.
2. Select the credential lifetime (one day by default).
3. For a real qualification, enter the evidence you reviewed or its private
   reference. A request or an agent recommendation is not evidence of a qualification.
4. Confirm the review checkbox and click **Approve credential**.

The exact `relay-test-only` / `SyntheticRelayTestOnly` qualification can be
reviewed with no text entry. The dashboard records a synthetic-only review note;
it does not attest real qualifications or data permissions. Other claims require
review notes or an evidence reference. This is manual review, not automated
verification of the evidence's truth.

To deny, choose a reason from the menu; an optional explanation can be added.
The application disappears from the pending list, a result appears above it,
and issuance or denial appears in recent authority events. The existing authority
tables also show the application state and issued credentials. Repeat decisions
on an already decided application are refused.

The reviewer is the authority's configured `authority.operator`; a request
cannot override that identity. As with other cockpit actions, this records the
local operator's decision, not a separate browser login identity. All decision
POSTs require the existing cockpit token and loopback Host/Origin checks. The
public relay registrar and visitor demo do not gain approval endpoints.

Review notes and references are stored as mode-0600 files under the authority
key directory's `dashboard-reviews/`. The registrar's normal `reviews/` audit
links to that private record using `dashboard-review:<UUID>`. Neither the issued
credential nor the shared event feed contains the notes or private references.
A decision interrupted by an I/O failure may leave a review record without an
issued credential: refresh and inspect state before retrying.

The CLI alternative remains inside a collapsed section. Notification messages
now include the actual loaded configuration path and explicitly require a review
reference for relay applications.

Verification:

```bash
.venv/bin/pytest -q tests/test_dashboard_approvals.py tests/test_cli_resources.py tests/test_cockpit.py
RUN_BROWSER_CHECKS=1 .venv/bin/pytest -q tests/test_dashboard_approvals.py
```

The browser test opens an isolated test authority, approves a synthetic request
using the buttons, denies another with the reason menu, and reloads to check the
persisted decisions. No live credentials are issued by the tests.
