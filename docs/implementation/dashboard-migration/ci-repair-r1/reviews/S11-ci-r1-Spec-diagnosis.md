# S11 CI r1 — Spec 只读诊断

固定候选 `37cd0180fd5222d0753a0f3203ae56b1df6e0f0a`，tree `a69c4abe71569defeaa6b901e414a5f064d6f5d3`。本报告是 CI 首败归因和验收映射，不是修复后独立评审，也不作整体验收 PASS。

PR `37886281186`：522 PASS／5 FAIL；Push `37886278313`：525 PASS／2 FAIL。7 次观察涉及 6 条不同用例；全部 trace 内测试源文件与固定候选逐字节一致。原始日志、trace 和此前报告保持不变。

| 失败 | 已证实的边界与尚未证实的原因 |
| --- | --- |
| Inbox 返回 A 草稿，两次失败 | `selectSession(A)` 在 B 的连接尚未同步时被拒绝；测试忽略返回值，随后把 B 的空输入当作 A 检查。没有证据证明 A 草稿被删除。 |
| idle reconnect 精确核对 | 首个被计数 GET 早于 reconnect state；第二个才在 state 后。计数包含此前 `run.finished` 延后刷新是有代码依据的假设，尚不能据此认定错误保存或重复重连读取。 |
| Aggregation／Database／Integrations 共三条 | POST 201 返回前点击“打开主对话”，随后出现“已有新的页面或阅读意图”回执，Session 未采用，消息框禁用。不能仅加等待掩盖：需验证同一 Mainbar 的呈现动作是否误撤销原新建意图。 |
| restored empty IDs | present-empty query、HTTP 200、完整正文和 recorded 均正确；浏览器最终仍 loading、0 article。首次挂载使 `shell.generation` 变化并触发退役是待验证原因。S01 空 ID HTTP 合同未被此失败推翻。 |

验收处理：**CI／AC25 = FAIL；G01 及 AC23 的最终组合结论不能维持 PASS。** AC04／AC06／AC07 对应的本次浏览器责任保留 FAIL 观察并暂停无条件 PASS，不能把“未证实根因”写成“未执行”。三条新建失败尚未进入的聚合、SQL、搜索与账目业务步骤，在这条证据内记 NOT RUN；不据此宣告这些领域实现失败。AC03 的真实 IME 仍 BLOCKED。

必须保留守卫：真实导航、编辑、主动移焦、进程替换、在途 Run、旧响应退休均不能因本轮修复失效。闭环须使用权威状态／实际回执屏障，保留原业务断言；不能改成固定 sleep、允许两次读取、跳过空 ID 或直接设置测试成功状态。

证据、逐条时序及源责任建议见 [details](S11-ci-r1-Spec-diagnosis-details.md)；只读提取与源文件绑定见 [JSON](S11-ci-r1-Spec-diagnosis.json)。本轮未改产品、测试、canonical，未启动服务、重跑测试、更改 Git 状态或调用 GitHub 写接口。
