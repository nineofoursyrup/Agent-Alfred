import { Stream } from "./stream.js";
import { node, textBlocks } from "./dom.js";
import { Progress } from "./progress.js";
import { ConnectionNotices, Announcer } from "./notices.js";
import { inbox } from "./pages.js";
import { runsPage, outcomeLabel } from "./runs.js";
/** @typedef {Record<string, any>} Wire */
/** @template {Element} T @param {string} id @returns {T} */
function element(id) {
  const result = document.getElementById(id);
  if (!result) throw new Error(`Missing element ${id}`);
  return /** @type {T} */ (/** @type {unknown} */ (result));
}
const input = /** @type {HTMLTextAreaElement} */ (element("message"));
const toggle = /** @type {HTMLButtonElement} */ (element("toggle"));
const drawer = /** @type {HTMLElement} */ (element("conversation"));
new ResizeObserver((entries) => {
  const height = entries[0]?.target.getBoundingClientRect().height;
  if (height)
    document.documentElement.style.setProperty(
      "--mainbar-height",
      `${height}px`,
    );
}).observe(element("mainbar"));
const error = element("error");
let session = sessionStorage.getItem("alfred.session");
let expanded = sessionStorage.getItem("alfred.expanded") === "true";
const narrow = matchMedia("(max-width:640px)");
/** @type {HTMLElement|null} */ let returnFocus = null;
let modal = false;
let csrf = "";
let instance = "";
let revision = -1;
let connected = false;
let valid = false;
let sending = false;
let unavailable = false;
/** @type {Wire|null} */ let busySummary = null;
/** @type {Wire|null} */ let active = null;
/** @type {Map<string, Wire>} */ const replies = new Map();
/** @type {Wire[]} */ let historyItems = [];
let historyCursor = "";
let historyLoaded = false;
let historyRequest = 0;
let historyPending = false;
let historyLoading = false;
let historyRefresh = false;
const progress = new Progress();
/** @type {ReturnType<typeof runsPage>|null} */ let runPage = null;
const notices = new ConnectionNotices(element("connection"));
const announcer = new Announcer(element("announcements"));
const alerted = new Set();
/** @type {Map<string, Promise<void>>} */ const recovering = new Map();

/** @param {string} runId */
async function recoverReply(runId) {
  const target = session;
  const process = instance;
  if (target === null) return;
  const key = JSON.stringify([process, target, runId]);
  if (recovering.has(key)) return recovering.get(key);
  const operation = (async () => {
    try {
      const response = await fetch(
        "/api/reply?" +
          new URLSearchParams({
            process_instance_id: process,
            session_id: target,
            run_id: runId,
          }),
      );
      const result = await response.json();
      if (session !== target || instance !== process) return;
      const reply = replies.get(runId);
      if (!reply || reply.session_id !== target) return;
      if (
        !response.ok ||
        result.process_instance_id !== process ||
        result.session_id !== target ||
        result.run_id !== runId ||
        typeof result.reply_text !== "string"
      )
        throw new Error("正文未完整加载");
      reply.text = result.reply_text;
      reply.loading = false;
    } catch {
      if (instance === process && session === target) {
        const reply = replies.get(runId);
        if (reply) reply.loading = true;
      }
    } finally {
      recovering.delete(key);
      renderMessages();
    }
  })();
  recovering.set(key, operation);
  return operation;
}

function canSend() {
  return (
    !!csrf &&
    connected &&
    valid &&
    !active &&
    !busySummary &&
    !unavailable &&
    !sending &&
    session !== null &&
    !!input.value.trim()
  );
}
function updateSend() {
  /** @type {HTMLButtonElement} */ (element("send")).disabled = !canSend();
  /** @type {HTMLButtonElement} */ (element("new-session")).disabled =
    sending || !!active || !!busySummary || unavailable || !csrf;
}
/** @param {Wire|null} summary */
function renderBusy(summary) {
  busySummary = summary;
  const card = /** @type {HTMLElement} */ (element("busy"));
  card.hidden = !summary;
  card.replaceChildren();
  if (!summary) return;
  card.append(
    node("strong", summary.stage),
    node(
      "p",
      summary.purpose === "chat" && summary.prompt_preview
        ? summary.prompt_preview
        : summary.purpose,
    ),
  );
  card.append(
    node(
      "small",
      `${summary.gateway === "cli" ? "CLI" : summary.gateway === "web" ? "Web" : summary.gateway} · ${summary.started_at || "尚未开始"}${summary.current_step === null ? "" : " · Step " + summary.current_step}`,
    ),
  );
  const href = summary.navigation?.href;
  if (typeof href === "string") {
    const url = new URL(href, location.origin);
    if (url.origin === location.origin && url.pathname.startsWith("/runs/")) {
      const link = node("a", "查看当前运行");
      link.href = url.href;
      card.append(link);
    }
  }
}
function renderMessages() {
  const list = element("messages");
  list.replaceChildren();
  const recorded = new Set();
  for (const item of historyItems) {
    if (item.type === "run_pair") {
      if (recorded.has(item.run_id)) continue;
      recorded.add(item.run_id);
      if (item.user) list.append(node("p", textBlocks(item.user)));
      if (item.assistant) list.append(node("p", textBlocks(item.assistant)));
      list.append(node("small", "已保存"));
    } else list.append(node("p", textBlocks(item.blocks)));
  }
  for (const [id, reply] of replies) {
    if (reply.session_id !== session || recorded.has(id)) continue;
    if (reply.user) list.append(node("p", reply.user));
    if (reply.text !== undefined) list.append(node("p", reply.text));
    if (reply.loading) {
      list.append(node("p", "正文未完整加载"));
      const retry = node("button", "重新加载正文");
      retry.addEventListener("click", () => void recoverReply(id));
      list.append(retry);
    }
    if (reply.outcome) list.append(node("small", outcomeLabel(reply)));
    list.append(
      node(
        "small",
        reply.recording_state === "recorded"
          ? "已保存"
          : reply.recording_state === "failed"
            ? "回复已收到但未保存"
            : reply.recording_state === "pending"
              ? "正在保存"
              : "已接受",
      ),
    );
  }
  const temporary = progress.text(session);
  if (temporary) {
    const text = node("p", temporary);
    text.className = "temporary";
    list.append(text);
  }
}
function resetHistory() {
  historyRequest++;
  historyItems = [];
  historyCursor = "";
  historyLoaded = false;
  historyLoading = false;
  historyRefresh = false;
  historyPending = false;
}
/** @param {boolean} [older] */
async function loadMessages(older = false) {
  const target = session;
  if (target === null) return;
  if (historyLoading) {
    if (!older) historyRefresh = true;
    return;
  }
  const request = ++historyRequest;
  historyLoading = true;
  const button = /** @type {HTMLButtonElement} */ (element("older"));
  button.disabled = true;
  const height = drawer.scrollHeight;
  try {
    const params = {
      session_id: target,
      ...(older && historyCursor ? { cursor: historyCursor } : {}),
    };
    const response = await fetch("/api/mainbar?" + new URLSearchParams(params));
    const page = await response.json();
    if (session !== target || request !== historyRequest) return;
    if (!response.ok) {
      if (response.status === 404) valid = false;
      throw new Error(
        response.status === 404
          ? "会话已失效；草稿已保留，请选择或新建会话。"
          : "对话暂时无法加载；草稿已保留。",
      );
    }
    valid = true;
    const resumePending = historyPending && !historyCursor && !historyItems.some(item => item.type === "historic_message");
    const items = /** @type {Wire[]} */ ([...page.items].reverse());
    if (!historyLoaded) historyItems = items;
    else {
      const pairs = new Map(
        historyItems
          .filter((item) => item.type === "run_pair")
          .map((item) => [item.run_id, item]),
      );
      for (const item of items)
        if (item.type === "run_pair") pairs.set(item.run_id, item);
      const historic = historyItems.filter(
        (item) => item.type === "historic_message",
      );
      historyItems = [
        ...(older || (resumePending && !historic.length)
          ? items.filter((item) => item.type === "historic_message")
          : []),
        ...historic,
        ...[...pairs.values()].sort(
          (a, b) => a.activity_revision - b.activity_revision,
        ),
      ];
    }
    if (!historyLoaded || older || resumePending)
      historyCursor = page.next_cursor || "";
    historyPending = page.runs_pending;
    historyLoaded = true;
    button.hidden = !historyCursor;
    button.textContent = historyPending
      ? "当前运行保存后继续读取"
      : "更早的消息";
    renderMessages();
    if (older) drawer.scrollTop += drawer.scrollHeight - height;
  } catch (failure) {
    if (request === historyRequest)
      error.textContent =
        failure instanceof Error
          ? failure.message
          : "对话暂时无法加载；草稿已保留。";
  } finally {
    if (request === historyRequest) {
      historyLoading = false;
      button.disabled = historyPending;
      updateSend();
      if (historyRefresh) {
        historyRefresh = false;
        void loadMessages();
      }
    }
  }
}
element("older").addEventListener("click", () => void loadMessages(true));
const stream = new Stream(
  (kind, body, first) => {
    if (kind === "state_patch") {
      if (first && instance !== body.process_instance_id) {
        instance = body.process_instance_id;
        revision = -1;
        replies.clear();
        progress.clear();
        resetHistory();
        if (body.session_valid) void loadMessages();
        csrf = "";
        void refreshEntry(instance);
      }
      if (body.process_instance_id !== instance) return;
      if (first) {
        connected = true;
        valid = body.session_valid;
        notices.snapshot();
        updateSend();
      }
      if (body.state_revision <= revision) return;
      notices.snapshot();
      const incoming = body.active_run;
      if (
        incoming?.recording_state === "pending" &&
        ["recorded", "failed"].includes(
          replies.get(incoming.run_id)?.recording_state,
        )
      )
        return;
      revision = body.state_revision;
      connected = true;
      valid = body.session_valid;
      active = body.coordinator_state === "idle" ? null : incoming;
      progress.snapshot(active, body.step);
      notices.settled(
        body.coordinator_state === "idle",
        ![...progress.attempts.values()].some(
          (attempt) => attempt.suppressed && !attempt.terminal,
        ),
      );
      unavailable = body.coordinator_state === "recording_failed";
      const projection = body.unrecorded_terminal_projection;
      if (projection) {
        const old = replies.get(projection.run_id) || {};
        replies.set(projection.run_id, {
          ...old,
          session_id: projection.session_id,
          user: old.user || projection.prompt_preview,
          outcome: projection.outcome,
          recording_state: projection.recording_state,
          loading: old.text === undefined,
        });
        if (projection.session_id === session && old.text === undefined)
          void recoverReply(projection.run_id);
        if (
          projection.recording_state === "failed" &&
          !alerted.has(projection.run_id)
        ) {
          alerted.add(projection.run_id);
          element("alerts").textContent = "回复已收到但未保存";
        }
      }
      const stages = /** @type {Record<string,string>} */ ({
        accepted: "已接受",
        running: "运行中",
        recording_pending: "正在保存",
        recording_failed: "保存失败",
      });
      renderBusy(
        active
          ? {
              ...active,
              stage: stages[body.coordinator_state],
              navigation: {
                href: `/runs/${encodeURIComponent(active.run_id)}?filter=${active.purpose === "chat" ? "chat" : "system"}`,
              },
            }
          : null,
      );
      announcer.say(active ? stages[body.coordinator_state] : "就绪");
      if (unavailable) error.textContent = "记录服务不可用；草稿已保留。";
      if (incoming?.recording_state) {
        const old = replies.get(incoming.run_id);
        if (old) old.recording_state = incoming.recording_state;
        if (incoming.recording_state === "recorded") void loadMessages();
      }
      if (!valid && session !== null)
        error.textContent = "会话已失效；草稿已保留，请选择或新建会话。";
      renderMessages();
      updateSend();
      runPage?.sync(active);
    } else if (kind === "domain_event") {
      if (!progress.receive(body)) return;
      const event = body.payload;
      const envelope = body.envelope;
      notices.settled(
        event.name === "run.finished",
        ![...progress.attempts.values()].some(
          (attempt) => attempt.suppressed && !attempt.terminal,
        ),
      );
      if (event.name === "run.finished" && envelope.session_id === session) {
        const old = replies.get(envelope.run_id) || {};
        replies.set(envelope.run_id, {
          ...old,
          session_id: envelope.session_id,
          outcome: event.outcome,
          text: textBlocks(event.reply?.blocks) || event.error || "运行已结束",
          recording_state: old.recording_state || "pending",
          loading: false,
        });
        renderMessages();
        void loadMessages();
      }
      renderMessages();
      runPage?.update();
      if (event.name === "run.finished") void runPage?.loadEvidence();
    } else if (kind === "transport_notice") {
      progress.interrupt();
      notices.receive(body);
      renderMessages();
    }
  },
  () => {
    connected = false;
    progress.interrupt();
    notices.disconnected();
    renderMessages();
    updateSend();
  },
);

/** @param {string} expected */
async function refreshEntry(expected) {
  try {
    const response = await fetch("/api/entry");
    const entry = await response.json();
    if (instance !== expected) return;
    if (!response.ok || entry.instance_id !== expected)
      throw new Error("实例已改变");
    csrf = entry.csrf_token;
  } catch {
    if (instance === expected)
      error.textContent = "入口暂不可用；请刷新页面，草稿仍保留。";
  }
  updateSend();
}

async function send() {
  if (!canSend()) return;
  const target = session;
  const draft = input.value;
  sending = true;
  updateSend();
  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": csrf,
      },
      body: JSON.stringify({ message: draft, session_id: target }),
    });
    const result = await response.json();
    if (response.status === 409) {
      renderBusy(result.active_run_summary || null);
      if (!result.active_run_summary)
        error.textContent = "当前正忙；草稿已保留。";
      return;
    }
    if (response.status === 503) {
      unavailable = true;
      throw new Error("记录服务不可用；草稿已保留。");
    }
    if (response.status === 404) {
      valid = false;
      throw new Error("会话已失效；草稿已保留，请选择或新建会话。");
    }
    if (response.status !== 202) throw new Error("消息未获准；草稿已保留。");
    const old = replies.get(result.run_id) || {};
    replies.set(result.run_id, { ...old, session_id: target, user: draft });
    if (session === target && input.value === draft) {
      input.value = "";
      sessionStorage.setItem(`alfred.draft:${target}`, "");
    }
    renderMessages();
  } catch (failure) {
    error.textContent =
      failure instanceof Error ? failure.message : "连接不可用";
  } finally {
    sending = false;
    updateSend();
    input.focus();
  }
}
element("compose").addEventListener("submit", (event) => {
  event.preventDefault();
  void send();
});
input.addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    void send();
  }
});

function restoreDraft() {
  input.disabled = session === null;
  input.value =
    session === null
      ? ""
      : sessionStorage.getItem(`alfred.draft:${session}`) || "";
  element("session-name").textContent =
    session === null ? "尚未选择会话" : "会话";
}
function showDrawer() {
  const shell = /** @type {HTMLElement} */ (element("mainbar"));
  const wasModal = modal;
  modal = expanded && narrow.matches;
  if (modal && !wasModal)
    returnFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : input;
  for (const background of [
    document.querySelector(".site-header"),
    element("page"),
  ]) {
    if (background instanceof HTMLElement) background.inert = modal;
  }
  document.body.classList.toggle("modal-open", modal);
  if (modal) {
    shell.setAttribute("role", "dialog");
    shell.setAttribute("aria-modal", "true");
    shell.setAttribute("aria-label", "主对话对话框");
  } else {
    shell.removeAttribute("role");
    shell.removeAttribute("aria-modal");
    shell.setAttribute("aria-label", "MainBar");
  }
  drawer.hidden = !expanded;
  toggle.textContent = expanded ? "收起对话" : "展开对话";
  toggle.setAttribute("aria-expanded", String(expanded));
  element("mainbar").classList.toggle("expanded", expanded);
  sessionStorage.setItem("alfred.expanded", String(expanded));
  if (modal && !wasModal) input.disabled ? toggle.focus() : input.focus();
  if (!modal && wasModal)
    (returnFocus?.isConnected ? returnFocus : input).focus();
}
narrow.addEventListener("change", showDrawer);
function route() {
  const isRuns = location.pathname.startsWith("/runs");
  const heading = document.createElement("h1");
  heading.textContent = isRuns ? "运行详情" : "Gateway 收件箱";
  element("page").replaceChildren(heading);
  if (!isRuns) inbox(element("page"), resume);
  runPage = isRuns ? runsPage(element("page"), progress, route) : null;
  if (connected) runPage?.sync(active);
  for (const link of document.querySelectorAll("nav a")) {
    if ((link.getAttribute("href") === "/runs") === isRuns)
      link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}
/** @param {string} target */
async function resume(target) {
  if (
    sending ||
    (active?.purpose === "chat" &&
      active.session_id === session &&
      target !== session)
  ) {
    error.textContent = "当前会话的运行尚未收尾，请完成后再切换。";
    return;
  }
  if (target !== session) {
    session = target;
    sessionStorage.setItem("alfred.session", target);
    resetHistory();
    valid = false;
    connected = false;
    revision = -1;
    restoreDraft();
    progress.interrupt();
    stream.connect(session);
  }
  expanded = true;
  showDrawer();
  await loadMessages();
  input.focus();
}
input.addEventListener("input", () => {
  if (session !== null)
    sessionStorage.setItem(`alfred.draft:${session}`, input.value);
  updateSend();
});
toggle.addEventListener("click", () => {
  expanded = !expanded;
  showDrawer();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Tab" && modal) {
    const controls = [
      ...element("mainbar").querySelectorAll(
        "button:not(:disabled),textarea:not(:disabled),a[href],summary",
      ),
    ].filter(
      (item) => item instanceof HTMLElement && item.getClientRects().length,
    );
    const first = /** @type {HTMLElement|undefined} */ (controls[0]);
    const last = /** @type {HTMLElement|undefined} */ (controls.at(-1));
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    }
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
  if (event.key === "Escape" && expanded && !event.isComposing) {
    const wasModal = modal;
    expanded = false;
    showDrawer();
    if (!wasModal) input.focus();
  }
});
document.addEventListener("click", (event) => {
  const link =
    event.target instanceof Element ? event.target.closest("a") : null;
  if (
    !link ||
    link.origin !== location.origin ||
    !(
      link.pathname === "/inbox" ||
      link.pathname === "/runs" ||
      link.pathname.startsWith("/runs/")
    ) ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    event.altKey
  )
    return;
  event.preventDefault();
  history.pushState(null, "", link.href);
  route();
});
window.addEventListener("popstate", route);
element("new-session").addEventListener("click", async () => {
  const button = /** @type {HTMLButtonElement} */ (element("new-session"));
  button.disabled = true;
  try {
    const response = await fetch("/api/sessions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-agent-alfred-csrf": csrf,
      },
      body: "{}",
    });
    const result = await response.json();
    if (!response.ok || typeof result.session_id !== "string")
      throw new Error("会话未能创建，请稍后重试。");
    session = result.session_id;
    sessionStorage.setItem("alfred.session", /** @type {string} */ (session));
    error.textContent = "";
    restoreDraft();
    resetHistory();
    connected = false;
    revision = -1;
    stream.connect(session);
    await loadMessages();
    input.focus();
  } catch (failure) {
    error.textContent =
      failure instanceof Error ? failure.message : "连接不可用";
  } finally {
    updateSend();
  }
});
restoreDraft();
showDrawer();
route();
try {
  const response = await fetch("/api/entry");
  const entry = await response.json();
  if (!response.ok) throw new Error("连接不可用");
  csrf = entry.csrf_token;
  updateSend();
  instance = entry.instance_id;
  stream.connect(session);
  await loadMessages();
} catch {
  error.textContent = "连接不可用；草稿仍保留在本标签页。";
}
