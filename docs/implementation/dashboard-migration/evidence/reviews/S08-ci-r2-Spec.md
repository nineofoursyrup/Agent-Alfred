# S08 ci-r2 独立 Spec 绑定增量

**PASS；Spec findings：0。** 本结论只覆盖 accepted S05／listener 同步与证据绑定，继承本人 ci-r1 的独立完整因果评审。

- Head：`abb3817cee1c6491094f589c2c25c5257d5a1ac8`
- Tree：`cbe04daa248ffe4020316544f9c7ee5630fadafd`
- Accepted base：`da7d6b84899b19d690d574c74397a45b0239078b`
- Previous：`e6a91d0d8c5eae5d75829e543a79140f9e060935`

真实 merge parents 正是 previous 与 accepted base。8 个同步文件逐字节等于 accepted base；整个 `src` tree 同为 `22706c002421b5393ae4a830b989afa53e68b33b`。相对 accepted 仍只有 `mcp.spec.js` **+4/-0**，测试 blob 与 ci-r1 完全相同，S08 Models／Connections／focus／settings 及 fixtures／依赖配置未变。`c34d846` 祖先及清空修复 blob 保留。未发现新增缺失、错误实现或范围扩张。

实际 listener 变化为已接受的 `request_queue_size = socket.SOMAXCONN`；一次原 lost-receipt target **1 PASS，retries=0**，包含同 operation／相等 payload、一次新增 initialize、四视口滚动／焦点／详情／无横溢出。本次未重跑 ci-r1 的真实 GET 屏障或 repeat5。新增 Memory CSS 使用 Memory 专属选择器；S08 状态等待及渲染归属结论继续适用，仍满足 `VALIDATION.md:7` 的“等待权威事件／可见状态，不靠固定 sleep 猜竞争”及 `issue-91/DESIGN.md:113` 的同操作身份要求。

独立从固定 Git 对象的 HTML／JS／CSS 引用重建 **33 资源／77 边**，节点与边完整匹配，再与 ASSETS 对照；33 份真实 HTTP 的 200／MIME／SHA-256 全等候选，`model_calls=[]`、fixture exit 0。没有以 registry 自身代替引用发现，也未把资源存在视作 S07／S09 页面验收。

机器绑定 **PASS，errors=[]**：67 项本轮证据、23 项归档、63 项原 ci-r1、原 PR／push 28／27 项均匹配；9 份当前双镜像一致。55 source／19 AC／5 integration chains 与历史完整记录相等，冻结 source 元数据保持，历史 fragment 身份未重盖。本轮五个证据工具／即时 bind 失败保留；后续十端口 bind／connect／进程审计支持已清理。

原两次远端 CI 仍 **FAIL**；whole source／AC／G、最终 CI／安装／native 责任未升级。后续 S07 接受时须合并 details 展开与 nested cleanup，同时保留本四行等待。未读当前 root Standards，未改产品、Git、ledger、GitHub，未启动服务。

[独立核验结果](S08-ci-r2-Spec-verification.json) · [核验脚本](S08-ci-r2-Spec-verify.py) · [继承的 ci-r1 Spec](../S08/ci-r2/ci-r1-candidate/reviews/S08-ci-r1-Spec.md)

Spec：0 findings；无新增阻断。
