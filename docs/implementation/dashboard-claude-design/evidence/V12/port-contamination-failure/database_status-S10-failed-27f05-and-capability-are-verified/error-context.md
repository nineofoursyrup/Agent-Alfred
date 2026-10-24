# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: database_status.spec.js >> S10 failed cleanup blocks queries until both release and capability are verified
- Location: tests/browser/database_status.spec.js:127:1

# Error details

```
Error: Memory server closed (code=1, signal=null): Traceback (most recent call last):
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/lifecycle.py", line 598, in _bind
    server = OwnedResource.acquire(
        rollback,
    ...<6 lines>...
        close=close_bound,
    )
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/resource_rollback.py", line 414, in acquire
    initialize(owner)
    ~~~~~~~~~~^^^^^^^
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/lifecycle.py", line 847, in _initialize_bound_server
    factory(address, handler, owner)
    ~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/lifecycle.py", line 882, in _default_server_factory
    _Server.__init__(server, address, handler)
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/socketserver.py", line 457, in __init__
    self.server_bind()
    ~~~~~~~~~~~~~~~~^^
  File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/http/server.py", line 148, in server_bind
    socketserver.TCPServer.server_bind(self)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
  File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/socketserver.py", line 478, in server_bind
    self.socket.bind(self.server_address)
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^
OSError: [Errno 48] Address already in use

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/Users/nineofour/Agent-Alfred-v12/tests/browser/database_server.py", line 133, in <module>
    main()
    ~~~~^^
  File "/Users/nineofour/Agent-Alfred-v12/tests/browser/database_server.py", line 30, in main
    dashboard.start()
    ~~~~~~~~~~~~~~~^^
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/server.py", line 304, in start
    descriptor = self._start_locked()
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/server.py", line 373, in _start_locked
    descriptor = service.start(rollback_step_timeout=self._rollback_step_timeout)
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/lifecycle.py", line 570, in start
    rollback.raise_failure(exc)
    ~~~~~~~~~~~~~~~~~~~~~~^^^^^
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/resource_rollback.py", line 682, in raise_failure
    raise propagated
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/lifecycle.py", line 565, in start
    self._bind(rollback, release_server)
    ~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/nineofour/Agent-Alfred-v12/src/agent_alfred/gateway/web/lifecycle.py", line 609, in _bind
    raise PortUnavailable(
        DEFAULT_HOST, self._requested_port, _bind_error_reason(exc)
    ) from exc
agent_alfred.gateway.web.lifecycle.PortUnavailable: cannot bind 127.0.0.1:18124: address already in use; the port is never chosen automatically

```

# Test source

```ts
  1   | import {spawn} from "node:child_process";
  2   | import {once} from "node:events";
  3   | import {createInterface} from "node:readline";
  4   | import {mkdtemp, rm} from "node:fs/promises";
  5   | import {tmpdir} from "node:os";
  6   | import {join} from "node:path";
  7   |
  8   | // A real Memory Dashboard on its own state directory. stdin drives only the
  9   | // offline model's next plan, the Host's own mutation gate and the test-edge
  10  | // faults documented in memory_server.py.
  11  | export async function memoryServer({threshold = 10, prepare, spawnProcess = spawn, script = "tests/browser/memory_server.py"} = {}) {
  12  |   const directory = await mkdtemp(join(tmpdir(), "alfred-memory-browser-"));
  13  |   const port = Number(process.env.ALFRED_BROWSER_TEST_PORT || 17736) + 4;
  14  |   let server;
  15  |   let lines;
  16  |   let stderr = "";
  17  |   let closed;
  18  |   const waiting = [];
  19  |   async function start() {
  20  |     stderr = "";
  21  |     server = spawnProcess(".venv/bin/python", [
  22  |       "-X", "faulthandler", "-B", script, "--port", String(port),
  23  |       "--state", directory, "--threshold", String(threshold),
  24  |     ]);
  25  |     // Capture close at creation: exit may precede final stderr or stop().
  26  |     closed = once(server, "close");
  27  |     lines = createInterface({input: server.stdout});
  28  |     server.stderr.on("data", data => {stderr += data;});
  29  |     lines.on("line", line => {
  30  |       const index = waiting.findIndex(item => item.line === line);
  31  |       if (index >= 0) waiting.splice(index, 1)[0].resolve();
  32  |     });
  33  |     await Promise.race([
  34  |       new Promise(resolve => waiting.push({line: "ready", resolve})),
  35  |       closed.then(([code, signal]) => {throw new Error(`Memory server closed before ready (code=${code}, signal=${signal}): ${stderr}`);}),
  36  |     ]);
  37  |   }
  38  |   async function stop() {
  39  |     if (!server) return;
  40  |     if (server.exitCode === null && server.signalCode === null)
  41  |       server.stdin.write("stop\n");
  42  |     const [code, signal] = await closed;
  43  |     lines?.close();
  44  |     if (code !== 0 || signal !== null)
> 45  |       throw new Error(`Memory server closed (code=${code}, signal=${signal}): ${stderr}`);
      |             ^ Error: Memory server closed (code=1, signal=null): Traceback (most recent call last):
  46  |   }
  47  |   async function send(command) {
  48  |     let waiter;
  49  |     const done = new Promise(resolve => {
  50  |       waiter = {line: "ok " + command, resolve};
  51  |       waiting.push(waiter);
  52  |     });
  53  |     try {
  54  |       if (server.exitCode === null && server.signalCode === null)
  55  |         server.stdin.write(command + "\n");
  56  |       await Promise.race([
  57  |         done,
  58  |         closed.then(([code, signal]) => {throw new Error(`Memory server closed during command ${command} (code=${code}, signal=${signal}): ${stderr}`);}),
  59  |       ]);
  60  |     } finally {
  61  |       const index = waiting.indexOf(waiter);
  62  |       if (index >= 0) waiting.splice(index, 1);
  63  |     }
  64  |   }
  65  |   async function close() {
  66  |     try {
  67  |       await stop();
  68  |     } finally {
  69  |       await rm(directory, {recursive: true, force: true});
  70  |     }
  71  |   }
  72  |   try {
  73  |     if (prepare) await prepare(directory);
  74  |     await start();
  75  |   } catch (error) {
  76  |     await close();
  77  |     throw error;
  78  |   }
  79  |   return {
  80  |     origin: `http://127.0.0.1:${port}`,
  81  |     directory,
  82  |     send,
  83  |     close,
  84  |     async crashAndRestart() {
  85  |       server.kill("SIGKILL");
  86  |       await closed;
  87  |       lines?.close();
  88  |       await start();
  89  |     },
  90  |     async restart() {
  91  |       await stop();
  92  |       await start();
  93  |     },
  94  |   };
  95  | }
  96  |
  97  | // Direct API use from a test is the same guarded HTTP boundary a second tab
  98  | // would use: the process CSRF token, no forged origin or permission.
  99  | export async function api(request, origin) {
  100 |   const {csrf_token: token} = await (await request.get(origin + "/api/entry")).json();
  101 |   return {
  102 |     async command(body) {
  103 |       const response = await request.post(origin + "/api/memory/commands", {
  104 |         headers: {"x-agent-alfred-csrf": token},
  105 |         data: {schema_version: 1, ...body},
  106 |       });
  107 |       return {status: response.status(), body: await response.json()};
  108 |     },
  109 |     async get(path) {
  110 |       const response = await request.get(origin + path);
  111 |       return {status: response.status(), body: await response.json()};
  112 |     },
  113 |   };
  114 | }
  115 |
```
