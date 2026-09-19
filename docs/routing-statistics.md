# 路由统计

实施与验收合同：[ROUTING-STATS-SPEC-r1 / #76](https://github.com/nineofoursyrup/Agent-Alfred/issues/76)。

Behaviour 的「路由统计」默认收起。展开、重展开、切换范围、刷新时读取一次持久快照；没有轮询，不影响设置和聚合表单。关闭消息分流仍可读历史。切范围或刷新会移除上一份结果；失败单独显示并可重试。页面采纳新进程身份或断连时会撤销在途读取、清除结果；下一次主动读取使用新身份。

`GET /api/behaviour/routing-statistics?window=7d` 接受 `24h`、`7d`、`30d`、`all`，默认 `7d`。窗口按服务端 `as_of` 和 Run 的 `accepted_at` 取 UTC 半开区间。响应含 `read_id`、`process_instance_id`、`from_at`、`as_of`、当前语义身份、样本计数与独立版本组。

- `sample.admitted = enabled + disabled + unknown`，只含 CLI/Web、purpose=chat、admission_state=admitted；每 Run 一次。
- `sample.enabled = finished + pending`；已启用且持久 finished 者进入版本组。结果缺失显示 unknown，不使用内存终态或 trace 补数。
- `groups[].decisions` 含五类次数、known、none、unknown、各自分支比例和决议覆盖。`fallback`、`recovered` 各有 yes/no/unknown、已知分母、发生率和覆盖。`bypass` 只给计数；`blocked` 单列次数与闭合原因。
- 所有比例同时返回整数 `numerator`、`denominator` 和可空 `value`；分母为零时 null。未知版本组的所有 value 为 null，不产生混合百分比。

迁移 v19 仅新增可空 `runs.routing_admission`，历史不补写。新准入保存 schema_version=1、enablement 以及稳定语义身份 `routing-statistics-1` / `message-routing-1`；它们与图代际、拓扑哈希、模型配置不同。统计口径或分类提示词/路由保护规则改变时，应显式更改相应身份及支持的读侧合同。未支持身份归历史未知，不能按当前规则猜解。

执行侧在内存保留已提交 route_decision、已提交 context_recovery、实际进入回退/绕行和 blocked 原因。`memory.routing_statistics` 与现有 telemetry 在唯一收尾事务保存。崩溃恢复只保留准入资格，缺失结果不补造。schema1 历史只按 #76 的保守判读表读取。图未进入与已提交决议/恢复、实际回退与 blocked，以及两份可识别摘要的矛盾，会降低相关轴的已知覆盖，其余可信事实保留。

查询在独立只读子进程内建立一个 SQLite 读事务；服务端从准备查询起使用单调时钟限制 2 秒，涵盖启动、锁等待、SQL、JSON 判读和结果构造。父请求到期或发现断连时终止子进程、等待退出、关闭管道，再返回；不会取消业务 Run。构造与清理共用可恢复 owner；清理持续失败时保留于异常链及 Host，下次读取或 Host 关闭继续释放，不把尚未回收的 worker 当作完成。SQL progress handler 同时限制正常扫描。成功没有分页截样。

错误：400 `invalid_window`；503 `recording_unavailable`（读取不可用）、`statistics_invalid_data`（关键样本归属无法判定）；504 `query_timeout`；499 `query_cancelled`。失败不含部分统计或伪零。页面用文本节点显示数据。

验证入口：`test_routing_statistics.py`、`test_routing_statistics_lifecycle.py`、`test_schema_v19.py` 与 `tests/browser/routing-statistics.spec.js`。模型是声明的外部替代，准入、Graph、Registry、SQLite、CLI、HTTP 与页面均保留真实组件。手工裁剪测试实际删除封存 trace bundle 并经 `schema.record_trace_prune` 保存 absence；现有基线没有自动保留器，因此未声称测试自动保留策略。
