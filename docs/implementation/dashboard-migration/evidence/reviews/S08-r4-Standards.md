结论：**PASS（S08 r4 有界 Standards 绑定复核）**。Documented violations：**0**；optional heuristic smells：**0**。

Head `51159d3854db388a6c178f8fea0b560fcda0bd13`；tree `a7779e06b2592939767764828fd7c80b5d13a904`；base `87aab611e4a040ad0edc9956a43f6dff5d4944f3`。起点为本人已审 r3 `2951bf6892d9c5a7b43dedcb412ee54c938b2977`。

唯一增量是 `tests/browser/shell-startup.spec.js:9–38` 的 30 行测试，与已接受 base 的新增测试逐字相同；其余文件内容与 r3 相同。测试阻塞后释放真实 SSE 请求，经过真实首快照和页面挂载，在 `1100→1099→1100` 布局切换后断言 MainBar 输入、焦点、选区 `[2,5]` 和原 wide 偏好；finally 释放请求。未伪造 snapshot 或 media 事件。原三处 Models 默认折叠定位适配完整保留。

独立核验完整 21 文件 manifest、两个 diff、26 份适用标准、依赖与配置。55 source／19 AC 原映射及原文哈希绑定不变；24 资源／52 runtime edges、注册表与生产字节保持 r3，复用本人已核验的实际 HTTP/MIME/200 证据。`c34d846` 仍是祖先，原独立 clear 提交和生产 blob 保留。归档 128 项均核验字节哈希，21 份固定源码与 r3 Git 一致；其他评审轴文件仅作不透明归档哈希核对，未读取结论。

实际 startup 日志 **5 PASS**：首次 entry/state/history 门、0 early GET/POST、唯一页面/Stream、同步后合法断连读取和真实同 revision 重启重连继续通过；重启退出码 `[0,0]`，端口 `17920–17929` 已释放。完整及增量 diff check PASS。r3 25/10 等未变检查按字节继承，计数不相加；没有重跑广测或 24 HTTP。12 项 Fowler possible-smell 基线无 actionable 新项，工具已强制规则不重复列 finding。所有历史 FAIL 保留。

未来 S03/S05/S06 组合、最终 S11、安装矩阵、全仓 CI、native 200%／真实中文 IME 仍 NOT RUN；实际 BFCache BLOCKED 和原 source／AC／G 总体 NOT RUN 不升级；实际回退构建未执行。

[本轮独立校验](S08-r4-standards-verification.json) · [继承的完整逐项评审](S08-r3-Standards-details.json)。未修改产品、Git、ledger 或 tracker；未读取当前 Spec 结论。
