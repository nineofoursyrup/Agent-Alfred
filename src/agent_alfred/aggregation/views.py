"""Body-free present-day availability for historical provided-source identities."""

from copy import deepcopy


def source_availability(facts, service):
    value = deepcopy(facts)
    for ref in value.get("provided", ()):
        available = False
        try:
            if ref["kind"] == "history":
                permission = service.forgetting.evaluate_history(
                    (ref["run_id"],), purpose="working_window"
                )
                available = "error" not in permission and not permission["denied"]
            else:
                record = service.get(ref["kind"], ref["memory_id"])
                permission = service.forgetting.evaluate_automatic_records(
                    ((ref["kind"], ref["memory_id"], ref["record_version"]),)
                )
                available = (
                    record is not None
                    and record.record_version == ref["record_version"]
                    and "error" not in permission
                    and not permission["denied"]
                )
        except Exception:
            pass
        ref["available"] = available
    return value
