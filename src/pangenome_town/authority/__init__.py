"""Credentials and demo authorities for pangenome towns.

Authority is presented, capability is exercised: credentials in this package are
signed public documents that travel with a request. Secrets that open compute
sites never appear here.
"""

CRED = "https://w3id.org/academic-wasteland/credentials/v0.1/"
AUTHORITY = "https://w3id.org/academic-wasteland/camelot/"  # Camelot, the authority town
VC_CONTEXT = ["https://www.w3.org/ns/credentials/v2", "https://w3id.org/academic-wasteland/credentials/v0.1"]
PROOF_TYPE = "PangenomeTownEd25519Jcs2026"
CREDENTIAL_TYPES = ("Accreditation", "EthicsApproval", "DataAccessAuthorization")


def issuer_iri(slug: str) -> str:
    return f"{AUTHORITY}issuers/{slug}"
