"""Immutable body-free accounting views; explicit expiry, never partial membership."""

import hashlib
import json
import threading
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_alfred.clock import format_instant
from agent_alfred.pricing import BILLING, project_cost

TOKENS = (
    "total_input_tokens",
    "uncached_input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)


class AccountingError(ValueError):
    pass


def normalize_filters(value, now):
    if not isinstance(value, dict) or set(value) - {
        "range",
        "timezone",
        "start",
        "end",
        "session_id",
        "purpose",
        "tool",
        "run_id",
    }:
        raise AccountingError("invalid_filters")
    try:
        zone = ZoneInfo(value.get("timezone", ""))
    except ZoneInfoNotFoundError, ValueError, TypeError:
        raise AccountingError("invalid_timezone") from None
    kind = value.get("range", "7d")
    today = now.astimezone(zone).date()
    if kind == "all":
        start = end = None
    elif kind in ("today", "7d", "30d"):
        start = today - timedelta(days={"today": 0, "7d": 6, "30d": 29}[kind])
        end = today + timedelta(days=1)
    elif kind == "custom":
        try:
            start, end = (
                date.fromisoformat(value["start"]),
                date.fromisoformat(value["end"]),
            )
        except KeyError, ValueError, TypeError:
            raise AccountingError("invalid_interval") from None
        if start >= end:
            raise AccountingError("invalid_interval")
    else:
        raise AccountingError("invalid_range")
    result = {
        k: value[k] for k in ("session_id", "purpose", "tool", "run_id") if value.get(k)
    }
    if any(type(v) is not str for v in result.values()):
        raise AccountingError("invalid_filters")
    result.update(
        range=kind,
        timezone=zone.key,
        start=None
        if start is None
        else datetime.combine(start, time(), zone).astimezone(UTC).isoformat(),
        end=None
        if end is None
        else datetime.combine(end, time(), zone).astimezone(UTC).isoformat(),
    )
    return result


class FrozenPrices:
    def __init__(self, prices, identities):
        self.values = {
            (e, m, d): prices.quote(e, m, d) for e, m in identities for d, _ in BILLING
        }

    def quote(self, endpoint_id, model_id, dimension):
        return self.values.get((endpoint_id, model_id, dimension))


def safe_attempt(raw, prices, at):
    if not isinstance(raw, dict) or not isinstance(raw.get("attempt_id"), str):
        raise ValueError("invalid_attempt")
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    safe = {}
    for key in TOKENS:
        value = usage.get(key)
        safe[key] = value if type(value) is int and value >= 0 else None
    value = usage.get("endpoint_reported_cost_usd")
    if type(value) in (str, int):
        try:
            amount = Decimal(value)
            if amount.is_finite() and amount >= 0:
                safe["endpoint_reported_cost_usd"] = format(amount, "f")
        except InvalidOperation:
            pass
    model = raw.get("model")
    if not isinstance(model, dict) or any(
        type(model.get(k)) is not str for k in ("endpoint_id", "model_id")
    ):
        model = {"endpoint_id": None, "model_id": None}
    model = {k: model[k] for k in ("endpoint_id", "model_id")}
    return {
        "attempt_id": raw["attempt_id"],
        "outcome": raw.get("outcome")
        if raw.get("outcome") in ("committed", "aborted")
        else "unrecorded",
        "model": {k: model[k] for k in ("endpoint_id", "model_id")},
        "usage": safe,
        "cost": project_cost(safe, prices, **model, computed_at=at),
    }


def summary(rows):
    result = {
        "run_count": len(rows),
        "attempt_count": 0,
        "unknown_cost_attempts": 0,
        "incomplete_runs": 0,
        "tool_requests": 0,
        "confirmed_starts": 0,
        "not_started": 0,
        "unconfirmed_starts": 0,
        "tokens": {k: {"known": 0, "missing_attempts": 0} for k in TOKENS},
        "tool_cost_states": {
            k: 0 for k in ("not_billable", "reported", "unknown", "unrecorded")
        },
    }
    exact = estimated = Decimal(0)
    groups = {}
    for row in rows:
        result["incomplete_runs"] += bool(row["coverage"])
        for attempt in row["attempts"]:
            result["attempt_count"] += 1
            cost = attempt["cost"]
            if cost["state"] == "exact":
                exact += Decimal(cost["amount"])
            elif cost["state"] == "estimated":
                estimated += Decimal(cost["amount"])
            else:
                result["unknown_cost_attempts"] += 1
            for key in TOKENS:
                value = attempt["usage"][key]
                result["tokens"][key][
                    "missing_attempts" if value is None else "known"
                ] += 1 if value is None else value
        for tool in row["tools"]:
            result["tool_requests"] += 1
            result[
                {
                    "confirmed": "confirmed_starts",
                    "not_started": "not_started",
                    "unconfirmed": "unconfirmed_starts",
                }.get(tool["start_confirmation"], "unconfirmed_starts")
            ] += 1
            cost = tool["cost"]
            kind = (
                cost.get("kind", "unrecorded")
                if isinstance(cost, dict)
                else "unrecorded"
            )
            if kind == "reported":
                try:
                    amount = Decimal(cost["units"])
                    if (
                        not amount.is_finite()
                        or amount < 0
                        or not cost["service"]
                        or not cost["unit"]
                    ):
                        raise ValueError("invalid_cost")
                    key = cost["service"], cost["unit"]
                    groups[key] = groups.get(key, Decimal(0)) + amount
                except ValueError, TypeError, KeyError, InvalidOperation:
                    kind = "unknown"
            result["tool_cost_states"][
                kind if kind in result["tool_cost_states"] else "unknown"
            ] += 1
    result.update(
        exact_usd=format(exact, "f"),
        estimated_usd=format(estimated, "f"),
        tool_costs=[
            {"service": k[0], "unit": k[1], "units": format(v, "f")}
            for k, v in sorted(groups.items())
        ],
    )
    return result


class AccountingSnapshots:
    def __init__(
        self,
        store,
        metering,
        clock,
        process_id,
        price_factory,
        *,
        ttl=900,
        max_count=8,
        max_bytes=64 * 1024 * 1024,
    ):
        self.store, self.metering, self.clock = store, metering, clock
        self.process_id, self.price_factory = process_id, price_factory
        self.ttl, self.max_count, self.max_bytes = ttl, max_count, max_bytes
        self.lock, self.snapshots = threading.RLock(), {}

    def create(self, filters, *, recording_failed=()):
        with self.lock:
            now = self.clock.wall_utc()
            normalized = normalize_filters(filters, now)
            self._expire()
            if len(self.snapshots) >= self.max_count:
                raise AccountingError("snapshot_quota")
            prices = self.price_factory()
            rows = []
            unresolved_membership = []
            captured_bytes = 0
            # No read transaction survives this block. A single consistent read
            # fixes membership, metering, telemetry, and prune facts together.
            with self.store.transaction() as conn:
                conn.execute("BEGIN")
                cursor = conn.execute("""SELECT run_id,session_id,purpose,phase,outcome,
                    accepted_at,started_at,activity_revision,telemetry FROM runs
                    ORDER BY activity_revision DESC,run_id DESC""")
                names = [d[0] for d in cursor.description]
                for values in cursor:
                    row = dict(zip(names, values, strict=True))
                    if any(
                        normalized.get(k) and normalized[k] != row[k]
                        for k in ("run_id", "session_id", "purpose")
                    ):
                        continue
                    tools = self.metering.read(row["run_id"], conn=conn)
                    for tool in tools:
                        identity = json.dumps(
                            [tool["source_id"], tool["capability_id"]],
                            separators=(",", ":"),
                        )
                        tool["identity"] = identity
                        tool["matches_filter"] = identity == normalized.get("tool")
                    if normalized.get("tool") and not any(
                        t["matches_filter"] for t in tools
                    ):
                        continue
                    row["at"] = row["started_at"] or row["accepted_at"]
                    row["time_basis"] = (
                        "started_at" if row["started_at"] else "accepted_at"
                    )
                    coverage = []
                    try:
                        instant = datetime.fromisoformat(row["at"])
                        if instant.tzinfo is None:
                            raise ValueError("naive_time")
                    except ValueError, TypeError:
                        instant = None
                        row["at"], row["time_basis"] = None, "unrecorded"
                        coverage.append("time_unrecorded")
                        if normalized["start"]:
                            unresolved_membership.append(row["run_id"])
                            captured_bytes += len(row["run_id"].encode()) + 64
                            if captured_bytes > self.max_bytes:
                                raise AccountingError("snapshot_quota")
                            continue
                    if (
                        normalized["start"]
                        and instant is not None
                        and not (
                            datetime.fromisoformat(normalized["start"])
                            <= instant
                            < datetime.fromisoformat(normalized["end"])
                        )
                    ):
                        continue
                    raw = row.pop("telemetry")
                    captured_bytes += len(raw.encode()) if isinstance(raw, str) else 0
                    captured_bytes += len(json.dumps(tools).encode()) + 512
                    if captured_bytes > self.max_bytes:
                        raise AccountingError("snapshot_quota")
                    try:
                        telemetry = json.loads(raw) if raw else {}
                        attempts = telemetry.get("attempts")
                        if not isinstance(attempts, list):
                            raise ValueError("missing_attempts")
                    except ValueError, TypeError, AttributeError:
                        telemetry, attempts = {}, []
                        coverage.append("telemetry_unreadable_or_missing")
                    if (
                        telemetry.get("accounting_version") != 1
                        or row["outcome"] in ("failed", "interrupted")
                    ) and not attempts:
                        coverage.append("attempt_count_unconfirmed")
                    if row["phase"] != "finished":
                        coverage.append(
                            "recording_pending"
                            if row["run_id"] not in recording_failed
                            else "recording_failed"
                        )
                    if (
                        row["run_id"] in recording_failed
                        and "recording_failed" not in coverage
                    ):
                        coverage.append("recording_failed")
                    if telemetry.get("accounting_version") != 1:
                        coverage.append("historic_tool_metering_unrecorded")
                    pruned = conn.execute(
                        "SELECT prune_reason FROM trace_prunes WHERE run_id=?",
                        (row["run_id"],),
                    ).fetchone()
                    row.update(
                        tools=tools,
                        coverage=coverage,
                        raw_attempts=attempts,
                        trace_incomplete=telemetry.get("trace_incomplete")
                        if type(telemetry.get("trace_incomplete")) is bool
                        else None,
                        prune_reason=pruned[0] if pruned else None,
                    )
                    rows.append(row)
            identities = set()
            for row in rows:
                for raw in row["raw_attempts"]:
                    model = raw.get("model") if isinstance(raw, dict) else None
                    if isinstance(model, dict) and all(
                        type(model.get(k)) is str for k in ("endpoint_id", "model_id")
                    ):
                        identities.add((model["endpoint_id"], model["model_id"]))
            frozen = FrozenPrices(prices, identities)
            at = format_instant(now)
            for row in rows:
                row["attempts"] = []
                seen = set()
                for raw in row.pop("raw_attempts"):
                    try:
                        attempt = safe_attempt(raw, frozen, at)
                        if attempt["attempt_id"] in seen:
                            raise ValueError("duplicate_attempt")
                        seen.add(attempt["attempt_id"])
                        row["attempts"].append(attempt)
                    except ValueError, TypeError, KeyError:
                        row["coverage"].append("damaged_attempt")
            result = {
                "snapshot_id": uuid.uuid4().hex,
                "process_instance_id": self.process_id,
                "computed_at": at,
                "filters": normalized,
                "expires_at": format_instant(now + timedelta(seconds=self.ttl)),
                "summary": summary(rows),
                "unresolved_membership": unresolved_membership,
                "runs": rows,
                "history_boundary": "Persistent Runs only; legacy messages excluded.",
            }
            result["price_version"] = hashlib.sha256(
                repr(frozen.values).encode()
            ).hexdigest()
            encoded = json.dumps(result, ensure_ascii=False).encode()
            if (
                len(encoded) + sum(len(v[1]) for v in self.snapshots.values())
                > self.max_bytes
            ):
                raise AccountingError("snapshot_quota")
            self.snapshots[result["snapshot_id"]] = (
                self.clock.monotonic() + self.ttl,
                encoded,
            )
            return self.page(result["snapshot_id"])

    def _expire(self):
        now = self.clock.monotonic()
        for key in list(self.snapshots):
            if self.snapshots[key][0] <= now:
                del self.snapshots[key]

    def get(self, identity):
        with self.lock:
            self._expire()
            if identity not in self.snapshots:
                raise AccountingError("snapshot_expired")
            return json.loads(self.snapshots[identity][1])

    def page(self, identity, offset=0):
        if type(offset) is not int or offset < 0:
            raise AccountingError("invalid_cursor")
        value = self.get(identity)
        rows = value.pop("runs")
        value["runs"] = [
            {k: v for k, v in row.items() if k not in ("attempts", "tools")}
            for row in rows[offset : offset + 50]
        ]
        value["next_offset"] = offset + 50 if offset + 50 < len(rows) else None
        return value

    def detail(self, identity, run_id):
        value = self.get(identity)
        row = next((r for r in value.pop("runs") if r["run_id"] == run_id), None)
        if row is None:
            raise AccountingError("run_not_in_snapshot")
        return {**value, "run": row}
