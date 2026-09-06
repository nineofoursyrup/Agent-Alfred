/** @typedef {Record<string, any>} Wire */

/** A single EventSource for the shell; asynchronous decoding stays in wire order. */
export class Stream {
  /** @param {(kind:string, body:Wire, first:boolean)=>void} receive @param {()=>void} disconnected */
  constructor(receive, disconnected) {
    this.receive = receive;
    this.disconnected = disconnected;
    /** @type {EventSource|null} */ this.source = null;
    this.generation = 0;
  }
  /** @param {string|null} session */
  connect(session) {
    this.source?.close();
    const generation = ++this.generation;
    const url =
      "/api/events" +
      (session === null
        ? ""
        : "?" + new URLSearchParams({ session_id: session }));
    const source = new EventSource(url);
    this.source = source;
    let first = true;
    let queue = Promise.resolve();
    /** @type {Wire|null} */ let candidate = null;
    /** @type {string[]} */ let fragments = [];
    let size = 0;
    let epoch = 0;
    source.addEventListener("open", () => {
      first = true;
    });
    source.addEventListener("error", () => {
      epoch++;
      candidate = null;
      fragments = [];
      this.disconnected();
    });
    for (const kind of ["state_patch", "transport_notice", "domain_event"]) {
      source.addEventListener(kind, (event) => {
        const currentEpoch = epoch;
        const data = /** @type {MessageEvent<string>} */ (event).data;
        queue = queue
          .then(async () => {
            if (generation !== this.generation || currentEpoch !== epoch)
              return;
            if (kind !== "domain_event") {
              this.receive(kind, JSON.parse(data), first);
              if (kind === "state_patch") first = false;
              return;
            }
            const match = data.match(
              /^(\{"seq":\d+,"event":"(?:[^"\\]|\\.)*","event_id":"(?:[^"\\]|\\.)*","chunk_index":\d+,"chunk_count":\d+,"event_sha256":"[a-f0-9]{64}"),"payload":([\s\S]*)\}$/,
            );
            if (!match) throw new Error("Invalid frame");
            const meta = JSON.parse(match[1] + "}");
            if (meta.chunk_index === 0) {
              candidate = meta;
              fragments = [];
              size = 0;
            }
            if (
              !candidate ||
              meta.chunk_index !== fragments.length ||
              meta.chunk_count < 1 ||
              meta.chunk_count > 2048 ||
              !Number.isSafeInteger(meta.seq) ||
              meta.seq < 1
            )
              throw new Error("Invalid chunks");
            for (const key of [
              "seq",
              "event",
              "event_id",
              "chunk_count",
              "event_sha256",
            ]) {
              if (candidate[key] !== meta[key])
                throw new Error("Inconsistent chunks");
            }
            size += new TextEncoder().encode(data).length;
            if (size > 32 * 1024 * 1024) throw new Error("Event too large");
            fragments.push(match[2]);
            if (fragments.length < meta.chunk_count) return;
            const raw = fragments.join("");
            candidate = null;
            fragments = [];
            const canonical = `{"event":${JSON.stringify(meta.event)},"event_id":${JSON.stringify(meta.event_id)},"payload":${raw}}`;
            const digest = await crypto.subtle.digest(
              "SHA-256",
              new TextEncoder().encode(canonical),
            );
            const hex = Array.from(new Uint8Array(digest), (byte) =>
              byte.toString(16).padStart(2, "0"),
            ).join("");
            if (hex !== meta.event_sha256) throw new Error("Invalid digest");
            if (generation !== this.generation || currentEpoch !== epoch)
              return;
            this.receive(kind, { ...JSON.parse(raw), seq: meta.seq }, false);
          })
          .catch(() => {
            if (generation !== this.generation) return;
            this.disconnected();
            source.close();
            // Reopening obtains an atomic snapshot, never a partial checkpoint.
            this.connect(session);
          });
      });
    }
  }
}
