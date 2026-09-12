"""Body-free request accounting, independent of trace and business receipts."""

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from agent_alfred.clock import format_instant


class MeteringError(RuntimeError):
    pass


def migrate_metering(conn):
    conn.execute("""CREATE TABLE tool_metering (
        run_id TEXT NOT NULL, step_index INTEGER NOT NULL, call_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL, tool_name TEXT NOT NULL,
        source_id TEXT, capability_id TEXT, effect TEXT, requested_at TEXT NOT NULL,
        start_confirmation TEXT NOT NULL, result TEXT NOT NULL, reason TEXT,
        finished_at TEXT, cost TEXT NOT NULL, operation_id TEXT,
        model_delivery TEXT NOT NULL DEFAULT 'unconfirmed',
        PRIMARY KEY(run_id,step_index,call_id))""")
    conn.execute("""CREATE TABLE tool_metering_health (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)""")
    conn.execute("INSERT INTO tool_metering_health VALUES (1,0)")
    conn.execute("""CREATE TABLE tool_operation_verifications (
        operation_id TEXT PRIMARY KEY, state TEXT NOT NULL, verified_at TEXT NOT NULL,
        evidence_source TEXT NOT NULL, related_run TEXT)""")


class ToolMetering:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.failed = threading.Event()

    @contextmanager
    def writing(self):
        try:
            with self.store.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
        except BaseException as exc:
            self.failed.set()
            if not isinstance(exc, Exception):
                raise
            raise MeteringError("metering_unconfirmed") from exc

    def register(self, calls, context, declarations):
        if self.failed.is_set():
            raise MeteringError("metering_unavailable")
        if len({c.id for c in calls}) != len(calls) or any(
            not isinstance(c.id, str) or not c.id for c in calls
        ):
            raise MeteringError("tool_identity_ambiguous")
        with self.writing() as conn:
            for ordinal, call in enumerate(calls):
                tool = declarations.get(call.name)
                identity = (context.run_id, context.step_index, call.id)
                old = conn.execute(
                    "SELECT tool_name,source_id,capability_id FROM tool_metering "
                    "WHERE run_id=? AND step_index=? AND call_id=?",
                    identity,
                ).fetchone()
                owner = (
                    call.name,
                    tool.source_id if tool else None,
                    tool.capability_id if tool else None,
                )
                if old is not None:
                    if tuple(old) != owner:
                        raise MeteringError("tool_identity_ambiguous")
                    continue
                conn.execute(
                    """INSERT INTO tool_metering
                    (run_id,step_index,call_id,ordinal,tool_name,source_id,
                     capability_id,effect,requested_at,start_confirmation,result,cost)
                    VALUES (?,?,?,?,?,?,?,?,?,'not_started','pending',?)""",
                    (
                        *identity,
                        ordinal,
                        *owner,
                        tool.effect if tool else None,
                        format_instant(self.clock.wall_utc()),
                        json.dumps({"kind": "not_billable"}),
                    ),
                )

    def prior(self, context):
        rows = self.read(context.run_id)
        return next(
            (
                r
                for r in rows
                if r["step_index"] == context.step_index
                and r["call_id"] == context.call_id
                and r["result"] != "pending"
            ),
            None,
        )

    def stop_run(self, run_id, reason):
        with self.writing() as conn:
            conn.execute(
                "UPDATE tool_metering SET result='not_executed',reason=? "
                "WHERE run_id=? AND result='pending'",
                (reason, run_id),
            )

            conn.execute(
                "UPDATE tool_metering SET model_delivery='not_sent' WHERE run_id=? "
                "AND step_index=(SELECT MAX(step_index) FROM tool_metering "
                "WHERE run_id=?)",
                (run_id, run_id),
            )

    def not_sent(self, context):
        with self.writing() as conn:
            conn.execute(
                "UPDATE tool_metering SET model_delivery='not_sent' "
                "WHERE run_id=? AND step_index=?",
                (context.run_id, context.step_index),
            )

    def intent(self, context):
        with self.writing() as conn:
            conn.execute(
                """UPDATE tool_metering SET start_confirmation='unconfirmed',
                result='unknown',cost=? WHERE run_id=? AND step_index=? AND call_id=?
                AND result='pending' """,
                (json.dumps({"kind": "unknown"}), *self.key(context)),
            )

    @staticmethod
    def key(context):
        return context.run_id, context.step_index, context.call_id

    def finish(
        self, context, *, entered, result, reason, cost, operation_id=None, conn=None
    ):
        def write(target):
            target.execute(
                """UPDATE tool_metering SET start_confirmation=?,
                result=?,reason=?,cost=?,operation_id=COALESCE(?,operation_id),
                finished_at=? WHERE run_id=? AND step_index=? AND call_id=?
                AND finished_at IS NULL""",
                (
                    "confirmed" if entered else "not_started",
                    result,
                    reason,
                    json.dumps(cost),
                    operation_id,
                    format_instant(self.clock.wall_utc()),
                    *self.key(context),
                ),
            )

        if conn is not None:
            try:
                self.store.validate_borrowed_transaction(conn)
                write(conn)
            except BaseException as exc:
                self.failed.set()
                if not isinstance(exc, Exception):
                    raise
                raise MeteringError("metering_unconfirmed") from exc
        else:
            with self.writing() as target:
                write(target)

    def atomic_success(self, context, conn, operation_id=None):
        self.finish(
            context,
            entered=True,
            result="succeeded",
            reason=None,
            cost={"kind": "not_billable"},
            operation_id=operation_id,
            conn=conn,
        )

    def associate_operation(self, context, conn, operation_id):
        """Bind the request in the same transaction as durable business intent."""
        try:
            self.store.validate_borrowed_transaction(conn)
            changed = conn.execute(
                "UPDATE tool_metering SET operation_id=? "
                "WHERE run_id=? AND step_index=? AND call_id=?",
                (operation_id, *self.key(context)),
            ).rowcount
            if changed != 1:
                raise ValueError("missing_metering_request")
        except BaseException as exc:
            self.failed.set()
            if not isinstance(exc, Exception):
                raise
            raise MeteringError("metering_unconfirmed") from exc

    def stopped(self, calls, context, reason):
        from dataclasses import replace

        for call in calls:
            self.finish(
                replace(context, call_id=call.id),
                entered=False,
                result="not_executed",
                reason=reason,
                cost={"kind": "not_billable"},
            )
        with self.writing() as conn:
            conn.execute(
                "UPDATE tool_metering SET model_delivery='not_sent' "
                "WHERE run_id=? AND step_index=?",
                (context.run_id, context.step_index),
            )

    def read(self, run_id, *, conn=None):
        from agent_alfred.tools.cost import read_tool_cost

        def query(target):
            cursor = target.execute(
                "SELECT * FROM tool_metering WHERE run_id=? "
                "ORDER BY step_index,ordinal",
                (run_id,),
            )
            names = [d[0] for d in cursor.description]
            rows = []
            for row in cursor:
                value = dict(zip(names, row, strict=True))
                try:
                    value["cost"] = read_tool_cost(
                        json.loads(value["cost"]), value["source_id"]
                    )
                except ValueError, TypeError:
                    value["cost"] = {"kind": "unrecorded"}
                rows.append(value)
            return rows

        if conn is not None:
            return query(conn)
        with self.store.reading() as target:
            return query(target)

    def recover(self):
        # A durable pre-dispatch intent is deliberately left unconfirmed.
        with self.writing() as conn:
            # Exercise registration and every settlement state, without invoking
            # a capability. The transient probe never enters the public ledger.
            probe = SimpleNamespace(
                run_id="metering-check-" + uuid4().hex, step_index=0, call_id="health"
            )
            conn.execute(
                "INSERT INTO tool_metering (run_id,step_index,call_id,ordinal,"
                "tool_name,requested_at,start_confirmation,result,cost) "
                "VALUES (?,0,'health',0,'health',?,'not_started','pending','{}')",
                (probe.run_id, format_instant(self.clock.wall_utc())),
            )
            for result in ("succeeded", "failed", "unknown", "not_executed"):
                self.finish(
                    probe,
                    entered=result != "not_executed",
                    result=result,
                    reason=None,
                    cost={"kind": "not_billable"},
                    conn=conn,
                )
                conn.execute(
                    "UPDATE tool_metering SET finished_at=NULL WHERE run_id=?",
                    (probe.run_id,),
                )
            conn.execute("DELETE FROM tool_metering WHERE run_id=?", (probe.run_id,))
            # A Run's terminal telemetry can retain the no-send fact even when
            # the original tool-row update could not be persisted.
            for run_id, raw in conn.execute(
                "SELECT run_id,telemetry FROM runs WHERE telemetry IS NOT NULL"
            ).fetchall():
                try:
                    steps = (
                        json.loads(raw)
                        .get("memory", {})
                        .get("tool_model_not_sent_steps", [])
                    )
                    for step in steps:
                        if type(step) is int and step >= 0:
                            conn.execute(
                                "UPDATE tool_metering SET model_delivery='not_sent' "
                                "WHERE run_id=? AND step_index=?",
                                (run_id, step),
                            )
                except ValueError, TypeError, AttributeError:
                    continue
            conn.execute(
                "UPDATE tool_metering SET result='not_executed', "
                "reason='interrupted_before_dispatch' WHERE result='pending'"
            )
            conn.execute("UPDATE tool_metering_health SET revision=revision+1")
        with self.store.reading() as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                self.failed.set()
                raise MeteringError("metering_unavailable")
        self.failed.clear()
