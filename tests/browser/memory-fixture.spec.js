import {test, expect} from "@playwright/test";
import {EventEmitter} from "node:events";
import {PassThrough} from "node:stream";
import {memoryServer} from "./memory-server.js";

// Model the OS child-process boundary: exit and stdio close are distinct.
// No product or Python service behavior is replaced by these fixture tests.
for (const [code, signal] of [[0, null], [7, null], [null, "SIGTERM"]]) {
  test(`Memory fixture waits for stdio close after exit ${code}/${signal}`, async () => {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new PassThrough();
    child.exitCode = null;
    child.signalCode = null;
    const starting = memoryServer({spawnProcess: () => child});
    // memoryServer creates its temporary directory asynchronously.
    await expect.poll(() => child.stdout.listenerCount("data")).toBeGreaterThan(0);
    child.stdout.write("ready\n");
    const server = await starting;
    const stopping = server.close();
    let settled = false;
    void stopping.then(() => {settled = true;}, () => {settled = true;});
    child.exitCode = code;
    child.signalCode = signal;
    child.emit("exit", code, signal);
    await new Promise(resolve => setImmediate(resolve));
    expect(settled).toBe(false);
    child.stderr.write("final shutdown diagnostic");
    child.stdout.end();
    child.stderr.end();
    child.emit("close", code, signal);
    if (code === 0) await stopping;
    else await expect(stopping).rejects.toThrow(`code=${code}, signal=${signal}): final shutdown diagnostic`);
  });
}
