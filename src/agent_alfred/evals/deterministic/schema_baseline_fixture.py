"""Independent-process SQLite observations for the pinned #35 baseline."""

import importlib
import inspect
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from agent_alfred import schema


def snapshot(conn, old_versions):
    objects = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    tables = {}
    for kind, name, _, _ in objects:
        if kind != "table":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        rows = conn.execute(f"SELECT * FROM {quoted}").fetchall()
        if name == "schema_migrations":
            for version, applied_at in rows:
                if version > old_versions:
                    parsed = datetime.fromisoformat(applied_at)
                    assert parsed.utcoffset() == timedelta(0), applied_at
                    assert applied_at.endswith("Z"), applied_at
            rows = [
                (v, stamp if v <= old_versions else "<new-aware-UTC>")
                for v, stamp in rows
            ]
        # repr preserves every string and blob byte, while sorting ignores row
        # scan order (not a SQL promise). No business field is normalized.
        tables[name] = sorted(repr(row) for row in rows)
    fts = {
        "facts": conn.execute(
            "SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'tea' ORDER BY rowid"
        ).fetchall(),
        "episodes": conn.execute(
            "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH 'dinner' "
            "ORDER BY rowid"
        ).fetchall(),
    }
    return {"objects": objects, "tables": tables, "fts": fts}


def prepare(directory):
    # Imported only in the pinned baseline process, before candidate observation.
    from agent_alfred.evals.deterministic.test_schema import (
        _historic_database,
        _seed_version_1_rows,
    )

    registry = schema.MIGRATIONS
    cases = [("empty", 0, None)]
    cases += [(commit, 1, commit) for commit in ("40f7f98", "ae253b2", "6d7659c")]
    cases += [(f"v{v}", v, "6d7659c") for v in range(2, 19)]
    for name, version, commit in cases:
        conn = _historic_database(commit) if commit else sqlite3.connect(":memory:")
        if commit:
            _seed_version_1_rows(conn, commit)
            for session_id, stamp in [("", "raw time"), ("历史会话", "1999-01-01")]:
                conn.execute(
                    "INSERT INTO agent_log "
                    "(session_id, role, content, source, created_at) "
                    "VALUES (?, 'user', '[]', 'cli', ?)",
                    (session_id, stamp),
                )
            conn.commit()
        if version >= 2:
            schema.MIGRATIONS = registry[:version]
            schema.migrate(conn)
        if version >= 3:
            revision = schema.allocate_activity_revision(conn)
            conn.execute(
                "INSERT INTO runs (run_id, purpose, session_id, gateway, phase, "
                "outcome, accepted_at, activity_revision, telemetry) VALUES "
                "('preserved-run', 'chat', 's1', 'cli', 'accepted', NULL, "
                "'arbitrary time', ?, '{\"trace_incomplete\":false}')",
                (revision,),
            )
        if version >= 12:
            conn.execute("CREATE TABLE custom_audit (run_id TEXT)")
            conn.execute("CREATE INDEX custom_runs_index ON runs (accepted_at)")
            conn.execute(
                "CREATE TRIGGER custom_runs_trigger AFTER UPDATE ON runs BEGIN "
                "INSERT INTO custom_audit VALUES (new.run_id); END"
            )
        conn.commit()
        target = sqlite3.connect(directory / f"{name}.sqlite")
        conn.backup(target)
        target.close()
        conn.close()
    schema.MIGRATIONS = registry
    return cases


def observe(directory, cases, output):
    results = {}
    for name, version, _ in cases:
        path = output / f"{name}.sqlite"
        shutil.copyfile(directory / f"{name}.sqlite", path)
        conn = sqlite3.connect(path)
        schema.migrate(conn)
        first = snapshot(conn, version)
        # All actual ledger timestamps, including this invocation's new rows,
        # are preserved by the second invocation, not just normalized for equality.
        before_repeat = snapshot(conn, 18)
        schema.migrate(conn)
        assert snapshot(conn, 18) == before_repeat, name
        if version >= 12:
            conn.execute("UPDATE runs SET accepted_at=accepted_at")
            assert conn.execute("SELECT * FROM custom_audit").fetchall() == [
                ("preserved-run",),
            ], name
            conn.rollback()
        results[name] = first
        conn.close()
    return {"sqlite_version": sqlite3.sqlite_version, "databases": results}


def contract():
    namespaces = [vars(schema)]
    if hasattr(schema, "_run_migrations"):
        namespaces += [
            vars(importlib.import_module("agent_alfred._schema." + name))
            for name in ("contracts", "migrations", "runner")
        ]
    constants = {}
    for namespace in namespaces:
        for name, value in namespace.items():
            if name.isupper() and isinstance(value, (str, int, tuple, frozenset, dict)):
                if name == "MIGRATIONS":
                    value = [
                        (m.version, m.apply.__name__, m.managed_objects) for m in value
                    ]
                elif isinstance(value, frozenset):
                    value = sorted(value)
                constants[name] = value
    names = (
        "migrate configure_connection insert_session insert_accepted_run "
        "allocate_activity_revision update_run_phase parse_instant "
        "pick_prune_reason record_trace_prune"
    ).split()
    signatures = {}
    for name in names:
        sig = inspect.signature(getattr(schema, name))
        signatures[name] = [
            (p.name, str(p.kind), repr(p.default)) for p in sig.parameters.values()
        ]
    from agent_alfred.evals.deterministic import historic_schema

    fixtures = {
        name: getattr(historic_schema, name)
        for name in (
            "V1_40F7F98",
            "V1_AE253B2",
            "V1_6D7659C",
        )
    }
    return {"constants": constants, "signatures": signatures, "fixtures": fixtures}


if __name__ == "__main__":
    mode, directory, output = sys.argv[1:]
    directory, output = Path(directory), Path(output)
    output.mkdir(parents=True)
    if mode == "baseline":
        directory.mkdir()
        cases = prepare(directory)
        (directory / "cases.json").write_text(json.dumps(cases))
    else:
        cases = json.loads((directory / "cases.json").read_text())
    result = {**observe(directory, cases, output), "contract": contract()}
    (output / "observations.json").write_text(json.dumps(result, ensure_ascii=False))
