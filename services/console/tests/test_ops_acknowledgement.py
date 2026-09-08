from thaqip_console.app import _checkpoint_has_acknowledgement, _checkpoint_json


def test_checkpoint_acknowledgement_requires_structured_marker():
    assert _checkpoint_has_acknowledgement({"acknowledged_at": "2026-09-08T23:25:40Z"})
    assert _checkpoint_has_acknowledgement('{"acknowledged_at":"2026-09-08T23:25:40Z"}')

    assert not _checkpoint_has_acknowledgement(None)
    assert not _checkpoint_has_acknowledgement({"note": "acknowledged_at appears in text only"})
    assert not _checkpoint_has_acknowledgement("acknowledged_at")


def test_checkpoint_json_returns_acknowledgement_metadata():
    checkpoint = _checkpoint_json(
        '{"acknowledged_at":"2026-09-08T23:25:40Z","acknowledged_note":"reviewed"}'
    )

    assert checkpoint["acknowledged_at"] == "2026-09-08T23:25:40Z"
    assert checkpoint["acknowledged_note"] == "reviewed"
    assert _checkpoint_json("acknowledged_at") == {}
