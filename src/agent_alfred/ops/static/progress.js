/** @typedef {Record<string, any>} Wire */

/** Published facts and the one disposable text slot are deliberately separate. */
export class Progress {
  constructor() {
    /** @type {Map<string, Map<number,Wire>>} */ this.events = new Map();
    /** @type {Map<string,Wire>} */ this.attempts = new Map();
    /** @type {Wire|null} */ this.slot = null;
  }
  clear() {
    this.events.clear();
    this.attempts.clear();
    this.slot = null;
  }
  interrupt() {
    for (const attempt of this.attempts.values())
      if (!attempt.terminal) attempt.suppressed = true;
    this.slot = null;
  }
  /** @param {Wire|null} active @param {Wire|null} step */
  snapshot(active, step) {
    if (!active || !step) return;
    for (const terminal of step.attempts || []) {
      const attempt = this.attempts.get(
        JSON.stringify([active.run_id, terminal.attempt_id]),
      );
      if (attempt) {
        attempt.terminal = true;
        attempt.blocks.clear();
        if (this.slot === attempt) this.slot = null;
      }
    }
  }
  /** @param {Wire} event @returns {boolean} */
  receive(event) {
    const { envelope: e, payload: p, seq } = event;
    let events = this.events.get(e.run_id);
    if (!events) {
      events = new Map();
      this.events.set(e.run_id, events);
    }
    if (events.has(seq)) return false;
    // Never retain thinking text, tool arguments, or tool results in the view.
    if (!p.name.startsWith("block.")) {
      const blocks = (p.blocks || []).map((/** @type {Wire} */ block) =>
        block.type === "text"
          ? { type: "text", text: block.text }
          : { type: block.type },
      );
      events.set(seq, {
        seq,
        envelope: e,
        payload: {
          name: p.name,
          attempt_id: p.attempt_id,
          model: p.model,
          streamed: p.streamed,
          blocks,
          usage: p.usage,
          duration_ms: p.duration_ms,
          stop_reason: p.stop_reason,
          outcome: p.outcome,
          error: p.error ? { code: p.error.code } : null,
        },
      });
    }
    const key = JSON.stringify([e.run_id, e.attempt_id]);
    if (p.name === "attempt.started" && !this.attempts.has(key)) {
      this.attempts.set(key, {
        run_id: e.run_id,
        attempt_id: e.attempt_id,
        session_id: e.session_id,
        suppressed: false,
        terminal: false,
        lastSeq: seq,
        blocks: new Map(),
      });
      this.slot = this.attempts.get(key) || null;
    }
    const attempt = this.attempts.get(key);
    if (
      p.name.startsWith("block.") &&
      attempt &&
      !attempt.suppressed &&
      !attempt.terminal &&
      seq > attempt.lastSeq
    ) {
      attempt.lastSeq = seq;
      if (p.name === "block.started" && p.block_type === "text")
        attempt.blocks.set(p.index, "");
      if (p.name === "block.delta" && attempt.blocks.has(p.index))
        attempt.blocks.set(p.index, attempt.blocks.get(p.index) + p.text);
    }
    if (p.name === "attempt.committed" || p.name === "attempt.aborted") {
      if (attempt) {
        attempt.terminal = true;
        attempt.blocks.clear();
      }
      if (this.slot === attempt) this.slot = null;
    }
    if (p.name === "run.finished" && this.slot?.run_id === e.run_id)
      this.slot = null;
    return true;
  }
  /** @param {string|null} session */
  text(session) {
    if (
      !this.slot ||
      this.slot.session_id !== session ||
      this.slot.suppressed ||
      this.slot.terminal
    )
      return "";
    return [...this.slot.blocks.entries()]
      .sort((a, b) => a[0] - b[0])
      .map((entry) => entry[1])
      .join("\n");
  }
}
