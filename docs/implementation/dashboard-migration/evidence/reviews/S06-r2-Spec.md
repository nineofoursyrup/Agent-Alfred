# S06 r2 独立 Spec 评审

**PASS（限定为 S06 首次完整 Spec 评审）；硬发现 0，最高严重级别：无。** 未发现本片缺失／部分实现的必需行为、未获要求的扩展或看似实现却错误的行为。此结论不把 source、AC 或 G 整体提升为 PASS，也不代替 Standards 轴。

- base：`6c152d62c2aec40a14d8164bad9e902141e31724`
- head：`48cf21162bd59d066669fe9a82cc795e1b3d3eb4`
- tree：`f495edbe74a00484c177953417c5def8bd297ecf`
- worktree clean；审阅完整 **13 文件**差异，非仅 r2 修复增量。**36 source／21 AC** 集合、原 identity/hash/owners 与固定映射一致；原文已沿 source-map 阅读，包含 broadsection 的 S06 责任。

分流保存保留真实 CAS／指纹恢复与后继选择；只读核对不暗换编辑基线。聚合冻结原 Session／参数，以提交快照识别新编辑，未知准入不重投，读取／草稿／记录分轴。源码及适用真实用例支持未选来源零隐式读取、发送前许可复核、NoAction／非法候选／记录失败／重启 interrupted 的区别。拓扑保留标旧图而统计撤旧值，比例条与数表共用经整体校验的版本快照。

独立机器回读确认 r1 **212**／r2 **78** 份证据 hash 不变；S03 `aggregationFacts` cleanup、Run／Inbox 及 S08 实现均与 accepted base 一致，pages 导出及 assets 为实际所需并集。重新读取了两个包内 **31** 资源字节并核对源码；现有源码 HTTP 证据仍是 11 入口／30 实际加载资源。

复用当前 **27** 项受影响组合及类型／Ruff／diff 检查；继承 **176** 后端、**60／22／19** 有界浏览器结果，以及 Run-path **68/70＋两个 successor PASS**，保留原首次失败。无具体疑点要求新探针，未重跑产品广测；判断依据及适用性见 [details](S06-r2-Spec-details.md)、[machine verification](S06-r2-Spec-machineverification.json)。

完整 source／AC／G、S11 最终 CI／全量、四安装 HTTP、upgrade／rollback、native 200%／真实中文 IME 仍 **NOT RUN**；历史 BFCache **BLOCKED** 保留。iPhone／iOS／真实移动键盘为 `excluded_by_user / NOT RUN`。
