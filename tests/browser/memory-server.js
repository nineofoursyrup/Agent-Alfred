import {spawn} from "node:child_process";
import {once} from "node:events";
import {createInterface} from "node:readline";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";

// A real Memory Dashboard on its own state directory. stdin drives only the
// offline model's next plan, the Host's own mutation gate and the test-edge
// faults documented in memory_server.py.
export async function memoryServer({threshold = 10, prepare} = {}) {
  const directory = await mkdtemp(join(tmpdir(), "alfred-memory-browser-"));
  const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736) + 4;
  let server;
  let lines;
  let stderr = "";
  const waiting = [];
  async function start() {
    server = spawn(".venv/bin/python", [
      "-B", "tests/browser/memory_server.py", "--port", String(port),
      "--state", directory, "--threshold", String(threshold),
    ]);
    lines = createInterface({input: server.stdout});
    server.stderr.on("data", data => {stderr += data;});
    lines.on("line", line => {
      const index = waiting.findIndex(item => item.line === line);
      if (index >= 0) waiting.splice(index, 1)[0].resolve();
    });
    await Promise.race([
      new Promise(resolve => waiting.push({line: "ready", resolve})),
      once(server, "exit").then(() => {throw new Error(stderr);}),
    ]);
  }
  async function stop() {
    if (server && server.exitCode === null) {
      const exited = once(server, "exit");
      server.stdin.write("stop\n");
      await exited;
    }
    lines?.close();
    if (server && server.exitCode !== 0) throw new Error(stderr);
  }
  async function send(command) {
    const done = new Promise(resolve => waiting.push({line: "ok " + command, resolve}));
    server.stdin.write(command + "\n");
    await done;
  }
  async function close() {
    try {
      await stop();
    } finally {
      await rm(directory, {recursive: true, force: true});
    }
  }
  try {
    if (prepare) await prepare(directory);
    await start();
  } catch (error) {
    await close();
    throw error;
  }
  return {
    origin: `http://127.0.0.1:${port}`,
    directory,
    send,
    close,
    async restart() {
      await stop();
      await start();
    },
  };
}

// Direct API use from a test is the same guarded HTTP boundary a second tab
// would use: the process CSRF token, no forged origin or permission.
export async function api(request, origin) {
  const {csrf_token: token} = await (await request.get(origin + "/api/entry")).json();
  return {
    async command(body) {
      const response = await request.post(origin + "/api/memory/commands", {
        headers: {"x-agent-alfred-csrf": token},
        data: {schema_version: 1, ...body},
      });
      return {status: response.status(), body: await response.json()};
    },
    async get(path) {
      const response = await request.get(origin + path);
      return {status: response.status(), body: await response.json()};
    },
  };
}
