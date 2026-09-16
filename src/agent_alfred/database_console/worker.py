"""Isolated diagnostic worker. User SQL never touches the source."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_alfred.database_console.budget import RESULT_ROW_LIMIT
from agent_alfred.database_console.cursor import DATASET_URI, query
from agent_alfred.database_console.encode import encode_rows
from agent_alfred.database_console.errors import ConsoleError
from agent_alfred.database_console.project import fill, open_dataset, open_source
from agent_alfred.database_console.sql import (
    referenced_objects,
)
from agent_alfred.database_console.sqlite_limits import (
    apply_heap_limit,
    json_available,
    required_functions_available,
)
from agent_alfred.redact import Redactor


def _barrier(barriers: dict, stage: str, deadline: float) -> None:
    path = barriers.get(stage)
    if not path:
        return
    if time.monotonic() >= deadline:
        raise ConsoleError("query_timeout")
    ready = path + ".ready"
    if os.path.exists(ready):
        signal = os.open(ready, os.O_WRONLY)
        os.write(signal, b"1")
        os.close(signal)
    fd = os.open(path, os.O_RDONLY)
    os.close(fd)
    if time.monotonic() >= deadline:
        raise ConsoleError("query_timeout")


def _interrupt(deadline: float):
    def handler():
        return 1 if time.monotonic() >= deadline else 0

    return handler


def run(request: dict) -> dict:
    deadline = time.monotonic() + float(request["remaining_s"])
    barriers = request.get("barriers") or {}
    sql = request["sql"]
    if time.monotonic() >= deadline:
        raise ConsoleError("query_timeout")
    apply_heap_limit()
    dataset = open_dataset(DATASET_URI)
    try:
        if not json_available(dataset) or not required_functions_available(dataset):
            raise ConsoleError("database_unavailable")
        names = referenced_objects(dataset, sql)
        redactor = Redactor(
            request.get("secrets") or [],
            min_length=int(request.get("min_length", 8)),
            approved=True,
        )
        _barrier(barriers, "extract", deadline)
        source = open_source(request["db_path"], tuple(request["identity"]))
        try:
            source.execute("BEGIN")
            source.execute(
                "SELECT revision FROM memory_revision WHERE singleton=1"
            ).fetchone()
            read_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            source.set_progress_handler(_interrupt(deadline), 100)
            coverage = fill(source, dataset, names, redactor, barriers=barriers)
            source.execute("COMMIT")
        finally:
            source.close()
        if time.monotonic() >= deadline:
            raise ConsoleError("query_timeout")
        _barrier(barriers, "protect", deadline)
        _barrier(barriers, "sql", deadline)
        dataset.commit()
        with query(sql) as cursor:
            columns = cursor.columns
            _barrier(barriers, "encode", deadline)

            def rows():
                index = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise ConsoleError("query_timeout")
                    if index == RESULT_ROW_LIMIT:
                        _barrier(barriers, "peek", deadline)
                    if index == 1:
                        _barrier(barriers, "byte_peek", deadline)
                    row = cursor.fetchone()
                    if row is None:
                        return
                    index += 1
                    yield row

            envelope = {
                "query_id": request["query_id"],
                "instance_id": request["instance_id"],
                "memory_revision": request["memory_revision"],
                "protection_version": request["protection_version"],
                "objects": list(names),
                "coverage": coverage,
                "read_at": read_at,
            }
            result = encode_rows(envelope, columns, rows())
            return result
    finally:
        dataset.close()


def main() -> None:
    raw = sys.stdin.buffer.read()
    request = json.loads(raw)
    try:
        result = run(request)
    except ConsoleError as exc:
        print(json.dumps({"error": exc.code}, separators=(",", ":")), flush=True)
        return
    except MemoryError:
        print('{"error":"resource_limit"}', flush=True)
        return
    except sqlite3.Error:
        print('{"error":"sql_error"}', flush=True)
        return
    except OSError, ValueError, TypeError, KeyError:
        print(
            json.dumps({"error": "database_unavailable"}, separators=(",", ":")),
            flush=True,
        )
        return
    print(
        json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        flush=True,
    )


if __name__ == "__main__":
    main()
