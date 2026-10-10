# V01 公共配方 fixture 竞态复现与复测

失败 CI 为 [38072252519](https://github.com/nineofoursyrup/Agent-Alfred/actions/runs/38072252519)，head `59aea6b4ddef2496a2223299bbf3185073cb6902`。修复测试候选为 `f9fef124f3fd656e5711c5d047f488f6e09c0783`。

## 受控延迟方法

旧／新测试各运行 3 次。临时复制目标测试，使用 `page.route('**/api/entry', ...)` 先通过 `route.fetch()` 得到响应，但用 Promise 暂缓 `route.fulfill({response})`。等待接口已被拦截，注入原样例并执行原有 tab 选中操作，随后释放响应，等待 `#page` 的首次子节点替换，再执行原有 `row.evaluate` 及其余断言。

选择 `/api/entry` 是因为它是工具页首次挂载的启动依赖：app.js 完成 entry 后连接事件流，初始就绪状态触发壳层 mount；`mountPage` 替换 `#page` 后才建立工具页读取。只延迟 `/api/tools` 的响应发生在这次替换之后，无法稳定制造本次覆盖时序。Promise 与 MutationObserver 让覆盖发生在原有两个操作之间，没有使用任意睡眠、重试或增加超时。

临时副本与观察只用于复现，不进入实际测试。临时辅助脚本和生成的样例测试文本仅留在 `/tmp`，不提交。`controlled-race.zip` 保存完整合并 stdout/stderr、观察 JSON，以及旧测试的截图、机器生成错误上下文与 trace。`MANIFEST.json` 记录各成员 SHA-256；两次结果摘要见 `old-results.json`／`new-results.json`。

| 版本 | 结果 | 页面完成首次挂载后的观察 |
| --- | --- | --- |
| 旧测试 | 3/3 FAIL，均为 15000ms 超时，等待公共配方区域中的 `.sy-row` | `fixture_count: 0`，宿主 null；标题已为“工具” |
| 新测试 | 3/3 PASS，Playwright 3.6s，命令总耗时 4.24s | `fixture_count: 1`，宿主 `BODY`；标题同样已为“工具” |

新测试仅改为 `page.evaluate` 并 `document.body.append(fixture)`，外加说明注释。fixture 处于应用页面宿主之外，全部样例及现有断言保持原样；实际测试不再查询 `#page` 或工具页内部 DOM。后续工具页重排不影响这个独立 section。临时 `page.route`、MutationObserver、观察输出和辅助测试文件均已移除，实际文件没有重试和新增等待，原超时 15000ms 保持。

## 实际测试 20 次重复

```sh
ALFRED_BROWSER_TEST_PORT=17810 CI=1 NO_COLOR=1 npx playwright test tests/browser/tools-visual.spec.js --grep 'V01 public recipes keep pulse opt-in, selection visible and reduced motion still' --repeat-each=20 --output=/tmp/alfred-v01-fixture-race-repeat20-successor/results
```

**20 passed（21.2s）**，exit 0，命令总耗时 21.72s；无临时接口延迟或观察代码。`repeat20.zip` 保存原始输出、报告和 `.last-run.json`，`repeat20-MANIFEST.json` 核对成员 SHA。

第一次命令误用 `--grep '^V01 public recipes'`，未匹配包含文件路径的完整测试标题，exit 1、`No tests found`，实际 **NOT RUN**；该调用及结果也完整保留在 `repeat20.zip` 的 `no-tests-found/`，成功复测在 `successor/`，没有用成功结果覆盖调用失败。

这两次受控运行和 20 次复测在提交前完成，记录中的 `base_head` 仍为 `59aea6b`。`candidate-binding.json` 将旧／新源文件 SHA-256 分别核对到失败 head 和修复提交的 Git blob；不把未提交时的运行伪称为提交后的候选运行。完整 07 门禁另在修复提交上执行。端口始终为 17810，retries 0，test timeout 15000ms，expect timeout 4000ms。
