"""Temporary CI-01 diagnostics; never included in the product candidate."""

import _sqlite3
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_alfred.database_console import sqlite_limits
from agent_alfred.evals.deterministic.test_database_http import (
    _catalog,
    _dashboard,
    _execute,
    _post,
)


def report(kind, value):
    print(json.dumps({"ci01": kind, "value": value}, default=str), flush=True)


report(
    "runtime",
    {
        "executable": sys.executable,
        "base_prefix": sys.base_prefix,
        "sqlite_version": sqlite3.sqlite_version,
        "extension": getattr(_sqlite3, "__file__", None),
        "extension_origin": _sqlite3.__spec__.origin,
        "LD_LIBRARY_PATH": os.environ.get("LD_LIBRARY_PATH"),
    },
)
with sqlite3.connect(":memory:") as conn:
    report("compile_options", conn.execute("PRAGMA compile_options").fetchall())
for name in ["sqlite3_hard_heap_limit64", "sqlite3_open_v2"]:
    try:
        report(
            name, str(getattr(ctypes.CDLL(getattr(_sqlite3, "__file__", None)), name))
        )
    except Exception:
        report(name, traceback.format_exc())
report("heap_supported", sqlite_limits.hard_heap_limit_supported())
for label, env in [
    ("empty", {}),
    (
        "library_only",
        {k: os.environ[k] for k in ["LD_LIBRARY_PATH"] if k in os.environ},
    ),
]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,sqlite3;print(sys.executable,sqlite3.sqlite_version)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    report(
        "python_start_" + label,
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
with TemporaryDirectory() as directory:
    dashboard = _dashboard(Path(directory))
    try:
        catalog = _catalog(dashboard)
        report(
            "catalog",
            {key: value for key, value in catalog.items() if key != "objects"},
        )
        report("issue", _post(dashboard, "/api/database/queries", {}))
        try:
            report("execute", _execute(dashboard, "SELECT 1 AS linux_probe"))
        except Exception:
            report("execute_exception", traceback.format_exc())
    finally:
        report("close", dashboard.close())
