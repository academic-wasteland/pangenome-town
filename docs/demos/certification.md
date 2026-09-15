# Independent certification providers: six cases and real Keycloak

*2026-09-15T12:54:56Z by Showboat 0.6.1*
<!-- showboat-id: 362bb562-93d4-472f-a1d1-771db5d060d6 -->

Run from the pangenome-town repository root with .venv populated using pip install .[dev]. These assertions use ephemeral keys and toy tasks; no messages are sent to towns and no research data is accessed.

Local provider instead of Camelot

```bash
.venv/bin/python examples/certification_scenarios.py 1
```

```output
1. Lisan: Camelot refused; local Fremen certification accepted.
```

One independent provider accepted by two towns

```bash
.venv/bin/python examples/certification_scenarios.py 2
```

```output
2. Independent Board accepted by two towns; qualification grants no dataset access.
```

Different authorities approve different parts of a task

```bash
.venv/bin/python examples/certification_scenarios.py 3
```

```output
3. Qualification + Ubar custody + Yamatai compute + ethics required; changed task refused.
```

Narrowly scoped delegation

```bash
.venv/bin/python examples/certification_scenarios.py 4
```

```output
4. Scoped Board-to-lab delegation accepted; raw export and undelegated onward authority refused.
```

Bloodninja and explicit human delegation

```bash
.venv/bin/python examples/certification_scenarios.py 5
```

```output
5. Bloodninja cannot borrow an analyst identity; human delegation must approve the exact task.
```

Expiry, withdrawal and unavailable status

```bash
.venv/bin/python examples/certification_scenarios.py 6
```

```output
6. Active approval accepted; revoked, unavailable and expired certification block protected work.
```

Real Keycloak 26.7.3 with a service-account group and a separate receiver introspection client. Requires Docker; the loopback-only temporary container is removed after the checks.

```bash
.venv/bin/python examples/keycloak_demo.py
```

```output
Real Keycloak: service-account identity and certified group accepted.
Real Keycloak: same token cannot authenticate Bloodninja.
Real Keycloak: group membership does not grant Saudi data access.
Real Keycloak: revoked token refused by fresh introspection.
Temporary Keycloak container and credentials removed.
```
