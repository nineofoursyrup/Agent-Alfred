import {spawn, execFileSync} from "node:child_process";
import {once} from "node:events";
import {createInterface} from "node:readline";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";

// Existing server --state/--port boundary, with a test-owned database seed.
export async function localServer(seed) {
  const directory = await mkdtemp(join(tmpdir(), "alfred-browser-fixture-"));
  const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736) + 3;
  let server;
  let lines;
  let stderr = "";
  async function close() {
    if (server && server.exitCode === null) {
      const exited = once(server, "exit");
      server.kill("SIGTERM");
      await exited;
    }
    lines?.close();
    await rm(directory, {recursive:true, force:true});
    if (server && server.exitCode !== 0) throw new Error(stderr);
  }
  try {
    if (seed) execFileSync(".venv/bin/python", ["-B", "-c", seed, directory]);
    server = spawn(".venv/bin/python", ["-B", "tests/browser/server.py", "--port", String(port), "--state", directory]);
    lines = createInterface({input:server.stdout});
    server.stderr.on("data", data => {stderr += data;});
    await Promise.race([
      new Promise(resolve => lines.on("line", line => {if (line === "ready") resolve();})),
      once(server,"exit").then(() => {throw new Error(stderr);}),
    ]);
    return {origin:`http://127.0.0.1:${port}`, close};
  } catch (error) {
    await close();
    throw error;
  }
}
