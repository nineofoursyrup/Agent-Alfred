# S08 r4 独立 Spec 绑定复核

**PASS；Spec findings：0。** 以已独立 PASS 的 r3 为起点，本结论只绑定 r4。

- Head：`51159d3854db388a6c178f8fea0b560fcda0bd13`
- Tree：`a7779e06b2592939767764828fd7c80b5d13a904`
- Base：`87aab611e4a040ad0edc9956a43f6dff5d4944f3`
- r3：`2951bf6892d9c5a7b43dedcb412ee54c938b2977`。

真实差异仅 `tests/browser/shell-startup.spec.js` **+30/-0**，为已接受 S02 r8 的原测试；合并 parent、完整 21 文件 manifest/diff 均匹配。生产代码、资源、依赖、配置及冻结合同全部不变，r3 三处 Models 默认折叠定位适配逐字节保留。没有发现缺失、越界或错误实现。

新增测试对应 #87 `SPEC.md:101` 的断点输入／焦点保留责任：暂停真实 native SSE，显式创建并编辑 MainBar，释放首快照后执行真实 `1100→1099→1100` 视口切换，断言输入、焦点、`[2,5]` 选区及宽屏偏好。没有伪造 Host snapshot 或用额外 focus 修补结果。

已读实际 startup 日志：**5 PASS**，包含新增用例与原 entry/SSE/history 首挂门、同 revision 真实重启重连；原 0 early GET/POST、唯一 page/Stream 及同步后合法断连读取断言保留。实际重连为同实例两份 revision 0 snapshot、单页、零 POST，fixture 退出 `[0,0]`；实施者清理记录保留端口释放结果。本评审未启动服务，也不重复 25/10/HTTP 检查。

独立 metadata 校验 **PASS／errors=[]**：8 份 r3 证据和 8 份源文档绑定、完整 55 source／19 AC 原图、24 资源／52 边原证据及 128 项归档均匹配；未重建覆盖表。`c34d8462ae6cdf3eb332784972db9521ebf8c070` 仍为祖先，清空修复 production blob 不变，r3 Models 修复及原失败证据继续适用。

S03 尚未集成；未来 S03/S11 组合、整体 source/AC/G、最终 CI／安装、native 200%／真实 IME 仍 NOT RUN，历史 BFCache BLOCKED 不变。未读当前 Standards 报告，未改产品、Git、ledger 或 tracker，无清理责任。

[独立身份／验证结果](S08-r4-Spec-verification.json) · [校验脚本](S08-r4-Spec-verify.py)

Spec：0 findings；无新增阻断。
