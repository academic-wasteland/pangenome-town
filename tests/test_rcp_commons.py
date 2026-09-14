from datetime import UTC, datetime, timedelta

from pangenome_town.rcp import commons


def stamp(author, quality, *, days_ago=0, severity="leaf", confidence=1.0):
    created = (datetime(2026, 9, 14, tzinfo=UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {"author": author, "subject": "yamatai", "valence": {"quality": quality, "rcp_status": "entailed" if quality == 1 else "unknown"},
            "confidence": confidence, "severity": severity, "created_at": created}


def test_handle_for_maps_town_agents_and_keeps_other_iris():
    assert commons.handle_for("https://w3id.org/academic-wasteland/ubar/agents/townsfolk") == "ubar"
    assert commons.handle_for("https://w3id.org/academic-wasteland/yamatai/") == "yamatai"
    assert commons.handle_for("https://orcid.org/0000-0001-8149-5890") == "https://orcid.org/0000-0001-8149-5890"


def test_standing_scores_fresh_entailed_stamps_at_one():
    now = datetime(2026, 9, 14, tzinfo=UTC)
    standing = commons.standing_from_stamps("yamatai", [stamp("ubar", 1.0)], now=now)
    assert round(standing.score, 3) == 1.0
    assert standing.authors == 1 and standing.stamps == 1


def test_standing_decays_and_weights_severity():
    now = datetime(2026, 9, 14, tzinfo=UTC)
    old = commons.standing_from_stamps("yamatai", [stamp("ubar", 1.0, days_ago=180)], now=now)
    assert 0.36 < old.score < 0.37
    root = commons.standing_from_stamps("yamatai", [stamp("ubar", 1.0, severity="root")], now=now)
    assert round(root.score, 3) == 3.0


def test_negative_and_unknown_stamps_count_correctly():
    now = datetime(2026, 9, 14, tzinfo=UTC)
    mixed = commons.standing_from_stamps("yamatai", [stamp("ubar", 0.0), stamp("sam", 0.5), stamp("bob", 1.0)], now=now)
    assert round(mixed.score, 3) == 0.0  # -1 + 0 + 1
    assert mixed.authors == 2  # quality >= 0.5 only


def test_wl_accept_ratings_are_normalised():
    assert commons._quality({"quality_rating": 5}) == 1.0
    assert commons._quality({"rating": 1}) == 0.0
    assert commons._quality("garbage") == 0.5
