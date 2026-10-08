import { node } from "./dom.js";
/** @typedef {Record<string,any>} Wire */

/** A page-owned source. Failures retain the successful body and cursor; only an
 * explicit refresh replaces its window. There is no background data polling.
 * @param {HTMLElement} root @param {string} path @param {Wire} params
 * @param {(body:Wire,replace:boolean)=>void} render @param {string} label
 * @param {()=>Wire} runtime */
export function sourceReader(root, path, params, render, label, runtime) {
  const status = node("p");
  status.setAttribute("role", "status");
  const refresh = node("button", `刷新${label}`),
    more = node("button", `更多${label}`);
  const latest = node("button", `查看最新${label}列表`);
  latest.hidden = true;
  const reopen = node("button", "重新核验页面");
  reopen.hidden = true;
  reopen.addEventListener("click", () => location.reload());
  root.append(status, refresh, more, latest, reopen);
  let cursor = /** @type {string|null} */ (null),
    busy = false,
    closed = false,
    hasRead = false,
    waiting = false;
  let observed = 0,
    invalidated = "",
    failure = "",
    sequence = 0;
  let controller = new AbortController();
  let readingIntent = 0;
  const moved = () => {
    readingIntent++;
  };
  document.addEventListener("pointerdown", moved);
  document.addEventListener("keydown", moved);
  const focused = (/** @type {FocusEvent} */ event) => {
    if (
      event.target instanceof HTMLElement &&
      event.target.matches(
        'input,textarea,button,select,a,summary,[contenteditable="true"]',
      )
    )
      moved();
  };
  document.addEventListener("focusin", focused);
  /** @type {{path:string,params:Wire}|null} */ let retry = null;
  const connection = runtime().instance;
  function paint() {
    const stale =
      invalidated ||
      (observed && Date.now() - observed >= 15 * 60 * 1000
        ? "已过期，请刷新"
        : "");
    status.textContent = [
      busy ? "读取中…" : "",
      failure,
      waiting ? "运行记录尚未落定；请稍后刷新，暂不跨越消息分段。" : "",
      observed
        ? `读取于 ${new Date(observed).toLocaleTimeString()}${stale ? " · " + stale : ""}`
        : invalidated,
    ]
      .filter(Boolean)
      .join(" ");
    refresh.disabled = busy;
    more.disabled = busy || waiting || !!invalidated;
    more.hidden = hasRead && !cursor;
    more.textContent = failure ? "读取暂不可用，点击重试" : `更多${label}`;
  }
  /** @param {boolean} replace @param {{path:string,params:Wire}|null} [location] */
  async function load(replace = false, location = null) {
    if (
      closed ||
      busy ||
      (!replace && !location && hasRead && (!cursor || waiting))
    )
      return;
    const own = ++sequence,
      instance = runtime().instance,
      intent = readingIntent;
    if (!instance) {
      failure = "连接尚未确认，暂不可读取";
      paint();
      return;
    }
    if (instance !== connection) {
      invalidated = "进程已变化，请重新打开此页面";
      paint();
      return;
    }
    busy = true;
    failure = "";
    paint();
    controller.abort();
    controller = new AbortController();
    const request = location || {
      path,
      params: { ...params, ...(!replace && cursor ? { cursor } : {}) },
    };
    try {
      const response = await fetch(
        request.path +
          "?" +
          new URLSearchParams({
            ...request.params,
            process_instance_id: instance,
          }),
        { signal: controller.signal },
      );
      const body = await response.json();
      if (
        closed ||
        own !== sequence ||
        !root.isConnected ||
        runtime().instance !== instance
      )
        return;
      if (!response.ok)
        throw new Error(body.code || body.error?.code || "read_unavailable");
      if (
        body.process_instance_id !== undefined &&
        body.process_instance_id !== instance
      )
        throw new Error("process_context_expired");
      if (
        request.params.session_id !== undefined &&
        body.session_id !== undefined &&
        body.session_id !== request.params.session_id
      )
        throw new Error("source_identity_mismatch");
      if (
        request.params.filter !== undefined &&
        body.filter !== undefined &&
        body.filter !== request.params.filter
      )
        throw new Error("source_filter_mismatch");
      if (location) {
        const expected =
          (location.path.startsWith("/api/runs/locate/")
            ? decodeURIComponent(
                location.path.slice("/api/runs/locate/".length),
              )
            : undefined) ??
          location.params.anchor ??
          location.params.run_id ??
          location.params.session_id;
        if (expected !== undefined && body.target?.anchor !== expected)
          throw new Error("source_identity_mismatch");
        if (
          location.params.session_id !== undefined &&
          body.target?.session_id !== location.params.session_id
        )
          throw new Error("source_identity_mismatch");
      }
      render(
        { ...body, source_focus_allowed: intent === readingIntent },
        replace || !!location,
      );
      cursor = body.next_cursor ?? null;
      waiting = body.runs_pending === true;
      hasRead = true;
      observed = Date.now();
      invalidated = "";
      retry = null;
      latest.hidden = true;
    } catch (error) {
      if (closed || own !== sequence) return;
      failure = error instanceof Error ? error.message : "read_unavailable";
      if (location) {
        retry = location;
        latest.hidden = false;
        failure = "来源暂不可定位：" + failure + "；未替换为其他目标。";
      } else failure = "读取暂不可用：" + failure + "；已读内容保留。";
    } finally {
      if (!closed && own === sequence) {
        busy = false;
        paint();
      }
    }
  }
  refresh.addEventListener("click", () => void load(true, retry));
  more.addEventListener("click", () => void load(false, retry));
  latest.addEventListener("click", () => {
    retry = null;
    void load(true);
  });
  const timer = setInterval(paint, 60 * 1000);
  paint();
  return {
    load,
    refresh,
    more,
    status,
    get observed() {
      return observed;
    },
    invalidate(/** @type {string} */ reason) {
      invalidated = reason;
      if (runtime().instance !== connection) reopen.hidden = false;
      paint();
    },
    close() {
      closed = true;
      ++sequence;
      controller.abort();
      clearInterval(timer);
      document.removeEventListener("pointerdown", moved);
      document.removeEventListener("keydown", moved);
      document.removeEventListener("focusin", focused);
    },
  };
}

/** Normal modified link semantics stay with the browser. @param {HTMLAnchorElement} link @param {()=>void} action */
export function follow(link, action) {
  link.addEventListener("click", (event) => {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    action();
  });
}

/** @param {HTMLElement} root @param {string} value @param {string} key */
export function highlightSource(root, value, key) {
  const target = [...root.querySelectorAll("[data-source-key]")].find(
    (item) =>
      item.getAttribute("data-source-key") === key &&
      item.getAttribute("data-source-id") === value,
  );
  if (target instanceof HTMLElement) {
    target.dataset.sourceHighlighted = "true";
    target.tabIndex = -1;
    target.scrollIntoView({ block: "center" });
    target.focus({ preventScroll: true });
  }
}

/** @param {HTMLElement} root @param {string} text @param {string} key */
export function clampedText(root, text, key) {
  const content = node("pre", text);
  content.className = "evidence-text";
  content.dataset.textKey = key;
  const button = node("button", "展开全文");
  button.setAttribute("aria-expanded", "false");
  button.dataset.textToggle = key;
  button.addEventListener("click", () => {
    const open = button.getAttribute("aria-expanded") !== "true";
    content.classList.toggle("expanded", open);
    button.setAttribute("aria-expanded", String(open));
    button.textContent = open ? "收起全文" : "展开全文";
  });
  root.append(content, button);
}

/** Preserve a reading position only in the same evidence identity. No text is
 * put in history/SourceContext, and replacement content never inherits a range.
 * @param {HTMLElement} root */
export function captureReading(root) {
  const selection = window.getSelection();
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  const startElement =
    range?.startContainer instanceof Element
      ? range.startContainer
      : range?.startContainer.parentElement;
  const text = startElement?.closest("[data-text-key]");
  let selected =
    /** @type {{key:string,text:string,start:number,end:number}|null} */ (null);
  if (
    range &&
    text instanceof HTMLElement &&
    root.contains(text) &&
    text.contains(range.endContainer)
  ) {
    const before = document.createRange();
    before.selectNodeContents(text);
    before.setEnd(range.startContainer, range.startOffset);
    const until = document.createRange();
    until.selectNodeContents(text);
    until.setEnd(range.endContainer, range.endOffset);
    selected = {
      key: text.dataset.textKey || "",
      text: text.textContent || "",
      start: before.toString().length,
      end: until.toString().length,
    };
  }
  const scroller = root.closest("#page");
  const top = scroller?.getBoundingClientRect().top ?? 0;
  const anchor = [...root.querySelectorAll("[id]")].find((item) => {
    const rect = item.getBoundingClientRect();
    return rect.top >= top && rect.top < innerHeight && rect.height > 0;
  });
  const offset = anchor?.getBoundingClientRect().top;
  return () => {
    if (selected) {
      const item = [...root.querySelectorAll("[data-text-key]")].find(
        (item) => item.getAttribute("data-text-key") === selected?.key,
      );
      if (item && item.textContent === selected.text && item.firstChild) {
        const next = document.createRange();
        next.setStart(item.firstChild, selected.start);
        next.setEnd(item.firstChild, selected.end);
        selection?.removeAllRanges();
        selection?.addRange(next);
      }
    }
    if (scroller && anchor && offset !== undefined) {
      const next = document.getElementById(anchor.id);
      if (next && root.contains(next))
        scroller.scrollTop += next.getBoundingClientRect().top - offset;
    }
  };
}
