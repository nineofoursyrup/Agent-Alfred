# Spec 增量复核：PASS（证据采用与提交求 CI 阶段）

绑定 proposal SHA256 `b6ccba8ac14c7ecbb46caa3332ce3d944ae7b0afd864c4b6890647b28b392070`，产品 tree `d5ae23a4c989b3624b853a5493f61af38a11c7aa`、src `08582ef657e69669a3333606021918f9b0509e6f`、七文件 manifest `274775393b6f209350e45f2920b8abb5daa814f31160246421fbe84d1c822984`。1181 个产品候选树 blob/mode 与工作树一致，七文件及支持后继字节核对无漂移。仅审新证据、状态迁移及旧证据适用性，不重审整个 PR。

**发现处置：**[r1 原观察](spec-r1.md) 两项均关闭；当前阻塞 finding 0、产品代码 finding 0。

- **SPEC-EVID-01 / P2 → 已解决**：`issue-87/SPEC.md:124,226` 要求重复提交不重投并保留202前新草稿。`native-repeat-r2/events-sequence-order.json` seq8/10/12记录首发与两次重复Enter；它们均在202返回前（重复键发生在服务端写入202屏障记录之前）。`dispatches.jsonl`只有一笔POST，pending DOM显示Send禁用且新草稿/中央焦点保持；释放屏障后 `final-state.json` 只有一笔completed Run、两条消息，final DOM保持新草稿和IANA焦点。disabled Send点击是操作记录，禁用控件不产生click事件。此为受控准入补证，输入文本为CUA粘贴，非新增原生IME主张。首试夹具AttributeError FAIL和后继初始GET BrokenPipe均保留；后继正常关闭0，不宣称日志干净。
- **SPEC-EVID-02 / P2 → 已解决**：按 `issue-92/ACCEPTANCE.md:74`、`VALIDATION.md:102`，AC25已从INCOMPLETE改为BLOCKED；整体completion仍false。

**覆盖核验：**原7 BLOCKED source的25 owner片段精确对应4＋10＋9＋2，均追加旧片段历史后迁移；649原PASS片段完全未改。原45失败、旧CI、原生IME/BFCache BLOCKED和真机排除保留。聚合为309 sources/674 fragments PASS、25 AC PASS＋AC25 BLOCKED、12 MCE/8 G PASS。

AC03：独立读原生事件、SQLite、截图与只读观察脚本，确认composition Enter与换行后零Run、中文正文后唯一已保存Run、回复后新草稿/中央焦点保持。保留“用户拼音简体/TIS豆包”及compositionend不trusted，不主张Apple专属覆盖。200%继承原4da观察和已审核065适用性；本轮无相关几何/CSS/键处理变更。

AC24/G08：新installed old22c→current→supported链三次正常close、同状态两代消息/草稿/清空值/记忆/遗忘/账目及刷新零POST成立；support实际tree为641dba43，manifest顶层745df为其基线。产品仅index/shell的Inbox默认入口与current不同。

AC25：四安装各620文件、34资源、100 HTTP及产物hash均与当前字节对应。G01/02/06受影响路径以114项后继适用结果补足；G03/04/05/07原真实组合及MCE按未变依赖复用；G08使用新链。未重跑确定性绿灯。

可在既有授权下提交并推送Draft获取当前候选CI；**本报告不批准提前merge/close**。新PR/push CI、实际merge SHA CI和逐票验收/关闭回读仍是后续门槛。未修改产品、台账或远端。
