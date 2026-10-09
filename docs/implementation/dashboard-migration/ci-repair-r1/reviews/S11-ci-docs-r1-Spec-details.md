# S11 CI 文档 r1 — Spec 详情

固定增量：base `06588168d5049040abf0a4560091d659d244ddcf` → head `c96c26d1446ba97bdb7b219d79cef0b5fd001b53`，head tree `3d5f0997f26921056db04e224a6e7fa9ad417f8d`。27 个新增文件；未修改产品或测试。代码候选 source tree `0eaae1fbd39ec02656f4a61982ec73a06c46fe2f`、package source tree `91f1c1abcc26ea92be40fb5acee5fb93fbd62a0c` 与独立 r3 代码审查完全一致。

## F1 — P2：未限定的总状态掩盖未关闭的验收 FAIL

候选位置：`docs/implementation/dashboard-migration/ci-repair-r1.md:3` 的“整体仍 BLOCKED”，及 `ci-repair-r1.json:5` 的 `overall_result: BLOCKED`。同一 JSON:152–153 明确保留原 CI FAIL 与 AC23/G01 的组合 FAIL，并声明后继完整门禁仍待完成；代码双轴 PASS 的责任仅是 `06588168` 的修复代码。入口读者或读取顶层字段的消费者因此会得到“仅缺环境或未运行”的总状态，却看不到当前验收仍有未关闭 FAIL。

冻结要求：`docs/design/issue-92/ACCEPTANCE.md:69`（MCE-11）禁止“用 BLOCKED 藏已有 FAIL”；:74 要求每次观察分别记录实际结果；:76 要求保留未获有效证据的必需项。保留详细历史日志是正确的，但不能抵消入口总裁定的混淆。

最小修复：保留所有原件、历史 FAIL 与后继检查记录；明确当前验收仍 FAIL，独立修复代码审查 PASS，后继门禁 NOT RUN／pending，中文 IME BLOCKED。若顶层只表示修复执行阶段，应把字段与正文作用域明确命名，并另列当前验收状态。此 finding 不把“后继检查未运行”当作代码缺陷，也不要求重跑已适用的检查。

## F2 — P3：触控证据须标为 Chromium 模拟

候选位置：`ci-repair-r1.md:9` 将兼容 `mousedown` 写成“真实触屏”。实际证据为 Chromium `hasTouch: true` 上的 `locator.tap()`，可验证真实浏览器的兼容鼠标／焦点顺序，但未验证物理触屏设备。冻结 `docs/design/issue-92/ACCEPTANCE.md:38`（AC26）明确要求“移动只报告 Chromium 视口／触控模拟”，真实 iPhone／iOS Safari／软键盘仍范围外且 NOT RUN。

最小修复：正文改为“Chromium hasTouch 触控模拟中的兼容 mousedown”。原归档日志／历史报告应保持字节不变；在当前正文说明其精确执行环境即可，不需新增真机检查。

## 核验与保留边界

- 24 份归档副本逐字等于本地原件，并匹配每项声明 SHA256。未将历史 Standards 报告作为本轮独立 Spec 判断的替代；其档案身份与正文引用结果已核验。
- `recorded-outputs.json` 的 29 项 UTF-8 文本均能恢复与原件完全一致的字节／SHA256；包含首次 CI FAIL、局部 RED、successor GREEN、两次全浏览器 SIGINT／exit130，以及原始差异。文件自身 SHA256 为 `b349bcc7af8943c2c41a64ea30a1b2864f9815102cb3048e8095fdf7f6d16f3a`。
- 11 项源映射的 ID、源文件、行区间、原文哈希、owners、AC 映射全部与冻结提案对应项一致。D09 related-only、D04/D08 的 S03 组合责任与 R07 的 IME 子责任限定明确。
- 四个正文相对链接均在固定候选中存在。`docs/design`、src、tests、scripts、pyproject、uv.lock、package.json 与 lock 均未改变；原 proposal SHA256 `90bbc73416b33ac8428fb3f7122bea7c21c81e2eb8002b60cb5599773d174544` 匹配。
- `git diff --check 06588168 c96c26d` exit0。JSON 编码保存了原 patch 字节，没有新增忽略空白错误的规则。
- 核对 4da native200 原结果及 065 适用性声明：确为作用域有限的适用性意见；最终新资源 HTTP 字节仍待验证，IME 仍 BLOCKED。四安装环境历史记录绑定 4da；不会借旧产物证明 065。
- 当前 065 局部 16 PASS、typecheck PASS、assets/protocol 29 PASS 与既有 r3 证据一致；4da 24 PASS、50 次稳定性与四环境 HTTP 为历史资格。未提升两次中止全浏览器为 PASS。外部 startup 探针无法证明迟到响应成功投递的歧义也已正确保留。
- 支持回退必须保留三笔共享修复和 Inbox 默认入口；old `22c8720` 只作升级输入。新支持目标、同状态 G08、最终完整浏览器、安装产物与 CI 的 pending 在此冻结快照中如实保留，不作独立 finding。

复用已经通过且输入未变的 065 代码 Spec review。没有启动服务、重跑产品测试、修改产品／canonical／Git／GitHub，也没有读取另一轴本轮文档审查结论。静态机器核验结果见 `S11-ci-docs-r1-Spec-audit.json`；所有静态身份检查均通过，与以上两项语义 finding 分开记录。
