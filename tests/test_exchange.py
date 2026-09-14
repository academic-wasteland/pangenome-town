import json

import pytest

from pangenome_town.exchange import Attachment, Envelope, EnvelopeError, ExchangeLog


def test_envelope_round_trip(tmp_path):
    artifact = tmp_path / "variants.json"
    artifact.write_text("{}", encoding="utf-8")
    envelope = Envelope.new("question", "ubar", "yamatai", {"text": "hi", "region": "GRCh38:chr1:0-10"},
                            attachments=(Attachment.from_file(artifact),))
    data = json.loads(json.dumps(envelope.to_dict()))
    again = Envelope.from_dict(data)
    assert again == envelope
    assert again.digest == envelope.digest
    assert again.attachments[0].sha256.startswith("sha256:")


@pytest.mark.parametrize(
    "mutation",
    [
        {"kind": "order"},
        {"to": "ubar"},
        {"from": "not a town"},
        {"id": "12345"},
        {"kind": "answer", "in_reply_to": None},
        {"body": {"text": 5}},
    ],
)
def test_invalid_envelopes(mutation):
    data = Envelope.new("question", "ubar", "yamatai", {"text": "hi"}).to_dict()
    data.update(mutation)
    with pytest.raises(EnvelopeError):
        Envelope.from_dict(data)


def test_attachment_names_are_plain():
    with pytest.raises(EnvelopeError):
        Attachment.from_dict({"name": "../etc/passwd", "sha256": "sha256:00"})


def test_log_pending_and_answers(tmp_path):
    log = ExchangeLog(tmp_path / "x.db")
    question = Envelope.new("question", "ubar", "yamatai", {"text": "q"})
    assert log.record(question, town="ubar", direction="sent", status="sent") is True
    assert log.record(question, town="yamatai", direction="received", status="received") is False
    assert [m["id"] for m in log.pending_questions("yamatai")] == [question.id]
    assert log.pending_questions("ubar") == []
    log.event("yamatai", "dispatched", question.id, {})
    assert log.pending_questions("yamatai") == []
    answer = Envelope.new("answer", "yamatai", "ubar", {"text": "a"}, in_reply_to=question.id)
    log.record(answer, town="yamatai", direction="sent", status="sent")
    assert [a["id"] for a in log.answers(question.id)] == [answer.id]
    kinds = [e["kind"] for e in log.events(question.id)]
    assert kinds == ["sent", "received", "dispatched"]
    assert log.get(question.id)["status"] == "received"
    assert log.list(town="ubar")[0]["id"] == answer.id
    log.close()


def test_rcp_mirrors_are_not_pending_questions(tmp_path):
    from pangenome_town.exchange import Envelope, ExchangeLog, now_iso

    log = ExchangeLog(tmp_path / "x.db")
    try:
        plain = Envelope(id="urn:uuid:11111111-1111-4111-8111-111111111111", kind="question", sender="ubar", recipient="yamatai", created=now_iso(), body={"text": "q"})
        mirrored = Envelope(id="urn:uuid:22222222-2222-4222-8222-222222222222", kind="question", sender="ubar", recipient="yamatai", created=now_iso(), body={"text": "RCP completed"})
        log.record(plain, town="yamatai", direction="received", status="received")
        log.record(mirrored, town="yamatai", direction="received", status="rcp-completed")
        assert [m["id"] for m in log.pending_questions("yamatai")] == [plain.id]
    finally:
        log.close()
