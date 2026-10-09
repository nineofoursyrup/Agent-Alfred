# #91 完整用户路径与反例

对应 [MODELS-CONNECTIONS-OPS-DATABASE-DESIGN-r1](DESIGN.md)。状态：**待产品实现后执行，当前全部路径为 NOT RUN。** 已有测试是可复用入口，不因列在本文就成为本轮通过证据。

验证以当前页面、真实本地 Host／HTTP／存储／查询 worker 为主体，外部边界使用既有离线模型、目录与集成夹具；不以真实凭据、付费探针或生产账户作为默认条件。新增断言按实际失效方式补足，不照抄实现或为凑数量建立用例。

## 完整可见用户路径

| 路径 | 用户操作与可见结果 | 设计依据／复用入口 |
| --- | --- | --- |
| P01 模型目录与指派 | 进入 Models → 看主／辅助指派及跟随关系 → 展开另一端点 → 钉选候选 → 指派 → 取消非指派项；连接、目录、支持三轴保持，已指派项禁用取消并解释 | R02；`settings.spec.js`、`test_models_page.py`、`test_settings_commands.py` |
| P02 单项保存 | 修改显示名、手动线路和四维价格 → 未保存提示 → 分别保存 → 回读真实保存值 → 折叠重开；没有输入即提交或全行原子保存假象，提交后新编辑仍在 | R03–R04；`settings.spec.js`、设置命令与存储用例 |
| P03 覆盖价与显示名清除 | 已有显示名 → 清空保存 → 回到无显示名覆盖；已有单个／多个覆盖价 → 清空一维／最后一维 → 验证保存状态和价格链回退，显式 0 仍保留零价 | R03–R04、R12；扩展 `test_settings_commands.py` 与真实页面路径，不能只测初始 None |
| P04 双冲突与故障 | 两标签编辑同一设置 → 另一标签先保存 → 冲突保留草稿／核对后显式再保存；另模拟磁盘外改、损坏及较新 schema，页面反馈和文件保留符合原合同 | R03；`test_model_settings_store.py`、`settings.spec.js` |
| P05 设置回执与目标 | 保存中继续改同字段／另一字段，或切详情 → 延迟成功／失败 → 原回执只更新原请求；断连／换实例／离页后返回，保留未确认事实而不重发 | R03；现有设置／HTTP／壳层套件扩展 |
| P06 具体模型推理探针 | 两指派模型可在同一或不同端点 → 从各自行发起测试 → 观察提交／受理和准确 Run 链接 → 查看 Attempt、费用与记录；未保存编辑不参与，无新增会话消息 | R05；`test_inference_probe_target.py`、`test_runtime.py`、`settings.spec.js` |
| P07 探针拒绝与丢响应 | 缺凭据、过期目标、不支持、全局忙和服务故障各触发明确反馈；丢 202 响应保持待核验，恢复连接不再 POST，无可靠 ID 不猜测关联 | R05；推理目标、HTTP、准入和浏览器失败路径 |
| P08 凭据重读与端点 | 进入 Connections → 展开地址／变量／掩码凭据 → 重新读取 .env → 查看受影响观测重置和各服务状态；不存在的免费认证探针如实说明 | R06；`settings.spec.js`、连接与凭据确定性测试 |
| P09 可选集成 | 展开 Tavily → 看观测、最近尝试与 key/account 用量 → 显式测试 → 分别观察成功、400／422、探测 429、搜索 429 和缓存／冷却；旧观测与本次失败不混合 | R06；`integrations.spec.js` 与集成服务用例 |
| P10 MCP 维护 | 查看启动许可／工具可用数 → 应用配置 → 逐服务器核对 → 重连 → 清理未完成时继续清理 → 丢回执后重试同一操作；工具授权仍从 Tools 查看 | R06；`mcp.spec.js`、MCP 控制器／清理用例 |
| P11 Connections 更新归属 | 在详情展开状态下触发窗口 focus／跨标签通知 → 延迟读与维护回执交错 → 看较新状态和未生效提示；折叠仍保留进行中及异常摘要，不重复维护 | R01、R06；`settings.spec.js`、`integrations.spec.js`、`mcp.spec.js` |
| P12 账目范围与汇总 | 从 Ops 和 `/ops?tool=<identity>`／精确 Run 入口进入 → 选择时间／时区／Session／用途 → 刷新 → 看实际范围、费用分项、工具计量和覆盖；工具筛选仍呈完整 Run | R07；`accounting.spec.js`、`test_ops_snapshots.py` |
| P13 快照一致性 | 固定一次快照 → 翻页／查看 Run → 期间后台新增账与修改价格 → 旧页仍按原范围／价格 → 显式刷新才变化；刷新失败保留原快照及筛选 | R07；`accounting.spec.js`、`test_ops_snapshots.py` |
| P14 账目明细 | 选择一条 Run → 看 Attempt 的 exact／estimated／unknown，展开参与计费维度 → 看工具身份、启动／结果／成本；不把全局 summary 当所选 Run 总额 | R04、R08；账目浏览器、价格与快照用例 |
| P15 历史正文与核验 | 在工具请求选择模型投影／审计／提交参数 → 逐段读取 → 独立核验原操作并查看关联恢复 Run → 切另一 Run；原当次结果不被核验覆盖，旧正文及迟到响应被隔离 | R08；`accounting.spec.js`、工具历史与核验用例 |
| P16 过期与跨页返回 | 快照过期／回收／换实例 → 续页或未加载明细被拒、保留已加载财务内容标旧 → 进入／返回 Run 时保留固定期间／时区 → 显式刷新；离线历史重连核验后才能续读 | R07–R08；`accounting.spec.js` 的过期、离线／裁剪和 Ops→Run 返回路径 |
| P17 Database 目录与替换 | 核验目录 → 展开对象／字段／限制 → 示例写入 SQL 而不执行 → 手改 SQL → 选择另一示例 → 保留／放弃分支；目录折叠不丢输入，真实离页仍走守卫后清理 | R09；`database.spec.js`、#87 壳层用例 |
| P18 SQL 与结果关联 | 显式执行查询 A → 在途改草稿为 B → A 结果只标属于 A、提示草稿变化 → 下一次明确执行 B；空结果、语法错误、类型／重复列名和长值各如实显示 | R09；`database.spec.js`、SQL／对象确定性用例 |
| P19 结果分页与复制 | 查询获得超过一页结果 → 看当前行段、本次返回数量及截断／覆盖／来源说明 → 翻页 → 复制 SQL／本页结果；复制结果只含当前页与必要说明，不偷偷补查询或全量导出 | R09；`database.spec.js` 与剪贴板可见行为扩展 |
| P20 签发、忙态与取消 | 双击执行 → 仅一条查询；句柄迟到前点取消 → 迟到句柄被取消而不执行；已执行／取消前完成／丢正文／旧状态晚到逐一显示真实结果及清理 | R10；`database_cancel.spec.js`、`database_status.spec.js`、`test_database_http.py` |
| P21 预算与实际清理 | 超输入准备限制、结果行数／字节上限、65 列查询、5 秒预算、worker 停止失败 → 分别显示行／字节截断、列数超限拒绝及独立 cleanup；65 列不静默裁列 → 只有释放与能力确认后才接受新显式查询 | R10；数据库边界、HTTP、生命周期确定性用例及状态浏览器用例 |
| P22 保护失效与文档生命周期 | 查询中／有结果时真实删除记忆、保护变化、断连／重连、隐藏标签、打开主对话、实际离页、reload／close／返回 → 按各自规则清理／保留且不重跑，旧结果不能复活 | R01、R10；`database.spec.js`、`database_lifecycle.spec.js`、`database_native_lifecycle.mjs` |
| P23 四页窄屏与键盘 | 在四个参考视口，从主对话切回中央 → 完成模型保存、连接维护、账目阅读、SQL 执行／取消与复制 → 使用纯键盘、文字放大和减少动效 → 操作／提示／滚动可达 | R01、R11；#86/#87 与上述各实际页面套件 |

## 具有独立失效方式的反例

| 反例 | 不可发生的结果 | 关联路径 |
| --- | --- | --- |
| N01 正交状态被折叠 | 端点 connected 就把 unsupported 模型显示为支持，或端点 unconfigured 阻止本来合法的指派 | P01 |
| N02 目录读取扩大 | 所有组头上屏就拉全部端点；无凭据也发请求；同端点刷新重复并发；目录 200 更新连接观测 | P01、P08 |
| N03 名称代替身份 | 同名跨端点候选合并、指派／探针命中另一个 model_id，或凭目录失败改当前指派 | P01、P06 |
| N04 偷偷提交设置 | 选择线路、改价格、折叠详情、刷新目录或点击探针时自动保存；半行成功被称作整行成功 | P02 |
| N05 最后一维清空假成功 | 清空已有最后一个覆盖维后接口回执成功但旧覆盖仍在；把初始 None 当清空回归 | P03 |
| N06 显示名清空假成功 | 清空已有显示名后旧名仍被保留；仅输入框变空即显示已保存 | P03 |
| N07 空价冒充零价 | 空输入保存为 0、四维被强制补齐、价格单位未注明或 exact 同时显示估算分项 | P03、P14 |
| N08 冲突被刷新吞掉 | stale_revision 后自动换版本重试、external_change 后靠重读 .env 宣称解决，或外部文件被覆盖 | P04 |
| N09 回执清新草稿 | 提交后新编辑、另一模型／字段的输入被旧成功回执或整页 render 清掉 | P05 |
| N10 受理等于成功 | 202 被显示为探针已连接／支持／完成；503 被统一断言为没有任何持久记录 | P06、P07 |
| N11 失联自动重试 | 响应丢失、focus 或重连重发探针；根据模型名／时间猜 run_id；查看详情新建 Session | P07 |
| N12 不真实的认证能力 | 没有 auth_probe 声明时虚构免费验证，catalog_url=null 被直接称为无目录能力 | P08 |
| N13 重读等于全体就绪 | .env 重读成功掩盖配置未一致生效、MCP restart_required 或清理未完成 | P08、P10 |
| N14 旧观测覆盖本次失败 | Tavily 400／422 或冷却回执被写成刚刚测试成功；探测与搜索限流相互覆盖 | P09 |
| N15 MCP 维护偷换操作 | 重试原操作换新 ID，completed 被写成全部服务器成功，可用数被写成已授权数 | P10 |
| N16 旧 Connections 快照复活 | 旧实例／revision／迟到读取覆盖新状态，操作回执清掉尚存的未生效错误 | P11 |
| N17 账目范围错归因 | 工具筛选只剩该工具却仍显示整 Run 金额为其成本；表单新范围被贴到未刷新的旧快照 | P12 |
| N18 缺口变成零 | 无 telemetry 被说成零调用，损坏时间被补造纳入，未报告 Token 被补零，跨服务 credits 相加 | P12、P14 |
| N19 固定快照混账 | 翻页重定价、旧分页拼到新快照、detail 全局 summary 被标成单 Run 费用或推定 recording_state | P13、P14 |
| N20 正文与财务同寿命 | trace 裁剪把持久费用归零，财务快照仍有效就继续读已不可用正文，过期财务阻止本来独立的当前核验 | P15、P16 |
| N21 历史与当前核验混写 | 后续核验改写当次结果，模型投影被称为已送达，aborted Attempt 参数补入 committed 调用 | P15 |
| N22 过期自动换账 | 快照失效后悄悄刷新，跨午夜返回把原半开期间改成新的近七日，旧正文响应写入另一 Run | P16 |
| N23 示例吞草稿或执行 | 更换示例直接清用户 SQL、折叠销毁输入、写入编辑器动作自动执行或打开主对话触发清理离页 | P17 |
| N24 草稿改写旧查询 | A 在途时输入 B 使请求改为 B，或 A 结果被标成由 B 产生，失效后旧提交 SQL 从关联副本复活 | P18 |
| N25 复制范围不实 | 复制当前页却声称全部行，缺少截断说明，行段／返回总数混淆，复制动作暗中再次查询 | P19 |
| N26 取消假完成 | 点击取消即显示已停止，取消前已完成被改成失败，完成但正文未收到被显示为 0 行 | P20 |
| N27 旧句柄影响新查询 | A 的迟到签发仍执行、A 的取消／状态回执覆盖 B，单次点击或同句柄启动两次查询 | P20 |
| N28 清理责任消失 | 执行失败掩盖 cleanup=failed，隐藏 UI 就解除 worker／发送缓冲责任，清理未释放接受新查询 | P21 |
| N29 准备／结果边界被削弱 | WHERE／LIMIT 缩小保护准备、输入超限返回部分成功、NULL／空文本／BLOB 类型混淆、句柄期限冒充结果 TTL | P18、P21 |
| N30 受保护旧内容复活 | 断连／遗忘清结果后保留可复制副本、旧 HTTP 成功复活内容、重连／返回自动重跑；隐藏标签却暂停保护失效 | P22 |
| N31 密度牺牲状态 | 320px 或文字放大时错误／取消／保存不可达，横向整页溢出，支持与价格未知靠颜色／悬停表示 | P23 |
| N32 未完成验收被写 PASS | 只做文档／截图就称真实业务通过；历史 BFCache BLOCKED 被本次静态调查升级为 PASS | 全部 |

## 可复用入口与验证范围

文件名与位置用于接手导航，不固定测试数量，也不要求为了进入评审重新运行仍适用的确定性证据。

| 主题 | 现有入口 |
| --- | --- |
| 浏览器模型／连接 | `tests/browser/settings.spec.js`、`tests/browser/integrations.spec.js`、`tests/browser/mcp.spec.js` |
| 模型状态、持久设置与探针 | `src/agent_alfred/evals/deterministic/test_models_page.py`、`test_settings_commands.py`、`test_model_settings_store.py`、`test_inference_probe_target.py`、`test_runtime.py` |
| 账目与历史 | `tests/browser/accounting.spec.js`、`src/agent_alfred/evals/deterministic/test_ops_snapshots.py`；账目、工具历史／核验及价格用例按影响复用 |
| Database 用户操作 | `tests/browser/database.spec.js`、`database_cancel.spec.js`、`database_status.spec.js`、`database_lifecycle.spec.js` |
| Database 真实浏览器生命周期 | `tests/browser/database_native_lifecycle.mjs`；原生标签隐藏、reload／close、实际返回与 BFCache 分别记录能力和结果 |
| Database 服务与保护 | `src/agent_alfred/evals/deterministic/test_database_http.py`、`test_database_console_sql.py`、`test_database_console_objects.py`、`test_database_boundaries.py`、`test_database_lifecycle.py`、`test_database_review_repair.py` |

实现阶段按 [Dashboard 验证说明](../../dashboard.md) 和实际修改范围执行项目必要门禁；新增行为覆盖应复用真实页面与服务接缝。模型清空问题至少覆盖从已有持久值经公共设置路径清空、重读／重载仍为空以及对应价格回退；新旧消息身份、免费／付费区别不能只靠 DOM 文案测试。

当前文档中的历史 BFCache 限制见 [#66 实现说明](../../implementation/issue-66-database-console.md)：真正缓存恢复分支的历史记录为 BLOCKED，不能因补充事件测试或实际后退重载而改写。后续若环境变化，可新增明确身份的验证，保留原结果；本票不改变 no-store 合同。

## 本轮实际执行情况

本轮只有文档、源码、现有测试入口的静态核验及文档一致性检查，结果见 [VALIDATION.md](VALIDATION.md)。P01–P23、N01–N32 的产品执行、浏览器行为和真实外部调用均 **NOT RUN**；本文没有把未来验收要求登记为当前 PASS。
