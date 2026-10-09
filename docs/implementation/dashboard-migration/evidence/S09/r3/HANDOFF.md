# S09 r3 固定候选交接

修复并验证了正式 `S09-r2-Spec-F1 / P2` 的来源阅读位置缺口。实现者的有界验证通过，原正式 FAIL 保留；固定候选等待独立 Spec 与 Standards 增量复审，不能由本交接关闭该 finding 或提升整项验收。

- worktree：`/Users/nineofour/.codex/worktrees/dashboard-s09/Agent-Alfred`，branch：`codex/dashboard-s09`，clean。
- 最终 head：`3139d8a1055619af4c71d89e780633effbb9f595`；tree：`8c1cad94125d6d5680ef570e40a36643aa9b671f`。
- 父任务明确接受并授权同步的 base：`da7d6b84899b19d690d574c74397a45b0239078b`；tree：`20821980c0bb7977c157416a52db5ac80327b307`。已验证它是最终候选祖先。
- r3 修复 commit：`59ac9317e87b77b144764cd6eac017caf69eebdf`；修复前 r2：`190bb1b3a763fd0cce73029dcc20a67297a173d2`。接受的 S05 在最后 merge commit 同步。
- 最终相对 accepted base 仍仅 7 文件，`+905 -54`；r2→r3 自有修复仅 `accounting.js` 和既有 `accounting-migration.spec.js` 两文件，`+71 -19`。没有修改 app、Shell、MainBar、Stream、Run consumer、计价或数据规则。

## 实际缺陷与窄修复

正式首次观察为真实 55 Run 的 Ops offset50 → 已迁移 Run → 显式返回来源：原 snapshot/process/filter/Run、一个 GET offset50 和零 POST 均正确，但 page scrollTop 从8087变成0；焦点虽在来源链接，链接却位于 y2202，超过 page y65–800。

Ops `captureSource` 现在用原 trigger 保存相对 `#page` 顶部的一个数值 `reading_position.anchor_offset`。取得可信原快照及所选 Run 明细、生成原链接后，才按该锚点恢复滚动与焦点，并将目标限制在当前可见高度内。原列表有55行，返回只有5行，因此不复用原绝对高度。已有 Shell `scrollTop` 字段在存在可恢复锚点时记为0，让 Ops 负责这次位置恢复，避免共享 Shell 在异步增长时重放不适用的绝对位置。没有修改 Shell，也没有增加 API/SourceContext 的业务权限；I04 本来允许必要的非正文滚动锚点。

返回来源仍只含原 route、snapshot_id、process_instance_id、归一化 filters、offset、run_id、trigger 及上述数值位置。没有正文、金额、凭证、通用内容缓存；没有扫描前置页或创建替代快照。snapshot/detail 身份核验、请求代际和原财务读取规则保持。

原 pointer/key/focus 取消恢复保留，并增加 passive wheel/touchstart 取消与卸载清理。异步结果只在没有更新阅读意图时移动视口和焦点。测试实际发送 wheel 事件；没有声称执行真实触摸或移动设备测试。

## 首次失败和现候选观察

原 reviewer 的 report/details/JSON/probe/PNG 不改写，见 [first-failures.json](./first-failures.json)。扩展的是同一个真实 `migrated Run explicit return` 用例，在1280×800与390×844执行。`red-01.log` 两项先于产品修改在 r2 真实失败，均是 `visibleInPanel=false`；`green-01.log` 修复后两项通过。

最终接受 S05 后的 `final-01.log` 三项通过：两种宽度的完整来源位置／新意图场景，以及原快照 reload／失败／断连显式重试。每种宽度包含三次往返，保持原完整 snapshot、instance、normalized 7d/Shanghai/purpose、Run、trigger，分别仅一次 GET offset50、五行、零 snapshot POST。

| 最终实测 | 1280×800 | 390×844 |
| --- | ---: | ---: |
| 无新意图的原链接顶部 y | 424.484375 | 443.90625 |
| 返回后的原链接顶部 y | 424.109375 | 443.53125 |
| 链接完全在 page 可视范围内／有焦点 | 是／是 | 是／是 |
| 新输入发生后保留 Run ID 草稿与其焦点 | 是 | 是 |
| held detail 期间真实 wheel 后的 scrollTop，释放后仍保留 | 240 → 240 | 240 → 240 |

原绝对 scrollTop 从8087/8554变成1778/2245，是有界五行页面高度变化的正确结果；来源相对位置误差不到0.5px。新输入或新滚动反例中旧链接可以处于屏外，因为恢复已被用户新意图取消。最终两个正常返回截图已实际查看，链接及所选 Run 的阅读上下文可见；六组原始几何与完整 request/source readback 保存于 `final-01-results/**/source-position-observations.json` 和 `migrated-source-roundtrip.json`。

`related-01.log` 六项通过：成功和失败 refresh 的旧 paging 退休、新 pending page 所有权、原页／refresh retry、native Back／expiry、原 snapshot reload／read-error／disconnect retry、原 A23 请求顺序、过期 Run 的显式出口。其中 direct retry 在最终 S05 bundle 重复执行；其余五项适用性由相同 Ops/Run/shared/test 输入和最终真实 HTTP 验证绑定。R3 一共 **8 个不同用例**：2个新几何视口用例＋6个相关用例，最终3次重复不算新增，总计11次 PASS 执行。

## 接受同步、资源闭包和检查复用

接受 S05 的变化为 Memory 页面与CSS、资源注册以及 listener backlog。唯一 index.html 冲突的原日志与三阶段内容保存在 `merge-s05-conflicts/`；最终采用 accepted index 全部内容，加 accounting.css 一行。assets.py 自动合并后也验证为 accepted＋accounting.css 一项。所有非本片文件与 accepted 完全同字节，含 Memory、listener、S03、S06、S08、S10，见 [identity.json](./identity.json)。

最终 typecheck PASS。`resource_inventory_s05.py` 从真实 HTML script/stylesheet 出发，用 Node SourceTextModule 解析实际 JS import/re-export 并遍历 CSS 引用；真实同源 HTTP 共 **34 项**，accepted baseline为33项，唯一新增 accounting.css。每项 source bytes/hash、MIME、CSP、no-store、nosniff 均核对；见 [resource-binding.json](./resource-binding.json) 和 `resource-final-inventory.json`。此前565基线上的33资源原记录保持不变。

回滚闭包需同时恢复 accepted accounting.js、index.html 与 assets.py，并删除 accounting.css；只验证了结构闭包，没有执行产品回滚，也不声称旧 main 对最终组合安全。

没有重跑无关全套。原66项后端通过、价格／历史／计量等片内观察、Ruff/skills/env检查及已有四视口和宽度边界布局仍标为原执行结果，并给出逐项复用理由。r3只改来源位置恢复，不改相关计算/正文/渲染结构/CSS；接受 MemoryCSS 只匹配 memory-* 类。S05 listener 只改 backlog，不改变 handler/Host-close 所有权；原后端 HTTP-close 用例保持旧候选证据，最终真实 fixture 则各自正常关闭，不把旧66项称为本次新执行。最终已运行的三项浏览器检查和34项HTTP覆盖当前模块与listener组合。命令、输入候选、环境、日志SHA和限制均列在 [checks.json](./checks.json)。

## 原始责任、资料完整性和后续

[source-coverage.json](./source-coverage.json) 保留全部 **41 source / 23 AC** 的完整原始对象、owner、AC/G关联和未完成责任，机械比较固定 acceptance-map 完全相等。F20–F23是本轮新增的有界观察；F16 原来只证明焦点身份，正式 Spec F1 已明确其不能证明可视位置，旧记录及失败没有被覆盖。所有 source、AC、whole-product 仍为 **NOT RUN**。原完整合同阅读按当前同字节输入继承，见 `read-and-inheritance.json`。

原 R1 **152项**、R2 **96项**、正式 Spec原件和本轮同步前 **34项** 的哈希与字节全部保持；`preservation-before/after.json` 与 `before-s05-preservation.json` 可复核。新几何 RED/GREEN、merge conflict、原 parser/typecheck/fixture/visual first failures和历史 BFCache BLOCKED都保留。

后续由父任务安排当前固定候选的独立 Spec/Standards 增量复审。S11仍负责整套来源／AC以及 G01/G02/G04/G05/G06/G07全链、Models清除与探针、最终四种隔离安装及包资源、最终完整CI和升级／回滚。真实桌面200%缩放和中文IME为 NOT RUN；本片移动仅Chromium viewport，真实iPhone/iOS Safari/软键盘不在本片验收声明中。父任务另行跟踪的peer远端CI失败未由本片关闭。

没有修改 ledger、原合同或 GitHub，没有 push/main merge/部署/真实provider操作。最终工作树 clean，assigned18000–18009无监听，所有自有fixture正常close。候选固定后停止实现，交由父任务复审。
