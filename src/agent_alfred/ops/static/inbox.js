import { node, textBlocks } from "./dom.js";
import {
  sourceReader,
  follow,
  highlightSource,
  clampedText,
} from "./source.js";
import { runRow } from "./run-fields.js";
/** @typedef {Record<string,any>} Wire */

/** @param {HTMLElement} root @param {Wire} dashboard */
export function inbox(root, dashboard) {
  const url = new URL(location.href),
    session = url.searchParams.get("session_id");
  const partition = url.searchParams.get("view") ?? "messages";
  const connection = dashboard.runtime().instance;
  let closed = false,
    restored = false,
    anchor = /** @type {Wire|null} */ (null),
    origin = /** @type {Wire|null} */ (null);
  const rows = node("div"),
    controls = node("div");
  rows.className = "inbox-rows";
  const preview = node("section");
  preview.setAttribute(
    "aria-label",
    session === null ? "会话列表" : "会话只读预览",
  );
  root.append(preview);
  const seen = new Set();
  const heading = node("h2", session === null ? "最近会话" : `会话 ${session}`);
  preview.append(heading);
  const actions = node("div");
  actions.className = "inbox-actions";
  const createButton = node("button", "新建会话并打开主对话");
  const createReason = node("p"),
    continueReason = node("p"),
    restoreNotice = node("p");
  for (const status of [createReason, continueReason, restoreNotice])
    status.setAttribute("role", "status");
  const continueButton = node("button", "继续此会话");
  let targetVerified = false,
    targetFailure = "",
    actionFailure = "";
  function paintActions() {
    const createState = dashboard.sessionActionState("create");
    createButton.disabled = !createState.allowed;
    createReason.textContent = createState.reason || actionFailure;
    if (session !== null) {
      const state = dashboard.sessionActionState("continue", session);
      const reason =
        state.reason ||
        (dashboard.runtime().instance !== connection
          ? "来源进程已变化，请重新核验会话。"
          : !targetVerified
            ? targetFailure || "正在核验会话身份，请等待读取。"
            : "");
      continueButton.disabled = !!reason;
      continueReason.textContent = reason;
    }
  }
  createButton.addEventListener("click", async () => {
    actionFailure = "";
    const result = await dashboard.createSession();
    if (closed) return;
    actionFailure = result.status === "applied" ? "" : result.reason;
    paintActions();
  });
  actions.append(createButton, createReason);
  preview.append(actions, restoreNotice);
  const back = node("a", "返回会话列表");
  back.href = "/inbox";
  if (session !== null) {
    follow(
      back,
      () =>
        void dashboard.navigate("/inbox", {
          source: origin || {
            route: "/inbox",
            kind: "sessions",
            anchor: session,
            trigger: {
              key: "open-session",
              href:
                "/inbox?" +
                new URLSearchParams({ session_id: session, view: "messages" }),
            },
            process_instance_id: connection,
          },
        }),
    );
    continueButton.addEventListener("click", async () => {
      const result = await dashboard.continueSession(session);
      if (closed) return;
      paintActions();
      if (result.status !== "applied")
        continueReason.textContent = result.reason;
    });
    preview.append(
      back,
      node("p", "只读预览；继续此会话将切换主对话。"),
      continueButton,
      continueReason,
    );
    const nav = node("nav");
    nav.setAttribute("aria-label", "会话预览分区");
    for (const [value, label] of [
      ["messages", "消息"],
      ["runs", "运行"],
    ]) {
      const link = node("a", label);
      link.href =
        "/inbox?" + new URLSearchParams({ session_id: session, view: value });
      if (value === partition) link.setAttribute("aria-current", "page");
      follow(
        link,
        () =>
          void dashboard.navigate(link.href, {
            source: { returnSource: origin },
          }),
      );
      nav.append(link);
    }
    preview.append(nav);
  }
  if (session !== null && !["messages", "runs"].includes(partition)) {
    targetFailure = "无效的会话预览分区。";
    const stopActions = dashboard.subscribeSessionActions(paintActions);
    preview.append(node("p", "无效的会话预览分区。"));
    return {
      close() {
        closed = true;
        stopActions();
      },
    };
  }
  preview.append(rows, controls);
  const kind =
    session === null
      ? "sessions"
      : partition === "messages"
        ? "messages"
        : "session-runs";
  const path =
    session === null
      ? "/api/sessions"
      : partition === "messages"
        ? "/api/sessions/messages"
        : "/api/sessions/runs";
  const params =
    session === null
      ? { limit: "25" }
      : partition === "messages"
        ? { session_id: session, page_size: "25" }
        : { session_id: session, limit: "25" };
  const label =
    session === null
      ? "会话"
      : partition === "messages"
        ? "会话消息"
        : "会话运行";
  /** SourceContext contains identifiers and presentation state only. */
  function captureSource() {
    return {
      route: url.pathname + url.search,
      kind,
      session_id: session,
      partition,
      anchor: anchor?.id ?? null,
      anchorKind: anchor?.kind ?? null,
      trigger: anchor?.trigger ?? null,
      process_instance_id: connection,
      observed_at: reader.observed,
      returnSource: origin,
    };
  }
  /** @param {HTMLAnchorElement} link @param {string} id @param {string} key */
  function open(link, id, key) {
    link.dataset.sourceTrigger =
      key === "session" ? "open-session" : "open-run";
    follow(link, () => {
      anchor = {
        id,
        kind: key,
        trigger: {
          key: link.dataset.sourceTrigger,
          href: link.getAttribute("href"),
        },
      };
      void dashboard.navigate(link.href, {
        source: { returnSource: captureSource() },
      });
    });
  }
  const reader = sourceReader(
    controls,
    path,
    params,
    (body, replace) => {
      const items =
        session === null
          ? [
              ...(body.non_terminal ? [body.non_terminal] : []),
              ...body.sessions,
            ]
          : partition === "messages"
            ? body.messages
            : body.runs;
      if (
        !Array.isArray(items) ||
        items.some(
          (item) =>
            typeof (session === null
              ? item.session_id
              : partition === "messages"
                ? item.message_anchor
                : item.run_id) !== "string",
        )
      )
        throw new Error("source_identity_missing");
      if (replace) {
        rows.replaceChildren();
        seen.clear();
      }
      if (typeof body.title === "string") heading.textContent = body.title;
      for (const item of items) {
        const id =
          session === null
            ? item.session_id
            : partition === "messages"
              ? item.message_anchor
              : item.run_id;
        if (seen.has(id)) continue;
        seen.add(id);
        let row = node("article");
        row.className = "card";
        row.dataset.sourceKey =
          session === null
            ? "session"
            : partition === "messages"
              ? "message"
              : "run";
        row.dataset.sourceId = id;
        if (session === null) {
          const link = node("a", item.title || "未命名会话");
          link.href =
            "/inbox?" +
            new URLSearchParams({
              session_id: item.session_id,
              view: "messages",
            });
          open(link, id, "session");
          row.append(
            link,
            node("p", item.session_id),
            node("p", item.created_at),
          );
        } else if (partition === "messages") {
          row.append(
            node("p", `${item.role} · ${item.source} · ${item.created_at}`),
          );
          clampedText(row, textBlocks(item.blocks), id);
          if (typeof item.run_id === "string") {
            const link = node("a", "查看运行");
            link.href =
              "/runs/" + encodeURIComponent(item.run_id) + "?filter=chat";
            open(link, id, "message");
            row.append(link);
          } else row.append(node("small", "历史消息 · 无 Run 关联"));
        } else {
          row = runRow(item);
          if (typeof item.reply_preview === "string")
            clampedText(row, item.reply_preview, "reply-" + item.run_id);
          const link = node("a", "查看运行");
          link.href =
            "/runs/" + encodeURIComponent(item.run_id) + "?filter=chat";
          open(link, id, "run");
          row.append(link);
        }
        rows.append(row);
      }
      if (!seen.size && !body.runs_pending)
        rows.append(
          node(
            "p",
            session === null
              ? "还没有会话，可使用页头的新建会话入口。"
              : "此分区暂无已读记录。",
          ),
        );
      if (
        body.target &&
        body.target.placement !== "waiting" &&
        body.source_focus_allowed
      )
        queueMicrotask(() => {
          if (!closed) {
            const focused = highlightSource(
              rows,
              body.target.anchor,
              session === null
                ? "session"
                : partition === "messages"
                  ? "message"
                  : "run",
              anchor?.trigger || null,
              heading,
            );
            restoreNotice.textContent = focused
              ? "已重新核验来源并恢复原入口。"
              : "原入口已不可用，已恢复列表标题。";
          }
        });
    },
    label,
    dashboard.runtime,
    () => {
      targetVerified = true;
      targetFailure = "";
      paintActions();
      reconcileWaiting(dashboard.runtime());
    },
    (code) => {
      if (code === "unknown_session") {
        targetVerified = false;
        targetFailure = "此会话不存在或不可用，请返回会话列表。";
      } else if (!targetVerified)
        targetFailure = "会话身份尚未核验，请重试读取。";
      paintActions();
    },
  );
  let creationRevision = dashboard.sessionActionState("create").createdRevision;
  const stopActions = dashboard.subscribeSessionActions(() => {
    paintActions();
    const next = dashboard.sessionActionState("create").createdRevision;
    if (next > creationRevision && session === null)
      reader.changed("有新数据，请刷新会话。");
    creationRevision = next;
  });
  let lastRevision = dashboard.runtime().revision,
    lastActive = dashboard.runtime().active;
  /** Keep historical membership and cursors fixed; only announce related
   * durable-window changes observed through the accepted Host revisions.
   * @param {Wire} state */
  function observeChanges(state) {
    if (
      state.instance !== connection ||
      state.connected !== true ||
      !Number.isSafeInteger(state.revision)
    )
      return;
    if (state.revision > lastRevision) {
      const next = state.active;
      const related = (/** @type {Wire|null} */ run) =>
        typeof run?.session_id === "string" &&
        (session === null || run.session_id === session);
      const signature = (/** @type {Wire|null} */ run) =>
        run
          ? JSON.stringify([
              run.run_id,
              run.session_id,
              run.phase,
              run.recording_state,
            ])
          : "";
      const changed = signature(next) !== signature(lastActive);
      const settled =
        (related(next) &&
          ["recorded", "failed"].includes(next.recording_state)) ||
        (related(lastActive) && next?.run_id !== lastActive.run_id);
      // Absolute replacements may skip the active slot entirely. That revision
      // cannot prove this read unchanged, so offer a conservative explicit check.
      const unlocatedChange = !lastActive && !next && lastRevision >= 0;
      if (
        (changed &&
          (session === null
            ? related(next) || related(lastActive)
            : settled)) ||
        unlocatedChange
      )
        reader.changed(
          partition === "runs" && session !== null
            ? "有新运行，请刷新会话运行。"
            : "有新数据，请刷新" + label + "。",
        );
      lastRevision = state.revision;
      lastActive = next;
    }
  }
  /** A newer synchronized Host state can release this Session's waiting
   * boundary without reading or replacing its retained cursor.
   * @param {Wire} state */
  function reconcileWaiting(state) {
    if (
      session === null ||
      partition !== "messages" ||
      state.instance !== connection ||
      state.connected !== true
    )
      return;
    const active = state.active;
    if (
      active === null ||
      (active &&
        (active.session_id !== session ||
          ["recorded", "failed"].includes(active.recording_state)))
    )
      reader.releaseWaiting(state.revision);
  }
  let lastInstance = dashboard.runtime().instance,
    lastGap = 0;
  const unsubscribe = dashboard.subscribeState((/** @type {Wire} */ state) => {
    if (state.instance && state.instance !== lastInstance) {
      lastInstance = state.instance;
      reader.invalidate("进程已变化，请重新打开此页面");
    }
    if (state.connected === false) reader.invalidate("连接中断，保留旧快照");
    if (state.readGapRevision > lastGap) {
      lastGap = state.readGapRevision;
      reader.invalidate("增量通知缺口，快照待刷新");
    }
    observeChanges(state);
    reconcileWaiting(state);
    reader.sync();
  });
  queueMicrotask(() => {
    if (!closed && !restored) void reader.load();
  });
  return {
    captureSource,
    restoreSource(/** @type {Wire} */ source) {
      origin = source?.returnSource || null;
      if (source?.kind !== kind || typeof source.anchor !== "string") return;
      restored = true;
      anchor = {
        id: source.anchor,
        kind: source.anchorKind,
        trigger: source.trigger || null,
      };
      if (source.process_instance_id !== dashboard.runtime().instance) {
        reader.invalidate("来源进程已变化；请明确刷新当前列表");
        return;
      }
      const locate =
        session === null
          ? {
              path: "/api/sessions/locate",
              params: { session_id: source.anchor, limit: "25" },
            }
          : partition === "messages"
            ? {
                path: "/api/sessions/messages/locate",
                params: {
                  session_id: session,
                  anchor: source.anchor,
                  page_size: "25",
                },
              }
            : {
                path: "/api/sessions/runs/locate",
                params: {
                  session_id: session,
                  run_id: source.anchor,
                  limit: "25",
                },
              };
      void reader.load(true, locate);
    },
    close() {
      closed = true;
      reader.close();
      unsubscribe();
      stopActions();
    },
  };
}
