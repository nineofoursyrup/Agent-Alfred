# S11 semantic Standards 判断明细

## 范围与证据适用

仅使用固定 Git 对象读取：accepted base→c839736 的 Models／Memory 两生产文件和两个 browser 文件，以及 0758e46 相对其父的一个测试输入行。修复直接继承 accepted S01–S10；test successor 的两生产 blob 与 c839736 相同，Memory 测试相同，Models 测试仅增加一次显式后继输入。未以可变 S11 工作树作审查对象。

只读核验的 41 项均通过：提交／父／tree、四文件集合、successor 单文件单行、diff --check、9 个相关 fixture／focus／工具依赖文件未变、12 个规范／领域文件未变、4 份封存 log hash 和原失败保留、successor 结果、STD02 原失败 trace。精确摘要及可复核脚本见 `S11-semantic-Standards-verification.json`、`S11-semantic-Standards-verify.py`。这些是元数据审计，不冒充新产品执行。

执行绑定依照封存 handoff，日志核对到实际测试名称／输出。绿色执行含另行排除的默认入口提交；本组用例明确进入 `/models`、`/memory`，使用已经存在的 `/overview`／`/connections`，未用默认入口动作。修复本身只改变字段输入归属及判定，没有对入口新增接口的依赖。移植到支持目标后的整包运行仍待协调器 G08 证明。

## 文档规则与实现判断

1. **Models 的三种值各有责任。** #91 DESIGN R03.4 要求原请求固定对象、字段、值和预期 revision，回执不能清提交后输入；R03.5 要求保留冲突基线，显式采用前不重试。`models.js:32–34` 的 `unsubmitted` 在 pending／unknown 使用冻结 submitted，其他状态使用 baseline。`mutate:149–196` 只在原请求拥有当前 generation／attempt 时处理回执；发送仍使用既有 expected_revision。普通读取的 `accept` 继续跳过 pending／conflict／unknown；不能凭同值把未知写入当成功。

2. **状态退休不放宽 CAS。** 发送时冻结 submitted；明确非 2xx 拒绝清空；可核验成功读取匹配模型的保存值后清空；无法核验／网络错误保留；显式 adoption 在既有 `canAdopt` 条件通过后清空。没有变动服务端权限、协议或持久化。ADR-0022 的 expected_revision／外改冲突判据不受影响。实例变化与断连仍退休旧 generation，旧回调无法清除 successor。价格、线路、显示名复用同一判定，未增加按字段复制的脆弱规则。

3. **Memory 将原请求作为 pending／unknown 输入边界。** #90 R02 要求区分已提交动作与后继输入，R04 保留 expected_version 和明确冲突处理。`memory.js:1394–1399` 去掉以 `editDirty` 为前置门的错误条件；存在 editAttempt 时直接比较冻结 request，故提交 B 后回到旧 A 不再因 A 等于原始值而丢守卫。没有 editAttempt 时仍使用普通 editDirty。`submitEdit:1177–1238` 的明确回执退休、已保存提交值／record_version 更新、editing owner 判据均未改。`verify` 的确认 404 清请求正文、离线／失败隐藏以及 dispose 清理继续覆盖受保护草稿。

4. **唯一新增测试语义与真实 oracle。** Models pending 和 unknown 两个场景通过 `route.fetch()` 先到真实 HTTP／持久 settings，再延迟或丢掉浏览器回执；捕获实际 `[display_name, expected_revision]` 恰好一次 `[B,1]`，GET 检查保存 B，模型调用为零。取消离页保留 A；回到已提交 B 允许离页，无重送。现有实际冲突、失败／过早比较、显式 adoption、实例切换测试继续覆盖 revision 0／1／2 与旧回调隔离。

5. **Memory 场景保留并增强行为 oracle。** 真实保存 A 后提交 B，再输入 A，新增离页取消及默认焦点检查；回执到达后 A 与当前焦点保留，独立详情不被搜索空结果替换。真实 GET 先证 B，下一次明确保存证 version 3 的 A。原其他字串后继输入由同文件既有 create／edit target／leave 行为保留覆盖，无删除旧业务断言。

6. **STD02 successor 是输入修复。** 原失败 trace 在点击 `/connections` 后等待 `放弃并离开`；当时没有后继输入，仅持有原已提交值，按 #91 R01.8／ADR-0046 无需草稿守卫。0758e46 只在保存已到真实服务、回执仍被屏障保持时输入 `new unsaved input before retirement`，从而真实触发被测试的放弃路径。其余刷新焦点、原保存按钮回焦、MainBar 新焦点、离页后 `.env` 控件焦点、三次 POST 和零模型调用断言字节未改；未降低超时或放宽断言。

## 原结果与当前结论

- `submitted-red.log`：3 个实际守卫反例 FAIL，均为找不到默认保留按钮，原 trace／截图仍在。
- `submitted-green.log`：24 PASS、1 STD02 FAIL；三个新反例通过，原失败保留。
- `submitted-green-02.log`：pending、unknown、修正 STD02 共 3 PASS。
- `typecheck-01.log`：PASS；环境由 handoff 记录 Node 22／CPython 3.14.7，原失败 trace HTTP 响应也包含 Python/3.14.7。

24 个已通过用例的相关产品／fixture／依赖未因单行 successor 改变，故复用；新增一行只影响 STD02，已专项通过。未以 24＋3 宣称一次完整 27 项 suite，也未重新执行已有确定性测试。这里没有发现必须启动新产品探针的未解缺陷。

## 完整 smell baseline

所有条目只作为可选启发，不构成硬规则；未提出可操作新增 smell。

| Baseline | 本次判断 |
| --- | --- |
| Mysterious Name | submitted／baseline／unsubmitted 明确区分提交、保存和后继输入。 |
| Duplicated Code | 新公共谓词复用到摘要、字段反馈、取消钉选及离页；Memory 保留其不同 operation request 协议，不强行合并。 |
| Feature Envy | 值的所有权留在原 Models Draft／Memory recordPanel。 |
| Data Clumps | 新 submitted 属于原 Draft，不传播新参数簇。 |
| Primitive Obsession | 此值就是原编辑器字符串；未引入新的跨模块领域类型需求。 |
| Repeated Switches | 既有状态分支未复制新类型分派。 |
| Shotgun Surgery | 行为集中在一谓词及原状态转移；没有额外跨模块协议联动。 |
| Divergent Change | 当前变动同属原页面编辑／回执责任。 |
| Speculative Generality | 无新增 hooks、泛化层或未来用途。 |
| Message Chains | 无新增深层对象导航责任。 |
| Middle Man | unsubmitted 是实质输入归属判断，不是纯转发。 |
| Refused Bequest | 未新增继承或规避继承契约。 |

现有 accepted 基线的其他可选意见不因本报告消失；本报告不重开与窄修复无关的重构。最终入口、安装 HTTP、G01–G08、原生 IME／200% 及全量门禁仍由最终 S11 候选独立处理。
