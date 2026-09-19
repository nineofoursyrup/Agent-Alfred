"""Second review boundaries: actual owners, source publication and Gate facts."""

import inspect
import json
import os
import sys
import threading
import zipfile

import pytest

from agent_alfred.evals.deterministic.ops_support import GATE, ops_host, run
from agent_alfred.evals.deterministic.test_runtime_memory_gate import save_fact
from agent_alfred.evals.deterministic.test_trace_export import contents, export
from agent_alfred.evals.deterministic.test_trace_export_limits import limit_host
from agent_alfred.trace_export.errors import ExportError
from agent_alfred.trace_export.leases import BundleLeases
from agent_alfred.trace_export.service import TraceExports


@pytest.mark.parametrize(
    "stage", ["lease_return", "thread_constructed", "thread_started"]
)
def test_std03_interrupted_start_retains_owner_and_releases_reader(tmp_path, stage):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        hit = False

        def interrupt(frame, event, arg):
            nonlocal hit
            if event != "return" or hit:
                return interrupt
            obj = frame.f_locals.get("self")
            matches = (
                stage == "lease_return"
                and frame.f_code is BundleLeases.acquire.__code__
                or stage == "thread_constructed"
                and frame.f_code is threading.Thread.__init__.__code__
                and obj.name == "trace-export"
                or stage == "thread_started"
                and frame.f_code is threading.Thread.start.__code__
                and obj.name == "trace-export"
            )
            if event == "return" and matches and not hit:
                hit = True
                raise KeyboardInterrupt("fixture start ownership boundary")
            return interrupt

        try:
            sys.settrace(interrupt)
            with pytest.raises(KeyboardInterrupt):
                service.start(rid)
        finally:
            sys.settrace(None)
        assert hit
        if service._task is not None:
            service.cancel(service._task.task_id)
            if (
                service._task.thread is not None
                and service._task.thread.ident is not None
            ):
                service._task.thread.join(5)
            service.status(service._task.task_id)
        with service.leases.deleting(rid):
            pass
        next_task = service.wait(service.start(rid)["task_id"])
        assert next_task["state"] == "ready", next_task
        assert service.cancel(next_task["task_id"])["cleanup"] == "released"
        assert host.close()
        with service.leases.deleting(rid):
            pass


@pytest.mark.parametrize("name", ["gate.evaluated", "graph.started"])
@pytest.mark.parametrize("version", [999, 0, True, "1", None])
def test_spec04_unknown_event_version_rejects_entire_export(tmp_path, name, version):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        records = [json.loads(line) for line in path.read_text().splitlines()]
        event = next(e for e in records if e["payload_name"] == "gate.evaluated")
        if name == "graph.started":
            event["payload_name"] = name
            event["payload"] = {
                "name": name,
                "trace_policy": "persist",
                "graph_id": "fixture-graph",
                "topology_hash": "fixture-hash",
            }
        event["payload"]["schema_version"] = version
        path.write_text("".join(json.dumps(e) + "\n" for e in records))
        _, task = export(host, tmp_path, rid)
        assert task["reason"] == "unsupported_format", task
        assert task["cleanup"] == "released"
        assert task["download_token"] is None


def test_spec05_http_source_replacement_after_zip_sync_never_publishes_ready(
    tmp_path, monkeypatch
):
    with limit_host(tmp_path, [GATE, "done"], tools=()) as (host, http):
        rid, _ = run(host)
        trace = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        entered, release = threading.Event(), threading.Event()
        sync = os.fsync

        def after_sync(fd):
            sync(fd)
            task = http.service._task
            if task and task.output is not None and fd == task.output.fd:
                entered.set()
                assert release.wait(5)

        monkeypatch.setattr(os, "fsync", after_sync)
        task = http.start(rid)
        try:
            assert entered.wait(5)
            archive = next((tmp_path / "state/trace-exports").glob("*/archive.zip"))
            with zipfile.ZipFile(archive) as package:
                assert package.testzip() is None
            replacement = trace.with_suffix(".replacement")
            replacement.write_bytes(trace.read_bytes())
            replacement.replace(trace)
        finally:
            release.set()
        result = http.wait(task["task_id"])
        assert result["reason"] == "source_changed", result
        assert result["cleanup"] == "released"
        assert result["download_token"] is None


@pytest.mark.parametrize("outcome", ["skip", "hit", "miss", "error"])
@pytest.mark.parametrize("mode", ["share", "diagnostic"])
def test_spec06_real_gate_results_and_finite_duration_survive_export(
    tmp_path, outcome, mode
):
    retrieve = (
        '{"retrieve":true,"query":"coriander","reason_code":"personal_information"}'
    )
    with ops_host(tmp_path, [GATE if outcome == "skip" else retrieve, "done"]) as (
        host,
        _,
        conn,
        clock,
    ):
        if outcome == "hit":
            save_fact(host)
        elif outcome == "error":
            # Real SQLite retrieval fails while Run recording remains operational.
            conn.execute("DROP TABLE facts_fts")
            conn.commit()
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        original = path.read_bytes()
        source = next(
            json.loads(e)["payload"]
            for e in original.splitlines()
            if json.loads(e)["payload_name"] == "gate.evaluated"
        )
        assert source["outcome"] == outcome
        assert type(source["latency_ms"]) is float
        service, task = export(host, tmp_path, rid, mode)
        assert task["state"] == "ready", task
        output = contents(service, task)
        gate = next(
            json.loads(e)["payload"]
            for e in output["bundle/trace.jsonl"].splitlines()
            if json.loads(e)["payload_name"] == "gate.evaluated"
        )
        assert gate["outcome"] == source["outcome"]
        assert gate["latency_ms"] == source["latency_ms"]
        assert gate["timing"] == source["timing"]
        assert path.read_bytes() == original


@pytest.mark.parametrize("value", [0, 7, 1.25, -1, True, "1", None])
@pytest.mark.parametrize("mode", ["share", "diagnostic"])
def test_spec06_duration_domain_is_checked_before_publication(tmp_path, value, mode):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        events = [json.loads(e) for e in path.read_text().splitlines()]
        next(e for e in events if e["payload_name"] == "gate.evaluated")["payload"][
            "latency_ms"
        ] = value
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        service, task = export(host, tmp_path, rid, mode)
        assert task["state"] == "ready", task
        output = contents(service, task)
        gate = next(
            json.loads(e)["payload"]
            for e in output["bundle/trace.jsonl"].splitlines()
            if json.loads(e)["payload_name"] == "gate.evaluated"
        )
        if type(value) in (int, float) and value >= 0:
            assert gate["latency_ms"] == value
        else:
            assert "latency_ms" not in gate


@pytest.mark.parametrize("mode", ["share", "diagnostic"])
def test_spec06_known_tool_graph_and_stop_results_survive(tmp_path, mode):
    from agent_alfred.evals.deterministic.test_runtime_tools import calls
    from agent_alfred.graph import GraphBuilder, NodeOutcome, TerminalSpec, fn_node
    from agent_alfred.graph.context import GraphRunContext
    from agent_alfred.loop.budget import RunBudget
    from agent_alfred.messages import TextBlock, ToolCallBlock
    from agent_alfred.model import (
        AttemptRecord,
        ModelRef,
        ModelResponse,
        ModelResult,
        Usage,
    )
    from agent_alfred.tools import Tool, ToolSuccess
    from agent_alfred.tools.calendar import object_schema

    builder = GraphBuilder("export-positive-graph")
    builder.add_node(
        "answer",
        fn_node(lambda s, c: NodeOutcome({"reply": "done"})),
        writes=("reply",),
        terminal=TerminalSpec.result("reply"),
    )
    graph = builder.compile()

    def body(args, context):
        result = graph.invoke(
            {},
            context=GraphRunContext(
                budget=RunBudget(0),
                run_id=context.run_id,
                session_id=context.session_id,
                clock=clock,
                events=host._fanout,
            ),
        )
        return ToolSuccess((TextBlock(result.output),))

    tool = Tool("graph", "fixture", object_schema({}), body, "local_read")
    answer = ModelResult(
        (AttemptRecord("answer", False, "committed", Usage()),),
        ModelResponse((TextBlock("done"),), "refusal", ModelRef("fixture", "model")),
        None,
    )
    with ops_host(
        tmp_path,
        [GATE, calls(ToolCallBlock("graph-call", "graph", {})), answer],
        tools=(tool,),
    ) as (host, _, _, clock):
        rid, _ = run(host)
        path = next((tmp_path / "traces").glob("*/*/trace.jsonl"))
        original = path.read_bytes()
        service, task = export(host, tmp_path, rid, mode)
        assert task["state"] == "ready", task
        output = contents(service, task)
        events = {
            e["seq"]: e
            for line in output["bundle/trace.jsonl"].splitlines()
            if (e := json.loads(line))
        }
        observed = set()
        for line in original.splitlines():
            event = json.loads(line)
            for key in ("outcome", "stop_reason", "effect", "schema_version"):
                if key in event["payload"]:
                    assert events[event["seq"]]["payload"][key] == event["payload"][key]
                    observed.add((event["payload_name"], key, event["payload"][key]))
        assert {
            ("graph.started", "schema_version", 1),
            ("graph.finished", "outcome", "Completed"),
            ("tool.finished", "outcome", "ok"),
            ("step.finished", "stop_reason", "refusal"),
        } <= observed
        assert path.read_bytes() == original


@pytest.mark.parametrize(
    "stage", ["before_send_clock", "before_source_verify", "write"]
)
def test_std04_interrupted_download_retires_actual_sender(tmp_path, stage):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, ready = export(host, tmp_path, rid)
        task = service._task
        method = (
            TraceExports._send
            if stage == "before_source_verify"
            else TraceExports.download
        )
        lines, first = inspect.getsourcelines(method)
        anchor = (
            "task.source.verify()"
            if stage == "before_source_verify"
            else "task.send_at ="
        )
        boundary = first + next(i for i, line in enumerate(lines) if anchor in line)
        interrupted = False

        def interrupt(frame, event, arg):
            nonlocal interrupted
            if (
                stage != "write"
                and event == "line"
                and frame.f_code is method.__code__
                and frame.f_lineno == boundary
            ):
                interrupted = True
                raise KeyboardInterrupt("fixture sender handoff")
            return interrupt

        try:
            with (tmp_path / "user-download.zip").open("wb") as output:

                def write(raw):
                    nonlocal interrupted
                    assert output.write(raw[:64]) == 64
                    interrupted = True
                    raise KeyboardInterrupt("fixture after actual destination write")

                try:
                    sys.settrace(interrupt)
                    with pytest.raises(KeyboardInterrupt):
                        service.download(
                            ready["task_id"], ready["download_token"], write
                        )
                finally:
                    sys.settrace(None)
            assert interrupted
            result = service.status(ready["task_id"])
            assert result["state"] == "failed", result
            assert result["reason"] == "io_failed", result
            assert result["cleanup"] == "released", result
            assert not list((tmp_path / "state/trace-exports").glob("*/archive.zip"))
            with service.leases.deleting(rid):
                pass
            service.cancel(ready["task_id"])
            successor = service.wait(service.start(rid)["task_id"])
            assert successor["state"] == "ready", successor
            assert "manifest.json" in contents(service, successor)
            assert host.close()
            with service.leases.deleting(rid):
                pass
        finally:
            sys.settrace(None)
            # Only failing pre-fix probes need this teardown: the download call
            # has already exited. It never contributes to the assertions above.
            if task.cleanup != "released":
                task.sending = False
                service.cancel(task.task_id)


@pytest.mark.parametrize("replay", [False, True])
def test_std04_rejected_request_cannot_retire_active_sender(tmp_path, replay):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, ready = export(host, tmp_path, rid)
        entered, release = threading.Event(), threading.Event()
        errors = []
        destination = tmp_path / "accepted-download.zip"

        def send():
            try:
                with destination.open("wb") as output:
                    writes = 0

                    def write(raw):
                        nonlocal writes
                        writes += 1
                        if writes == 1:
                            return output.write(raw[:64])
                        if writes == 2:
                            raise BlockingIOError()
                        return output.write(raw)

                    def wait_writable():
                        entered.set()
                        assert release.wait(5)

                    service.download(
                        ready["task_id"],
                        ready["download_token"],
                        write,
                        wait_writable=wait_writable,
                    )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=send)
        thread.start()
        try:
            assert entered.wait(5)
            with pytest.raises(ExportError, match="credential_invalid"):
                service.download(
                    ready["task_id"],
                    ready["download_token"] if replay else "wrong-token",
                    lambda raw: pytest.fail("rejected request sent bytes"),
                )
            result = service.status(ready["task_id"])
            assert result["state"] == "sending", result
            assert result["reason"] is None
            assert result["cleanup"] == "pending"
            with pytest.raises(ExportError, match="busy"):
                with service.leases.deleting(rid):
                    pass
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert errors == []
        with zipfile.ZipFile(destination) as package:
            assert package.testzip() is None
        assert service.status(ready["task_id"])["state"] == "transferred"


def test_std04_interrupted_sender_retirement_is_publicly_retryable(tmp_path):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, ready = export(host, tmp_path, rid)
        task = service._task
        lines, first = inspect.getsourcelines(TraceExports.download)
        boundary = first + max(
            i for i, line in enumerate(lines) if "with self._lock:" in line
        )
        hit = False

        def interrupt(frame, event, arg):
            nonlocal hit
            if (
                not hit
                and event == "line"
                and frame.f_code is TraceExports.download.__code__
                and frame.f_lineno == boundary
            ):
                hit = True
                raise KeyboardInterrupt("fixture sender retirement")
            return interrupt

        destination = tmp_path / "user-download.zip"
        try:
            with destination.open("wb") as output:
                try:
                    sys.settrace(interrupt)
                    with pytest.raises(KeyboardInterrupt):
                        service.download(
                            ready["task_id"], ready["download_token"], output.write
                        )
                finally:
                    sys.settrace(None)
            assert hit
            with zipfile.ZipFile(destination) as package:
                assert package.testzip() is None
                assert "manifest.json" in package.namelist()
            result = service.cancel(ready["task_id"])
            assert result["cleanup"] == "released", result
            assert not list((tmp_path / "state/trace-exports").glob("*/archive.zip"))
            with service.leases.deleting(rid):
                pass
            successor = service.wait(service.start(rid)["task_id"])
            assert successor["state"] == "ready", successor
            assert "manifest.json" in contents(service, successor)
            assert host.close()
            assert destination.exists()
        finally:
            sys.settrace(None)
            if task.cleanup != "released":
                # Failed pre-fix observation is complete; isolated fixture only.
                task.sending = False
                service.cancel(task.task_id)


def test_exited_generator_retirement_is_publicly_retryable(tmp_path, monkeypatch):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        meta = next((tmp_path / "traces").glob("*/*/meta.json"))
        original = meta.read_bytes()
        data = json.loads(original)
        data["schema_version"] = 999
        meta.write_text(json.dumps(data))
        service = host.attach_trace_exports(tmp_path / "state", tmp_path / "traces")
        lines, first = inspect.getsourcelines(TraceExports._generate)
        boundary = first + max(
            i for i, line in enumerate(lines) if "task.generating = False" in line
        )
        ended = threading.Event()
        observed = []
        previous = threading.excepthook

        def hook(args):
            if (
                args.exc_type is KeyboardInterrupt
                and args.thread.name == "trace-export"
            ):
                observed.append(args.exc_type)
                ended.set()
            else:
                previous(args)

        def interrupt(frame, event, arg):
            if (
                event == "line"
                and frame.f_code is TraceExports._generate.__code__
                and frame.f_lineno == boundary
            ):
                raise KeyboardInterrupt("fixture generation retirement")
            return interrupt

        monkeypatch.setattr(threading, "excepthook", hook)
        try:
            try:
                threading.settrace(interrupt)
                accepted = service.start(rid)
                assert ended.wait(5)
            finally:
                threading.settrace(None)
            task = service._task
            task.thread.join(5)
            assert not task.thread.is_alive()
            assert observed == [KeyboardInterrupt]
            result = service.cancel(accepted["task_id"])
            assert result["cleanup"] == "released", result
            with service.leases.deleting(rid):
                pass
            meta.write_bytes(original)
            successor = service.wait(service.start(rid)["task_id"])
            assert successor["state"] == "ready", successor
            assert "manifest.json" in contents(service, successor)
            assert host.close()
        finally:
            threading.settrace(None)
            if service._task.cleanup != "released":
                service._task.generating = False
                service.cancel(service._task.task_id)


@pytest.mark.parametrize("anchor", ["task.sender.close()", "task.sender = None"])
def test_sender_frame_close_interruption_keeps_retryable_owner(tmp_path, anchor):
    with ops_host(tmp_path, [GATE, "done"]) as (host, *_):
        rid, _ = run(host)
        service, ready = export(host, tmp_path, rid)
        lines, first = inspect.getsourcelines(TraceExports._cleanup)
        boundary = first + next(i for i, line in enumerate(lines) if anchor in line)
        hit = False

        def interrupt(frame, event, arg):
            nonlocal hit
            if (
                not hit
                and event == "line"
                and frame.f_code is TraceExports._cleanup.__code__
                and frame.f_lineno == boundary
            ):
                hit = True
                raise KeyboardInterrupt("fixture actual frame close boundary")
            return interrupt

        destination = tmp_path / "user-download.zip"
        try:
            with destination.open("wb") as output:
                sys.settrace(interrupt)
                service.download(
                    ready["task_id"], ready["download_token"], output.write
                )
        finally:
            sys.settrace(None)
        assert hit
        with zipfile.ZipFile(destination) as package:
            assert package.testzip() is None
        observed = service.status(ready["task_id"])
        assert observed["cleanup"] == "failed", observed
        assert observed["state"] == "cleaning", observed
        with pytest.raises(ExportError, match="busy"):
            with service.leases.deleting(rid):
                pass
        assert host.close()
        assert service.status(ready["task_id"])["cleanup"] == "released"
        with service.leases.deleting(rid):
            pass
        assert destination.exists()
