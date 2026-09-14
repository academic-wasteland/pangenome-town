"""Research Commons Protocol layer of a pangenome town (step 3)."""

PG = "https://w3id.org/academic-wasteland/pangenome-town/contract/"
RCP = "https://w3id.org/research-commons/v0.1/"


def town_iri(town_name: str) -> str:
    return f"https://w3id.org/academic-wasteland/{town_name}/"
