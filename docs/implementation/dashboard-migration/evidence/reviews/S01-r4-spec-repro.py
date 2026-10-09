"""Review-only reproduction against an exported immutable candidate.

Uses the existing real temporary Host/SQLite helper with scripted model transport.
No worktree imports, product changes, network providers, or user business data.
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

HEAD = "5672fbb9e9aa13224bc1d9f07dcd736c033ae0df"
REPO = "/Users/nineofour/.codex/worktrees/dashboard-s01/Agent-Alfred"

with tempfile.TemporaryDirectory(prefix="alfred-spec-s01-r4-") as temporary:
    root = Path(temporary)
    archive = subprocess.check_output(["git", "archive", HEAD, "src"], cwd=REPO)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(root, filter="data")
    sys.path.insert(0, str(root / "src"))
    from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
    from agent_alfred.gateway.web.api import DashboardApi

    with ops_host(root / "state", [GATE, "answer"]) as (host, model, conn, _):
        original, _ = run(host)
        session = host.list_runs().runs[0].session_id
        for run_id in ("tie-a", "tie-b", "tie-c"):
            conn.execute(
                "INSERT INTO runs (run_id,purpose,session_id,gateway,"
                "prompt_preview,phase,outcome,accepted_at,finished_at,"
                "activity_revision,telemetry,admission_state) "
                "SELECT ?,purpose,session_id,gateway,prompt_preview,phase,"
                "outcome,accepted_at,finished_at,100,telemetry,admission_state "
                "FROM runs WHERE run_id=?",
                (run_id, original),
            )
        conn.commit()
        before = (host.snapshot(), len(model.requests))
        api = DashboardApi(facade=host)
        for limit in (1, 2):
            status, body = api.shared_read(
                "/api/sessions/runs/locate",
                {
                    "process_instance_id": host.process_instance_id,
                    "session_id": session,
                    "run_id": "tie-a",
                    "limit": str(limit),
                },
            )
            print(json.dumps({
                "candidate": HEAD,
                "case": "same activity_revision uses the existing run_id tie breaker",
                "requested_run": "tie-a",
                "limit": limit,
                "status": status,
                "target": body.get("target"),
                "returned_runs": [row["run_id"] for row in body.get("runs", ())],
                "target_in_page": any(row["run_id"] == "tie-a" for row in body.get("runs", ())),
            }, ensure_ascii=False))
        assert (host.snapshot(), len(model.requests)) == before
