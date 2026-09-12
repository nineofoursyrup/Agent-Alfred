"""Transport mapping for host-owned Tools/Ops operations."""

from agent_alfred.runtime.accounting import AccountingError
from agent_alfred.runtime.tool_history import HistoryError

READS = {
    "/api/tools",
    "/api/ops",
    "/api/ops/detail",
    "/api/tools/history",
    "/api/tools/verification",
}
WRITES = {
    "/api/tools/authorization",
    "/api/tools/reapply",
    "/api/ops/snapshots",
    "/api/tools/metering/recover",
}


def status(value):
    code = value.get("error", {}).get("code")
    if not code:
        return 200, value
    if code in ("run_in_progress", "mutation_in_flight", "authorization_conflict"):
        return 409, value
    if code == "invalid_input":
        return 400, value
    return 503, value


def read(host, root, path, params):
    try:
        if path == "/api/tools":
            return 200, host.tools_catalog()
        if path == "/api/ops":
            return 200, host.accounting_page(
                params["snapshot_id"], int(params.get("offset", "0"))
            )
        if path == "/api/ops/detail":
            return 200, host.accounting_detail(params["snapshot_id"], params["run_id"])
        if path == "/api/tools/verification":
            return 200, host.tool_verification(
                params["run_id"], int(params["step_index"]), params["call_id"]
            )
        if root is None:
            return 503, {"error": {"code": "history_unavailable"}}
        if set(params) - {"run_id", "step_index", "call_id", "projection", "cursor"}:
            return 400, {"error": {"code": "invalid_input"}}
        return 200, host.read_tool_history(
            root,
            run_id=params["run_id"],
            step_index=int(params["step_index"]),
            call_id=params["call_id"],
            projection=params.get("projection", "model"),
            cursor=params.get("cursor"),
        )
    except AccountingError as error:
        return (410 if str(error) == "snapshot_expired" else 400), {
            "error": {"code": str(error)}
        }
    except HistoryError as error:
        return 410, {"error": {"code": str(error)}}
    except ValueError, KeyError, TypeError:
        return 400, {"error": {"code": "invalid_input"}}


def write(host, path, body):
    if not isinstance(body, dict):
        return 400, {"error": {"code": "invalid_input"}}
    try:
        if path == "/api/ops/snapshots":
            return 200, host.accounting_snapshot(body)
        if path == "/api/tools/authorization":
            return status(
                host.save_tool_authorization(
                    body["identity"], body["authorization"], body["expected_revision"]
                )
            )
        if path == "/api/tools/reapply":
            return status(host.reapply_tool_authorization(body["expected_revision"]))
        result, refusal = host.recover_tool_metering()
        return status({"error": {"code": refusal}} if refusal else {"recovered": True})
    except AccountingError as error:
        return (429 if str(error) == "snapshot_quota" else 400), {
            "error": {"code": str(error)}
        }
    except ValueError, KeyError, TypeError:
        return 400, {"error": {"code": "invalid_input"}}
