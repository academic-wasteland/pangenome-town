import json

from pangenome_town.cli import main
from pangenome_town.exchange import Envelope, ExchangeLog


def test_send_dry_run_and_messages(towns, capsys):
    ubar = towns["ubar"]
    assert main(["--town", str(ubar.city_root / "town.toml"), "send", "--to", "yamatai", "--text", "q", "--region", "chr1:0-10", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["envelope"]["to"] == "yamatai" and out["envelope"]["body"]["region"] == "GRCh38:chr1:0-10"
    assert out["url"].endswith("/v0/city/yamatai/svc/envoy/v0/messages")
    log = ExchangeLog(towns["db"])
    question = Envelope.new("question", "ubar", "yamatai", {"text": "q"})
    log.record(question, town="yamatai", direction="received", status="received")
    log.close()
    yamatai_toml = str(towns["yamatai"].city_root / "town.toml")
    assert main(["--town", yamatai_toml, "inbox", "--check"]) == 0
    assert main(["--town", yamatai_toml, "inbox", "--mark-dispatched", question.id]) == 0
    capsys.readouterr()
    assert main(["--town", yamatai_toml, "inbox", "--check"]) == 1
    assert main(["--town", yamatai_toml, "messages", "--id", question.id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "dispatched"


def test_send_to_unknown_peer_fails(towns, capsys):
    assert main(["--town", str(towns["ubar"].city_root / "town.toml"), "send", "--to", "atlantis", "--text", "q"]) == 2
    assert "unknown peer" in capsys.readouterr().out
