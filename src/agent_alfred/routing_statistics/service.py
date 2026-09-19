"""One read transaction, exact counts, and a bounded read-only query lifetime."""

import json
import sqlite3
import time
import uuid
from datetime import timedelta, timezone
from functools import partial
from pathlib import Path

from agent_alfred import schema
from agent_alfred.resource_rollback import ResumableRollback, dominant_error
from agent_alfred.routing_statistics import METRICS_VERSION, POLICY_VERSION, ROUTES

WINDOWS = {"24h": 1, "7d": 7, "30d": 30, "all": None}


class StatisticsError(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def object_value(value):
    return value if isinstance(value, dict) else {}


def decode(raw):
    try:
        return object_value(json.loads(raw)) if raw is not None else {}
    except ValueError, TypeError, RecursionError:
        return {}


def boolean(value):
    return value if type(value) is bool else None


def fraction(numerator, denominator, comparable):
    return dict(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator and comparable else None,
    )


def result_facts(memory):
    result = object_value(memory.get("routing_statistics"))
    if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        return dict(
            route="unknown",
            fallback=None,
            recovered=None,
            bypass=None,
            blocked=None,
            blocked_unknown=True,
            missing_reason="summary_missing" if not result else "unsupported_format",
        )
    route = result.get("route")
    route = (
        route
        if isinstance(route, str) and route in ROUTES
        else (
            "none"
            if "route" in result
            and route is None
            and result.get("decision_complete") is True
            else "unknown"
        )
    )
    f, b = boolean(result.get("fallback")), boolean(result.get("bypass"))
    entered = boolean(result.get("graph_entered"))
    if (f is True and entered is not True) or (b is True and entered is not False):
        f = b = None
    if f is True and b is True:
        f = b = None
    recovered = boolean(result.get("recovered"))
    if entered is False:
        if route in ROUTES:
            route = "unknown"
        if recovered is True:
            recovered = None
    blocked = result.get("blocked")
    blocked_known = "blocked" in result and (
        blocked is None or (blocked in BLOCKED and f is False and b is False)
    )
    if blocked in BLOCKED:
        # Explicit denial conflicts with evidence of actual ordinary-loop entry.
        if f is True:
            f = None
        if b is True:
            b = None
    if f is not False or b is not False or blocked not in BLOCKED:
        blocked = None
    return dict(
        route=route,
        fallback=f,
        bypass=b,
        recovered=recovered,
        blocked=blocked,
        blocked_unknown=not blocked_known,
        missing_reason="invalid_or_missing_metric",
    )


BLOCKED = (
    "side_effect_occurred",
    "side_effect_unknown",
    "budget_exhausted",
    "context_invalid",
    "forced_stop",
    "overall_deadline",
    "cancelled",
    "input_evidence_unavailable",
)


def group_new(versions, current=False):
    return dict(
        metrics_version=versions[0],
        policy_version=versions[1],
        current=current,
        comparable=versions != (None, None),
        total=0,
        decisions=dict(counts=dict.fromkeys(ROUTES, 0), known=0, none=0, unknown=0),
        fallback=dict(yes=0, no=0, unknown=0),
        recovered=dict(yes=0, no=0, unknown=0),
        bypass=dict(yes=0, no=0, unknown=0),
        blocked=dict(total=0, reasons={}),
        missing_reasons={},
    )


def collect(group, facts):
    group["total"] += 1
    if (
        facts["blocked_unknown"]
        or facts["route"] == "unknown"
        or any(facts[name] is None for name in ("fallback", "recovered", "bypass"))
    ):
        reasons = group["missing_reasons"]
        reason = facts["missing_reason"]
        reasons[reason] = reasons.get(reason, 0) + 1
    route = facts["route"]
    decisions = group["decisions"]
    if route in ROUTES:
        decisions["counts"][route] += 1
        decisions["known"] += 1
    else:
        decisions[route] += 1
    for name in ("fallback", "recovered", "bypass"):
        key = (
            "yes"
            if facts[name] is True
            else "no"
            if facts[name] is False
            else "unknown"
        )
        group[name][key] += 1
    if facts["blocked"]:
        blocked = group["blocked"]
        blocked["total"] += 1
        reasons = blocked["reasons"]
        reasons[facts["blocked"]] = reasons.get(facts["blocked"], 0) + 1


def finish(group):
    n, comparable = group["total"], group["comparable"]
    decisions = group["decisions"]
    d = decisions["known"]
    assert n == d + decisions["none"] + decisions["unknown"]
    decisions["coverage"] = fraction(d, n, comparable)
    decisions["ratios"] = {
        name: fraction(count, d, comparable)
        for name, count in decisions["counts"].items()
    }
    for name in ("fallback", "recovered", "bypass"):
        metric = group[name]
        k = metric["yes"] + metric["no"]
        assert n == k + metric["unknown"]
        metric["known"] = k
        if name != "bypass":
            metric["rate"] = fraction(metric["yes"], k, comparable)
            metric["coverage"] = fraction(k, n, comparable)
    group["missing"] = {
        "decision_unknown": decisions["unknown"],
        "fallback_unknown": group["fallback"]["unknown"],
        "recovery_unknown": group["recovered"]["unknown"],
        "bypass_unknown": group["bypass"]["unknown"],
    }
    return group


def read_statistics(
    path,
    *,
    window,
    as_of,
    process_instance_id,
    cancelled=None,
    work_clock=time.monotonic,
    checkpoint=None,
):
    if window not in WINDOWS:
        raise StatisticsError("invalid_window", 400)
    deadline = work_clock() + 2.0

    def check(stage):
        if checkpoint is not None:
            checkpoint(stage)
        if cancelled is not None and cancelled():
            raise StatisticsError("query_cancelled", 499)
        if work_clock() >= deadline:
            raise StatisticsError("query_timeout", 504)

    check("open")
    if not path:
        raise StatisticsError("recording_unavailable")
    as_of = as_of.astimezone(timezone.utc)
    lower = as_of - timedelta(days=WINDOWS[window]) if WINDOWS[window] else None
    current = (METRICS_VERSION, POLICY_VERSION)
    groups = {current: group_new(current, True)}
    sample = dict(
        admitted=0,
        enabled=0,
        disabled=0,
        unknown=0,
        finished=0,
        pending=0,
        unknown_bypass=0,
    )
    conn = None
    try:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=max(0, deadline - work_clock()),
        )
        conn.set_progress_handler(
            lambda: int(work_clock() >= deadline or bool(cancelled and cancelled())),
            100,
        )
        check("sql")
        conn.execute("BEGIN")
        cursor = conn.execute(
            "SELECT run_id, purpose, gateway, admission_state, accepted_at, phase, "
            "routing_admission, telemetry FROM runs"
        )
        check("snapshot")
        for (
            run_id,
            purpose,
            gateway,
            admitted,
            accepted,
            phase,
            raw,
            telemetry,
        ) in cursor:
            check("row")
            if not isinstance(run_id, str) or not run_id:
                raise StatisticsError("statistics_invalid_data")
            if purpose not in schema.PURPOSES or gateway not in schema.GATEWAYS:
                raise StatisticsError("statistics_invalid_data")
            if purpose != "chat" or gateway not in ("cli", "web"):
                continue
            if admitted not in ("admitted", "pending", "rejected", "unconfirmed"):
                raise StatisticsError("statistics_invalid_data")
            if admitted != "admitted":
                continue
            try:
                at = schema.parse_instant(accepted)
            except ValueError, TypeError:
                raise StatisticsError("statistics_invalid_data") from None
            if at >= as_of or (lower is not None and at < lower):
                continue
            if phase not in schema.PHASES:
                raise StatisticsError("statistics_invalid_data")
            check("parse")
            evidence = decode(raw)
            memory = object_value(decode(telemetry).get("memory"))
            enablement = evidence.get("enablement", "unknown")
            if enablement not in ("enabled", "disabled", "unknown"):
                enablement = "unknown"
            supported = (
                type(evidence.get("schema_version")) is int
                and evidence["schema_version"] == 1
            )
            if not supported:
                enablement = "unknown"
            versions = (evidence.get("metrics_version"), evidence.get("policy_version"))
            if not supported or versions != current:
                versions = (None, None)
            facts = (
                result_facts(memory)
                if supported and versions == current
                else result_facts({})
            )
            if supported and versions == current and "routing" in memory:
                from agent_alfred.routing_statistics.legacy import legacy_facts

                _, previous = legacy_facts(memory)
                if facts["route"] != "unknown" and previous["route"] != "unknown":
                    if facts["route"] != previous["route"]:
                        facts["route"] = "unknown"
                for metric in ("fallback", "bypass", "recovered"):
                    if (
                        facts[metric] is not None
                        and previous[metric] is not None
                        and facts[metric] != previous[metric]
                    ):
                        facts[metric] = None
                if (
                    not facts["blocked_unknown"]
                    and not previous["blocked_unknown"]
                    and facts["blocked"] != previous["blocked"]
                ):
                    facts["blocked"] = None
                    facts["blocked_unknown"] = True
                if facts["blocked"] and (
                    facts["fallback"] is not False or facts["bypass"] is not False
                ):
                    facts["blocked"] = None
                    facts["blocked_unknown"] = True
            if raw is None and phase == "finished":
                from agent_alfred.routing_statistics.legacy import legacy_facts

                enablement, facts = legacy_facts(memory)
            if supported and versions != current:
                facts["missing_reason"] = "unsupported_semantics"
            sample["admitted"] += 1
            sample[enablement] += 1
            if (
                enablement == "unknown"
                and phase == "finished"
                and facts["bypass"] is True
            ):
                sample["unknown_bypass"] += 1
            if enablement != "enabled":
                continue
            if phase != "finished":
                sample["pending"] += 1
                continue
            sample["finished"] += 1
            group = groups.setdefault(versions, group_new(versions))
            collect(group, facts)
        cursor.close()
        check("result")
        result = dict(
            schema_version=1,
            window=window,
            from_at=lower.isoformat() if lower else None,
            as_of=as_of.isoformat(),
            timezone="UTC",
            read_id=uuid.uuid4().hex,
            process_instance_id=process_instance_id,
            current_version=dict(metrics_version=current[0], policy_version=current[1]),
            sample=sample,
            groups=[finish(g) for g in groups.values()],
        )
        check("complete")
        return result
    except sqlite3.Error:
        check("sqlite_error")
        raise StatisticsError("recording_unavailable") from None
    finally:
        if conn is not None:
            conn.close()


def _close_worker(child):
    """Idempotent cleanup; Popen returncode and pipe.closed retain progress."""
    if getattr(child, "pid", None) is not None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
    for name in ("stdin", "stdout", "stderr"):
        pipe = getattr(child, name, None)
        if pipe is not None and not pipe.closed:
            pipe.close()


def _finish_worker(owner, failure):
    try:
        owner.close()
    except BaseException as cleanup_error:
        primary = dominant_error(failure, cleanup_error)
        # One immediate continuation settles a single interrupted release.
        # Persistent failure retains the typed retry handle, never a bare child.
        if not owner.retry():
            owner.raise_incomplete(primary)
        raise primary


def query_statistics(
    path, *, window, as_of, process_instance_id, cancelled=None, _deadline=None
):
    """The request owns the child until it exits, including on disconnect.

    A process boundary bounds C-level JSON parsing, SQLite busy waits and result
    construction as well as VM progress. No work survives the HTTP response.
    """
    import subprocess
    import sys

    deadline = _deadline if _deadline is not None else time.monotonic() + 2.0
    if not isinstance(window, str) or window not in WINDOWS:
        raise StatisticsError("invalid_window", 400)
    request = json.dumps(
        dict(
            path=path,
            window=window,
            as_of=as_of.isoformat(),
            process_instance_id=process_instance_id,
        )
    )
    # Retain the object before native construction or its return boundary.
    child = subprocess.Popen.__new__(subprocess.Popen)
    owner = ResumableRollback()
    owner.own(child, partial(_close_worker, child))
    failure = None
    try:
        try:
            if time.monotonic() >= deadline:
                raise StatisticsError("query_timeout", 504)
            subprocess.Popen.__init__(
                child,
                [sys.executable, "-m", "agent_alfred.routing_statistics.worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            incoming = request
            while True:
                if cancelled is not None and cancelled():
                    raise StatisticsError("query_cancelled", 499)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StatisticsError("query_timeout", 504)
                try:
                    output, _ = child.communicate(
                        incoming, timeout=min(remaining, 0.025)
                    )
                    break
                except subprocess.TimeoutExpired:
                    incoming = None
            if time.monotonic() >= deadline:
                raise StatisticsError("query_timeout", 504)
            if child.returncode != 0:
                raise StatisticsError("recording_unavailable")
            response = json.loads(output)
            if "code" in response:
                raise StatisticsError(response["code"], response["status"])
            return response["result"]
        except OSError, ValueError, KeyError:
            failure = StatisticsError("recording_unavailable")
            raise failure from None
        except BaseException as exc:
            failure = exc
            raise
        finally:
            _finish_worker(owner, failure)
    except BaseException as exc:
        # Also own interruption at cleanup dispatch/return boundaries.
        owner.raise_failure(exc)
