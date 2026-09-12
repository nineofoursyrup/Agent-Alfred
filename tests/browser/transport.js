import { createHash } from "node:crypto";
import { expect } from "@playwright/test";

// The transport is the controlled system boundary. No app functions are exposed.
export async function controlledEventSource(page) {
  await page.addInitScript(() => {
    window.sources = [];
    window.EventSource = class extends EventTarget {
      constructor(url) {
        super();
        this.url = url;
        window.sources.push(this);
      }
      close() {
        this.closed = true;
      }
    };
  });
}

export async function controlledTransport(page) {
  await controlledEventSource(page);
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "消息" })).toBeEnabled();
  await expect.poll(() => page.evaluate(() => window.sources.length)).toBe(2);
  const entry = await (await page.request.get("/api/entry")).json();
  await page.route("**/api/entry", (route) =>
    route.fulfill({ json: { ...entry, instance_id: "test-process" } }),
  );
  return page.evaluate(() => sessionStorage.getItem("alfred.session"));
}

export function state(session, revision = 1, extra = {}) {
  return {
    process_instance_id: "test-process",
    state_revision: revision,
    coordinator_state: "idle",
    active_run: null,
    step: null,
    recording_state: null,
    session_valid: true,
    unrecorded_terminal_projection: null,
    ...extra,
  };
}

export function run(session, extra = {}) {
  return {
    run_id: "r1",
    purpose: "chat",
    gateway: "cli",
    phase: "running",
    outcome: null,
    session_id: session,
    prompt_preview: "终端的问题",
    started_at: "2026-09-06T08:00:00Z",
    current_step: null,
    recording_state: null,
    ...extra,
  };
}

export async function emit(page, kind, body) {
  await page.evaluate(
    ({ kind, body }) => {
      const source = window.sources.at(-1);
      source.dispatchEvent(
        kind === "error"
          ? new Event(kind)
          : new MessageEvent(kind, { data: JSON.stringify(body) }),
      );
    },
    { kind, body },
  );
}

export async function domain(
  page,
  seq,
  session,
  payload,
  { attempt = null, step = null, runId = "r1" } = {},
) {
  const envelope = {
    ts: seq,
    run_id: runId,
    session_id: session,
    step_index: step,
    attempt_id: attempt,
    node_id: null,
    source: "web",
  };
  const body = { envelope, payload };
  const eventId = `event-${seq}`;
  const digest = createHash("sha256")
    .update(
      JSON.stringify({ event: payload.name, event_id: eventId, payload: body }),
    )
    .digest("hex");
  await emit(page, "domain_event", {
    seq,
    event: payload.name,
    event_id: eventId,
    chunk_index: 0,
    chunk_count: 1,
    event_sha256: digest,
    payload: body,
  });
}
