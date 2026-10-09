# S11 CI 修复增量

**当前验收仍为 FAIL，整项未完成。** 原37cd的必需 CI／组合失败尚待有效后继门禁及协调器裁定关闭；修复代码 **06588168d5049040abf0a4560091d659d244ddcf** 已获双轴代码复审 **PASS**，其最终全浏览器结果、修订文档复核及远程 CI 仍 **PENDING**；真实中文 IME 子责任保持 **BLOCKED**。这些状态分别绑定各自候选与范围，不把065认定为新发现的产品失败。本文不替换 [原冻结提案](acceptance-proposal.json)，也不把原失败改写为未执行或通过。

## 已修复的边界

| 原始观察 | 根因与修复 | 保留的约束 |
| --- | --- | --- |
| Aggregation、Database、Integrations 在新建后输入始终禁用 | 真实201未返回时再次打开同一 MainBar，呈现动作错误撤销创建意图。仅同一 opener 激活及同步自动聚焦不撤销；Chromium `hasTouch` 触控模拟中的兼容 mousedown 也属于该次激活。 | 独立焦点、阅读、关闭、编辑、离页、进程变化、另一 Run 仍阻止迟到切换；原会话草稿保留，只有一笔新建POST、一份Stream。 |
| 原 Inbox 返回A草稿断言失败 | 测试在B尚未同步时切A，忽略false；补接受Stream的屏障和切换成功／实际选中A断言。 | 未放宽产品连接守卫，没有已证实的草稿删除。 |
| 重连原期望一次读取却得到两次 | 计数混入之前run.finished／setup的延后历史刷新；补真实解码和历史读取完成边界。 | 仍要求一次本轮重连读取；其他Run不能确认目标已保存，后续明确核对才确认。 |
| 空Session／Run ID定位200后无正文 | HTTP身份和正文正确；首屏自动挂载改变页面代次误退役同页定位。改为实际导航代次。 | present-empty身份、普通历史失败、精确读取、记录状态、来源缺口、草稿与零POST原断言保留。 |
| 首次挂载前真实导航留下“正在定位” | 尚无旧页面controller负责清理。导航确认时立即通知MainBar复用既有retireLocation，不等待网络响应。 | 释放首SSE／旧响应前已清loading并取消请求；旧handler不能清新定位或抢SQL焦点。同地址、取消导航和首自动挂载不退休。 |

## 证据状态

- 当前代码已获独立 Standards／Spec 双轴 PASS（0 finding）；16项退休／创建／原失败相关检查 PASS；新增两种启动定位场景各5次共10 PASS；typecheck PASS；Python assets／protocol 29 PASS。
- 固定c96文档候选携带相同065源码，最终wheel／sdist × base／mcp四组安装全部 PASS，各100项HTTP断言、34项资产闭包、16条路由，源码污染检查均false。完整报告与产物SHA见 [安装证据](ci-repair-r1/final-gates/installations-03.json)。535项完整浏览器仍在运行，本快照不提前认定通过。
- 4da 历史资格：24项相关检查 PASS、10场景各5次共50 PASS、四安装环境各100 HTTP断言 PASS；因之后JS变化，均如实绑定其原候选。
- b214 和4da的两次完整浏览器运行在确认新问题后、改源码前正常SIGINT停止，exit130和中止理由保留，不能算完整通过。
- 原始 PR CI 522 PASS／5 FAIL，Push CI 525 PASS／2 FAIL，涉及6条不同用例。原日志可从 [可还原输出](ci-repair-r1/recorded-outputs.json) 中按UTF-8文本与SHA256核验。
- 原生200%在4da实际Chrome验证。065只移动真实导航的定位退休触发，未改几何、输入、发送、开合焦点或滚动；既有[适用性记录](ci-repair-r1/native/successor-065-applicability.json)已获代码复审认可，新增[最终安装字节绑定](ci-repair-r1/final-gates/native-installed-binding-03.json)核对四组全部资产与HTML均等于065。此为限定继承，未声称新的原生执行；全浏览器和远程CI仍需独立完成，中文IME保持BLOCKED。
- 触控检查使用 Chromium `hasTouch: true` 与 `locator.tap()` 模拟，验证浏览器兼容鼠标／焦点顺序；物理触屏、真实iPhone／iOS Safari／软键盘未执行且不在范围内。原归档文字保留，不扩大其证据环境。

详细候选树、三笔修复提交、逐源影响限定、原始失败、历史资格与待办见 [机器可读增量](ci-repair-r1.json)。独立诊断明确：D09只关联；D04／D08的S03是组合证据受影响，不代表已证实S03实现错误；R07不得掩盖IME子责任。

支持回退目标已固定为 **8d4adbc97cf1f5d55e3d36b65da7dbf2a1a1be2a**，在989662基础上依次保留 b21451f、4da39e5、0658816 的共享语义修复及Inbox默认入口。[新G08](ci-repair-r1/final-gates/lifecycle-01-portable.json)实际 PASS：三组独立安装产物在同一新空状态目录、同一保留浏览器页上连续执行 old22c→new065（build c96）→supported8d4adbc，各进程正常关闭exit0。消息、Session、草稿、清空后的display／price、记忆与遗忘完成及Ops一致性通过；未恢复数据库或删除锁。可移植派生记录只移除三处临时fixture CSRF token，显式保留原始结果SHA与脱敏字段。旧22c8720只作升级输入，不充当受支持回退目标。

原外部导航探针观察了真实200及悬挂loading，但其route.fulfill错误被捕获，不能据此声称受控迟到200已成功投递。永久回归的RED／GREEN在释放响应前直接验证立即退休与取消，并验证新定位不被旧handler清除，消除了该证据歧义。

本轮文档修正保留了c96的两轴FAIL报告及其原始字节，分别修复状态范围P2与触控模拟措辞P3；修订稿仍须增量复核。新安装、字节绑定和G08通过不会替代最终CI，也不改写冻结提案或canonical。
