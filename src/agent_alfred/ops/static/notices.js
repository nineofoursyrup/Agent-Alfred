import { node } from "./dom.js";
const reasons = /** @type {Record<string,string>} */ ({
  malformed: "浏览器检查点无效",
  instance_mismatch: "本地服务已重启",
  too_old: "超出重放窗口",
  ahead: "浏览器提交了服务端从未签发的检查点",
});
const messages = /** @type {Record<string,string>} */ ({
  recoverable: "连接已重新同步，继续接收当前运行",
  unrecoverable: "当前运行的过程无法完整恢复；已清除临时文字，等待最终结果",
  absent: "已重新载入保存的对话；当前没有运行",
});
export class ConnectionNotices {
  /** @param {HTMLElement} container */
  constructor(container) {
    this.container = container;
    this.code = "";
    this.recovery = "";
    this.timer = 0;
    this.waiting = false;
  }
  clear() {
    clearTimeout(this.timer);
    this.code = "";
    this.container.replaceChildren();
  }
  /** @param {string} message @param {string} [cause] */
  show(message, cause) {
    this.container.replaceChildren(
      node("p", message),
      node("small", "仅影响本标签页"),
    );
    if (cause) {
      const details = node("details");
      details.append(node("summary", "连接原因"), node("p", cause));
      this.container.append(details);
    }
    const close = node("button", "关闭连接提示");
    close.addEventListener("click", () => this.container.replaceChildren());
    this.container.append(close);
  }
  disconnected() {
    this.clear();
    this.code = "disconnected";
    this.show("连接已断开；已清除临时文字，正在重新连接。");
  }
  /** @param {Record<string,any>} notice */
  receive(notice) {
    this.clear();
    this.code = notice.code;
    if (notice.code === "replay_gap") {
      this.recovery = notice.current_run_state;
      this.waiting = true;
      this.show(
        messages[this.recovery] || "正在重新同步",
        reasons[notice.gap_reason],
      );
    } else if (notice.code === "deltas_dropped")
      this.show("部分增量未收到；已清除临时文字，等待尝试收尾。");
  }
  snapshot() {
    if (this.code === "disconnected") this.clear();
    if (this.code === "replay_gap" && this.waiting) {
      this.waiting = false;
      if (this.recovery !== "unrecoverable")
        this.timer = window.setTimeout(() => this.clear(), 6000);
    }
  }
  /** @param {boolean} finished @param {boolean} attemptsSettled */
  settled(finished, attemptsSettled) {
    if (
      (finished &&
        this.code === "replay_gap" &&
        this.recovery === "unrecoverable") ||
      (attemptsSettled && this.code === "deltas_dropped")
    )
      this.clear();
  }
}

export class Announcer {
  /** @param {HTMLElement} container */
  constructor(container) {
    this.container = container;
    this.last = "";
    this.timer = 0;
  }
  /** @param {string} message */
  say(message) {
    if (message === this.last) return;
    this.last = message;
    clearTimeout(this.timer);
    this.timer = window.setTimeout(() => {
      this.container.textContent = message;
    }, 400);
  }
}
