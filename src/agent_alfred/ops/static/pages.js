import { node, textBlocks } from "./dom.js";
import { outcomeLabel } from "./runs.js";
/** @typedef {Record<string, any>} Wire */

/** Every paginator keeps its own opaque cursor and single in-flight request. */
export class Pager {
  /** @param {HTMLElement} root @param {string} path @param {Record<string,string>} params @param {(body:Wire)=>void} append @param {string} label */
  constructor(root, path, params, append, label) {
    this.root = root;
    this.path = path;
    this.params = params;
    this.append = append;
    this.cursor = "";
    this.busy = false;
    this.finished = false;
    this.button = node("button", label);
    this.label = label;
    this.button.addEventListener("click", () => void this.load());
    root.append(this.button);
  }
  async load() {
    if (this.busy || this.finished) return;
    this.busy = true;
    this.button.disabled = true;
    try {
      const params = {
        ...this.params,
        ...(this.cursor ? { cursor: this.cursor } : {}),
      };
      const response = await fetch(
        this.path + "?" + new URLSearchParams(params),
      );
      const body = await response.json();
      if (!this.root.isConnected) return;
      if (!response.ok) throw new Error("读取不可用");
      this.append(body);
      this.cursor = body.next_cursor || "";
      this.finished = !body.next_cursor;
      this.button.hidden = this.finished;
      this.button.textContent = this.label;
    } catch {
      if (this.root.isConnected)
        this.button.textContent = "读取暂不可用，点击重试";
    } finally {
      this.busy = false;
      this.button.disabled = false;
    }
  }
}

/** @param {HTMLElement} root @param {(id:string)=>void} resume */
export function inbox(root, resume) {
  const groups = node("div");
  const preview = node("section");
  preview.setAttribute("aria-label", "会话只读预览");
  root.append(groups, preview);
  const seen = new Set();
  const pager = new Pager(
    root,
    "/api/sessions",
    { limit: "25" },
    (body) => {
      const sessions = [
        ...(body.non_terminal ? [body.non_terminal] : []),
        ...body.sessions,
      ];
      if (!sessions.length && !seen.size)
        groups.append(node("p", "还没有会话。点击下方「新建会话」开始。"));
      for (const session of sessions) {
        if (seen.has(session.session_id)) continue;
        seen.add(session.session_id);
        const card = node("article");
        card.className = "card";
        const open = node("button", session.title);
        card.append(open, node("p", session.created_at));
        groups.append(card);
        open.addEventListener("click", () =>
          showSession(preview, session, resume),
        );
      }
    },
    "更多会话",
  );
  void pager.load();
}

/** @param {HTMLElement} root @param {Wire} session @param {(id:string)=>void} resume */
function showSession(root, session, resume) {
  const content = node("div");
  root.replaceChildren(content);
  const continueButton = node("button", "继续此会话");
  continueButton.addEventListener("click", () => resume(session.session_id));
  content.append(
    node("h2", session.title),
    node("p", "只读预览"),
    continueButton,
  );
  const messages = node("div");
  const runs = node("div");
  content.append(messages, runs);
  const messagePager = new Pager(
    content,
    "/api/sessions/messages",
    { session_id: session.session_id, page_size: "25" },
    (body) => {
      for (const message of body.messages)
        messages.append(node("p", textBlocks(message.blocks)));
    },
    "更多会话消息",
  );
  const seen = new Set();
  const runPager = new Pager(
    content,
    "/api/sessions/runs",
    { session_id: session.session_id, limit: "25" },
    (body) => {
      for (const run of body.runs) {
        if (seen.has(run.run_id)) continue;
        seen.add(run.run_id);
        const row = node("article");
        row.className = "card";
        row.append(
          node(
            "p",
            `${run.gateway === "cli" ? "CLI" : run.gateway === "web" ? "Web" : run.gateway} · ${run.accepted_at}`,
          ),
        );
        row.append(node("p", outcomeLabel(run)));
        if (run.reply_preview !== null)
          row.append(node("p", run.reply_preview));
        const link = node("a", "查看运行");
        link.href = `/runs/${encodeURIComponent(run.run_id)}?filter=chat`;
        row.append(link);
        runs.append(row);
      }
    },
    "更多会话运行",
  );
  void messagePager.load();
  void runPager.load();
}
