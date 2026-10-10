# PR #136 首次远端浏览器 CI 失败

- Run：[38072252519](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/38072252519)，head `59aea6b4ddef2496a2223299bbf3185073cb6902`。
- Job：`114271902875`，步骤 `Offline Dashboard browser tests`，**1 failed / 542 passed（18.0m）**；其他 Python 四项和 typecheck 均成功。
- 测试：`tests/browser/tools-visual.spec.js:77`，`V01 public recipes keep pulse opt-in, selection visible and reduced motion still`。
- 失败位置：原第 102 行 `row.evaluate(...)`，默认测试超时 15000ms；等待 `getByRole('region', { name: '公共配方检查' }).locator('.sy-row')`。
- 失败快照 main 已显示标题“工具”和内置工具列表，没有公共配方样例。

下载命令：`gh run download 38072252519 --repo nineofoursyrup/Agent-Alfred -n browser-failure-evidence -D /tmp/alfred-v01-ci-race-38072252519`。`browser-failure-evidence.zip` 保存下载的全部 121 个文件，逐成员校验 SHA 与解压字节；`MANIFEST.json` 记录尺寸、SHA 和归档身份。

失败用例的 PNG、`trace.zip` 与 `error-context.md` 在归档中的 `tools-visual-V01-public-re-8d434-le-and-reduced-motion-still/`。完整原始 job 日志通过 `gh api repos/nineofoursyrup/Agent-Alfred/actions/jobs/114271902875/logs --allow-escape-sequences` 获取，以 `job.log.gz` 保留原字节；manifest 同时记录压缩文件和解压原流 SHA。`run.json` 保留查询到的 run head／状态及 job／step 信息。

原因是测试把 fixture 替换进应用拥有的 `#page`，但启动完成后的首次 mount 会再次替换该宿主，覆盖 fixture。这是测试竞态，生产 CSS 没有变化。受控复现和修复后结果另存于 `../fixture-race-reproduction/`；本记录、原始截图和 trace 不被后继 PASS 覆盖。
