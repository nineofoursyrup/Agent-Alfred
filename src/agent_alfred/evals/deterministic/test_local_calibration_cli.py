"""The local control entrypoint must not turn inspection or bad input into consent."""

import json

from agent_alfred.evals.acceptance.controlled import local


def test_default_inspection_has_no_installation_or_owner_side_effect(
    monkeypatch, capsys
):
    from agent_alfred.evals.acceptance.controlled import local_runtime, native

    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("inspection tried to install or authenticate")

    monkeypatch.setattr(local_runtime, "install_local_runtime", forbidden)
    monkeypatch.setattr(native, "authorize_owner", forbidden)
    assert local.main(["inspect"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "BLOCKED"
    assert report["run_grant"] is False
    assert report["real_requests"] == 0
    assert calls == []


def test_ambiguous_decision_is_rejected_before_opening_control_plane(
    tmp_path, monkeypatch, capsys
):
    from agent_alfred.evals.acceptance.controlled import local_runtime

    def forbidden(*args, **kwargs):
        raise AssertionError("malformed request reached the owner control plane")

    monkeypatch.setattr(local_runtime, "install_local_runtime", forbidden)
    request = tmp_path / "ambiguous.json"
    request.write_text('{"scope":"run","scope":"checkpoint"}')
    assert local.main(
        [
            "owner-decision",
            "--config",
            str(tmp_path / "unopened.json"),
            "--request",
            str(request),
            "--decision",
            "approved",
            "--reason",
            "test request only",
            "--evidence",
            "synthetic:malformed-input",
        ]
    ) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "BLOCKED"
    assert report["blockers"] != ["local_preflight_failed"]
    assert report["real_requests"] == 0
