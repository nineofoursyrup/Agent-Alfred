"""D01 structural checks, separate from public SQLite behavioral acceptance."""

import ast
import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest

from agent_alfred import database, schema
from agent_alfred.evals.deterministic import historic_schema
from agent_alfred.outcomes import RunOutcome
from agent_alfred.run_phases import RunPhase


def test_r07_historic_calendar_names_are_explicit_metadata():
    assert {
        commit: fixture.calendar_table
        for commit, fixture in historic_schema.V1_SCHEMAS.items()
    } == {"40f7f98": "events", "ae253b2": "events", "6d7659c": "calendar_entries"}


def test_d01_public_types_and_exception_identities():
    contracts = importlib.import_module("agent_alfred._schema.contracts")
    assert database.schema is schema
    for name in (
        "Migration",
        "Fts5UnavailableError",
        "SchemaVersionError",
        "RunPhaseError",
    ):
        assert getattr(schema, name) is getattr(contracts, name)
    assert schema.Migration._fields == ("version", "apply", "managed_objects")
    assert isinstance(schema.MIGRATIONS[0], tuple)
    assert schema.RunPhase is RunPhase
    assert schema.RunOutcome is RunOutcome
    hints = get_type_hints(schema.update_run_phase)
    assert hints["from_phase"] == RunPhase
    assert hints["to_phase"] == RunPhase
    assert hints["outcome"] == RunOutcome | None
    assert (
        get_type_hints(schema.record_trace_prune)["prune_reason"] == schema.PruneReason
    )


def test_d01_facade_contains_no_sql_and_internals_do_not_import_it():
    tree = ast.parse(inspect.getsource(schema))
    assert [n.name for n in tree.body if isinstance(n, ast.FunctionDef)] == ["migrate"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.lstrip().startswith(("CREATE ", "INSERT ", "UPDATE "))
    package = Path(schema.__file__).with_name("_schema")
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "agent_alfred.schema" for a in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "agent_alfred.schema", path
                assert not (
                    node.module in (None, "agent_alfred")
                    and any(a.name == "schema" for a in node.names)
                ), path


@pytest.mark.parametrize(
    "module,forbidden",
    [
        (
            "contracts",
            ("migrations", "runner", "run_records", "database", "runtime.host"),
        ),
        ("runner", ("migrations", "run_records", "database", "runtime.host")),
        ("run_records", ("migrations", "runner", "runtime.host")),
        ("pruning", ("migrations", "runner", "runtime.host")),
        ("migrations", ("run_records", "runtime.host")),
    ],
)
def test_d01_internal_imports_do_not_load_unrelated_owners(module, forbidden):
    script = f"""
import importlib, sys
importlib.import_module('agent_alfred._schema.{module}')
for name in {forbidden!r}:
    full = 'agent_alfred.' + (name if '.' in name or name == 'database'
                              else '_schema.' + name)
    assert full not in sys.modules, full
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


@pytest.mark.parametrize("entry", ["schema", "database", "runtime.host"])
def test_ce06_cold_import_then_real_empty_and_v13_upgrade(entry, tmp_path):
    script = f"""
import importlib, sqlite3
importlib.import_module('agent_alfred.{entry}')
from agent_alfred import schema
registry = schema.MIGRATIONS
for version in (0, 12):
    conn = sqlite3.connect(':memory:')
    if version:
        schema.MIGRATIONS = registry[:version]
        schema.migrate(conn)
    schema.MIGRATIONS = registry
    schema.migrate(conn)
    latest = conn.execute('SELECT max(version) FROM schema_migrations').fetchone()
    assert latest == (19,)
    conn.close()
"""
    source = Path(schema.__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        check=True,
        capture_output=True,
    )
