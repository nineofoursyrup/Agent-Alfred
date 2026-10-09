# PR120 后继证据适用性

本文件是待独立核验的交付提案，尚未采用为最终产品完成结论。原始 `acceptance-results.json` 已逐字节保存在 `acceptance-before-closeout.json`。产品候选由 `candidate.json` 的完整 tree、src tree 和七文件 manifest 固定；暂未提交，不以旧 HEAD 冒充后继字节。

## 产品改动与既有证据

本轮产品只改 `app.js` 的精确定位记录归属和定位响应退休、`models.js` 的未知 unpin 回执与编辑资格、`overview.js`/`run-fields.js` 的 `inference_probe` 用途标签。四项原 P2 及修复后的独立 Standards/Spec 结论分别保存在 `review-pr120-20261009`、`recheck-pr120-20261009`；代码复核只覆盖四修复及直接回归，不声称重审整个 PR。

- 原完整 535 browser PASS 留在 11dcc/065 的观察身份。本轮 16 文件的 114 个唯一适用 PASS 由原始混合/后继日志逐项核对，见 `repair-pr120-20261009-7wpdbm31/validation-summary.json` 与 `recheck-pr120-20261009/review-summary.json`。未将首个混合失败日志称为全绿，也未宣称运行了后继完整浏览器套件。新远端 CI 将执行完整套件。
- Python 实现、Python 测试、依赖、配置没有本轮改动。旧完整 Python 的确定性结果限定复用；四个 JS 资产变更由本轮 wheel/sdist 四独立安装、包内620文件和 HTTP 闭包重新核对。远端 CI 的完整 Python 是独立项目门禁。
- 309 个 source 的原文 hash、owner、AC/G 映射及全部既有 FAIL/UNKNOWN/BLOCKED 记录保留。649 个原 PASS owner fragment 继续引用原实现者和评审身份；本轮受影响的 MainBar 定位、Models 回执及 probe 标签由四项修复/114测试补足。7 个只欠原生 IME 的 source 共25 owner fragment 追加新证据后重新聚合；不是机械覆盖旧状态。
- AC06/07 与 G01 的定位退休/读取边界使用新的 location/reading/integration/empty-identity/streaming 结果。AC19/20 与 G06 的回执/编辑资格使用新的 models-migration/settings 结果。AC08/11 与 G02 的用途展示使用新的 run-purposes/Overview/Run 路径结果。未改 Host/SQLite/API/价格/工具/MCP/遗忘等原行为，G03/G04/G05/G07 的原真实组合与 MCE 原证据保留；最终 CI 仍会执行其现有套件。

## 原生 IME 与原生200%

`native/result.json` 绑定当前产品资源以及四安装 HTTP 闭包。macOS 原生 Chrome App.pressKey 在用户手动切换输入法后产生 trusted compositionstart/update 和 insertCompositionText，确认 Enter 时 isComposing=true；确认和 Shift+Enter 后数据库均0 Run/0消息。随后真实中文候选“你”组成正文 `ni\n你`，普通 Enter 仅产生一笔已完成/已保存 Run。回复前通过原生输入生成新草稿“好”并点击中央 IANA 字段；完成后草稿与中央焦点保留。fixture stop 正常 exit0。

原始事件的 compositionend 为 trusted=false；这项观察完整保留，不冒称所有事件 trusted。原生事件来源根据 macOS App.pressKey、trusted start/update/input、真实中文候选及用户操作共同认定，不把该 compositionend 单独当真实性依据。输入源名称也完整保留：用户反馈“拼音－简体”，helper进程TIS读到 `com.bytedance.inputmethod.doubaoime.pinyin`；只主张真实 macOS 中文 IME 覆盖，不主张已精确验证 Apple 输入法。

测试 HTML 仅追加同源只读事件监听脚本，全部产品 JS/CSS 原字节不变。记录器不派发输入、不设置值、不改变焦点；源码与产物hash绑定见 `native/served-product-manifest.json` 和 `installation-results.json`。测试使用离线 ScriptedModel；为迟到响应留12秒/模型调用，出现的 selector/gate model_deadline 降级属于该受控夹具，不是实际外部provider结果。

CE-14 的“202前编辑新草稿”，继续使用 `tests/browser/shell.spec.js` 的 `late accepted response preserves new chat input and the users central focus` 及相关既有用例；其处理函数本轮未改。原生本次只新增真实组合/换行/发送/回复后焦点草稿证据，不冒称重新控制了202前时序。`accessibility.spec.js` 的合成composition结果仍仅作为补充。

真实200%使用 `native-ci-r2/result.json` 的4da执行和 `successor-065-applicability.json` 的已批准适用性，再增量核对本轮四文件：没有几何、CSS、壳层断点、输入/发送键处理、关闭入口、焦点/滚动规则改动；定位专属历史投影更正和异步读取退休不改变200%用例路径。新四安装资源字节与当前原生IME页面相同；没有宣称200%被重新执行。移动真机仍 `excluded_by_user / NOT RUN`，历史BFCache BLOCKED保留。

## 打包与支持回退

当前 wheel/sdist × base/mcp 四组均在隔离环境执行真实安装、导入与HTTP；每组620包文件、34资源闭包、16入口/深链、100 HTTP检查，见 `installation-results.json`。新wheel SHA256 `55648a28cdfae7ca479033739ace599ab8f22546ebd5d03b5214d7d40cc96e5a`；旧包证据不改写为本轮结果。

支持后继由8d4精确携带七修复文件；与current全部产品文件只差已批准的 `index.html`/`shell.js` Inbox默认入口。支持manifest/tree及实际安装文件核对见 `support-preparation/candidate-identity.json`、`installed-source-binding.json`、`final-verification.json`。新G08执行现有 `check_dashboard_lifecycle.mjs`：三个新独立环境、同一全新隔离state、保留标签 old22c→current→supported，三次正常退出，保留消息/草稿/清空值/记忆/遗忘/账目，刷新零自动POST。原始结果保留；公开副本只去掉三处临时fixture CSRF token，转换路径/hash有记录。

## 当前终点

本地提案预期为309 sources PASS、674 fragments PASS、25AC PASS/AC25 BLOCKED、12MCE PASS、G01–G08 PASS。AC25仍等本轮独立证据复核与新PR/push CI；整体 completion=false。后续还须以预期head合并、实际merge SHA CI、逐票验收回写及REST/GraphQL CLOSED/COMPLETED回读。既有授权覆盖这些动作，不覆盖部署/发版/用户业务数据或清理主工作区。


## r2 证据精度修正

原 r1 提案及两项 Spec 证据发现分别保存在 `proposal-r1/` 和 `evidence-review-first-findings.json`。SPEC-EVID-02 已将 AC25 单项结果改为规范枚举 BLOCKED，整体仍 completion=false。SPEC-EVID-01 由新独立隔离 `native-repeat-r2/result.json` 补足：首次提交后、202返回前，输入框连续三次 Enter（首次加两次重复），随后点击已禁用的发送按钮；原始事件显示三次 Enter 均早于 Host 收到 POST。Host 随后受理首个 POST，HTTP 202 在夹具边界被保持；保持期间写新草稿并移焦中央字段。保持期间服务端只收到一次 POST/只有一笔 Run；释放202及真实回复完成后新草稿与焦点保持。该条使用 CUA 浏览器 UI 与粘贴测试文本，仅是受控准入交错补证，不冒称第二次原生 IME 执行。产品文件hash与首次原生试次完全相同。

首个重复准入试次的行为结果与本地观测夹具 AttributeError 分开保留于 `native-repeat/`（diagnostics.json 判定夹具首试FAIL）；后继仅修夹具对尚无path的请求使用getattr，产品字节不变，重新执行同交错后正常close0且无该AttributeError。后继日志另保留初始浏览器导航中止对应的GET BrokenPipeError；成功重取页面后的被测请求/回执/持久状态与退出均可核验，不声称日志无诊断。
