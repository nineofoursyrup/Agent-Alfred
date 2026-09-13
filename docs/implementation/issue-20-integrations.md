# #20 可选集成与 Tavily 实施

权威合同是 `docs/design/issue-20-integrations-spec.md` 的 r3，与 GitHub #20
正文逐字核验一致。基线 `796b88c7d1e134906e25cf42f259d20381017fc2`。
工作树 `/Users/nineofour/Agent-Alfred-issue-20`，分支 `codex/20-integrations`。
设计交接及其原 manifest 原样携入；原目录、设计目录的未提交改动保留。

## 使用与归属

在启动环境或启动时确定的 `.env` 设置 `TAVILY_API_KEY`。Connections 的
“重新读取 .env”显式应用文件改动；进程环境优先。仅改磁盘文件不会生效。
Connections 展示配置、当前观测、历史时间、余额已报告字段和冷却时刻。
测试连接显式发送 `/usage`；免费尚未证实。打开页面、焦点、跨标签通知和
Run 完成只读取状态。生产网络固定为 `https://api.tavily.com`，无 URL 设置。

在 Tools 将 `web_search` 授权设为 allowed 才开放已配置能力。无 key 且未拒绝时
保留空参数引导；缺依赖或无效凭据隐藏执行能力，并显示独立原因。
授权仍由既有文件和保存/生效协议拥有。配置发布失败保留一致旧视图；无法回滚时
暂停 external，修复后显式重读恢复，不自动修改授权。

`integrations.py` 拥有字段声明、可用性和 Tavily 观测；`integrations_http.py`
负责单次 urllib 请求、禁重定向、代理、协作式 deadline、分块读取与字节限额。
Registry/Loop/SQLite/计量沿用原链。新增 `NotSentCost` 只表达工具 transport
确认 HTTP 未发送的证据，保留 callable 已进入的启动事实。业务未知用
`tool_result_unverified` 停止本 Run 并固定解释；仅成本未知不停止。

credits 从 JSON 直接读为 Decimal；Ops 按 service/unit 分组，与模型金额分列。
汇总提高局部 Decimal 精度，避免默认精度舍入合法 fractional credits。
来源是脱敏后的选定字段；不回传原始错误正文，不自动访问来源，浏览器安全文本展示。

## 验收定位

公共 Python 证据统一位于
`src/agent_alfred/evals/deterministic/test_integrations.py`，默认模型为
ScriptedModelFactory，持久化为真实临时 SQLite，HTTP 为真实回环 urllib。
浏览器位于 `tests/browser/integrations.spec.js`，由
`tests/browser/integrations_server.py` 装配真实 Dashboard/Host/Registry。

| CE | 可定位用例与观察 |
|---|---|
| CE-01 | `unconfigured_guidance_reaches_next_step`、`six_cells`：完整六格、Schema、下一 Step 指引、HTTP 数、启动与计量 |
| CE-02 | `probe_is_explicit_cached_and_window_bounded`、`429_cooldowns_are_local_and_separate`；双标签第 1 例：真实 gate 409、失败缓存、第 11 次拒绝、600s 边界、换 key 保留宿主窗口 |
| CE-03 | `reread_is_atomic_and_preserves_authorization`、`same_key_probe_cache_cannot_overwrite_search_error`；双标签两例：旧凭据及同 key 旧成功最后交付不覆盖新观测 |
| CE-04 | `entire_search_outcome_table`：200/400/422/401/403/429/432/433/500/302、对象/数组/非 JSON，实际连接和 Run 后续调用数 |
| CE-05 | `fractional_credits_and_empty_success`、`decimal_cost_survives_restart_without_trace`、`ops_sum_preserves_reported_fraction`：缺失/非法/零/高精度小数、trace 删除及重启、真实 Ops；浏览器第 2 例显示 credits |
| CE-06 | `bad_200_stops_batch_and_model`、`control_exception_keeps_both_ledgers_unknown`、`metering_failure_stops_further_io`、`io_ownership_blocks_mutation_until_late_response_exits`：两账、控制异常、停止、未发送投影、shutdown 真实所有权 |
| CE-07 | `new_identity_searches_and_replay_never_resends`：真实多 Step、Registry 与 ledger 两层回读；`io_ownership...`：未知后新的明确用户请求可再查同 query |
| CE-08 | `all_projections_redact_and_redirect_never_follows`、`real_cli_failure_does_not_print_provider_secrets`、`proxy_errors_never_fall_back_to_direct`；浏览器第 2 例安全正文，无额外来源请求 |
| CE-09 | `invalid_inputs_send_nothing`、`invalid_source_rejects_whole_result`、`actual_byte_limits`、`truncated_or_unsupported_response_is_unknown`、`io_ownership...`：真实编解码、exact/+1、慢回包、关闭，浏览器来源 HTML 作为文本 |
| CE-10 | `distinguishes_start_and_send`：真实 SQLite trigger 消耗 intent 预算、工具已进入但 HTTP 未发送、收到 POST 后预算耗尽，分别断言启动/请求数/成本/Run |
| CE-11 | `injected_publication_failures_keep_consistent_view`：真实文件读取/解码、精确能力准备与发布故障、回滚失败暂停、显式恢复；双标签旧 UI 回包 |
| CE-12 | `invalid_key_keeps_permission_but_hides_execution`、`declarations_and_missing_extra_are_separate`：独立 extra 存在性夹具、真实 Registry 执行拒绝、重复 integration/field/tool 身份装配失败 |

初始 red 与阶段证据在 `.scratch/issue20/` 的编号日志。已有行为首次即通过的
用例只记录 PASS，不宣称新的 red。具体新增失败包括缺少集成装配、结果未知收尾、
坏 `.env` 发布、探活入口、启动前预算结局、控制中断账目、截断 HTTP、汇总精度。

最终候选 manifest、项目门禁、双独立评审及隔离安装回执在同一证据目录。
manifest 同时覆盖 staged、unstaged 与纳入 untracked，不使用 HEAD-only 差异验收。
没有真实 Tavily/模型请求；没有 commit、push、merge 或 Issue 关闭。
真实服务验收仍待单独明确授权，不把离线 PASS 称为 live PASS。

## 本地补充合同：不可表示的 JSON 数值

实施独立评审用 54 字节响应
`{"results":[],"usage":{"credits":1e9999999999999999999}}`
重现：数学上有限的 JSON 数值超出 Python Decimal 可表示范围。
用户在当前实施会话明确选择“采用建议，补充此数值边界”：这种 credits 为
unknown，保留有效 results 并继续；不会把数值转换异常扩大为业务结果未知。
原 r3 及其哈希保持不变，本段与 r3 共同构成当前离线验收合同。
用例 `unrepresentable_optional_decimal_is_unknown_cost_not_lost_result` 验证两次
合法新 call 都成功回喂且各自成本 unknown。

## 独立评审修复

v1 Standards 三项、Spec 两项（重启问题在两轴各自报告）均按具体反例修复：
配置暂停不由授权 save/reapply 解除；配置与观测失败恢复到同一检查点；
任何 external callable 逃逸异常保守停止未知结果；Connections 对齐进程身份，
并保留比旧缓存成功更新的失败提示。新增真实 Host 控制中断、轮换新旧 key 的
HTTP/audit/trace 扫描，以及 Chromium 对真实 SSE 消息的密钥扫描。

追加公共证据：`host_preserves_interrupted_control_and_closes_http`、
`unexpected_transport_exception_never_continues_unknown_run`、
`rotation_sends_only_new_key_and_redacts_both_in_history`；浏览器五例包含真实
Host 重启、旧进程回包、新失败提示和旧/新密钥 SSE。具体总数与最终无漂移
候选以 `.scratch/issue20/review-v2/` 及其后继 manifest/日志为准。


v2 Standards 新增一项授权恢复回归已修复：授权模块清除自身的
`authorization_write_unconfirmed` 暂停，仍保留配置模块的暂停。
`test_ce11_authorization_write_failure_can_recover` 对真实 Host 注入一次
授权文件写入 OSError，分别经 save/reapply 恢复，断言授权/暴露一致且
后续真实回环搜索恰好一次。两条用例先重现 hidden 错误，再验证恢复。
最终候选与全部门禁、双轴复审、wheel/sdist 安装证据在
`.scratch/issue20/review-v3/`；逐 CE 最终结果见该目录 `acceptance.md`。


## 本地补充合同：报告数值的展开上限

用户在实施会话明确回复“可以，按你建议”，采用以下第二项数值补充：
单项 credits 的十进制定点表示超过 4096 位时记为 unknown，保留有效
搜索结果并继续；`/usage` 的 plan_usage/plan_limit 使用同一上限，
超限字段显示未报告，不否定 connected 或其他有效余额字段。

位数包含整数部分（小于 1 时的前导 0）和小数部分，不计小数点及符号；
不做舍入或删除报告的小数尾零。恰好 4096 位保留，4097 位拒绝。
正指数零的定点表示仍是 `0`；负指数零保留其报告的小数位并应用相同上限。
实现从 Decimal 系数与指数计算展开长度，在任何定点格式化之前拒绝超限值。
单项上限不截断多个合法报告之和，Ops 仍精确相加。

`test_ce04_ce05_reported_number_expansion_is_bounded` 经真实 Host、Registry、
Loop、SQLite、回环 urllib 分别覆盖 search/usage，共 22 个边界：整数、
小数和系数的 exact/+1，正负百万指数、零，以及两次合法新 call 与 Ops
精确汇总。修复前 12 failed、10 passed；不把原本通过的边界称为新的 red。
原 44 字节 `credits=1e1000000` 反例现为结果成功、费用 unknown、Ops 可读。
本补充与前一不可表示 Decimal 补充均经确认；原 r3 文档及哈希保持不变。

最终冻结证据目录为 `.scratch/issue20/review-v4/`，逐 CE、门禁、隔离安装、
双独立评审与最终哈希回执统一见该目录 `acceptance.md`。

浏览器第六例从真实 `/usage` 显示“已连接 / key 套餐用量：未报告”，
再经 Tools 授权、MainBar 成功来源到 Ops 显示 unknown=1、reported=0；
实际请求恰为一次 usage 和一次 search。该新增浏览器证据首次即通过，
不宣称单独 browser red。
