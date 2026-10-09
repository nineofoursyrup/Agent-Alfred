# Spec 增量复核 r1（保留观察）

提案 SHA256：`669f8025f88cd4ffbaee8eac59997b80abe664ccc46f44114ff6feccd505d5ea`。产品 tree `d5ae23a4c989b3624b853a5493f61af38a11c7aa`，7文件 manifest `274775393b6f209350e45f2920b8abb5daa814f31160246421fbe84d1c822984`；1181个 tree blob/mode 与当前工作树逐项一致。仅复核新增证据和适用性，不重审完整PR、不修改产品/台账/远端。

- **SPEC-EVID-01 / P2 / 缺证**：`issue-87/SPEC.md:226` 的 CE-14 明确包括重复 Enter／点击发送。`APPLICABILITY.md` 的“CE-14 的202前编辑新草稿和连续提交准入时序”引用 `shell.spec.js:154`；该测试验证延迟202期间新草稿和中央焦点，但只按一次Enter。新增原生记录也只有seq52的一次普通Enter。单次POST计数不能独自证明重复操作下无重投。关闭方式：定位既有具体重复操作证据，或在相同产品字节上补受控202未返回时重复Enter/点击、确认仅一次请求/Run并保留新草稿的观察；无需修改产品或重跑全套。
- **SPEC-EVID-02 / P2 / 记录格式**：`issue-92/ACCEPTANCE.md:74`、`dashboard-implementation/VALIDATION.md:102` 限定单项证据结果 PASS／FAIL／BLOCKED／NOT RUN；提案 `AC25.result=INCOMPLETE` 超出枚举。关闭方式：AC25使用BLOCKED并保留等待当前CI/独立采用的理由；overall摘要仍可表示未完成，completion=false。

其余已核实：7source/25owner变更与旧BLOCKED精确对应，649原PASS片段未改；45原失败逐项原样保留并追加4条；BFCache和真机排除未改。native原始trusted start/update/input、确认Enter零Run、换行零Run、中文正文、唯一持久Run/两消息、草稿与焦点成立；系统输入源名称差异及不trusted compositionend被正确保留。四安装各620包文件、34资源、100 HTTP核对当前字节一致；新G08三次正常关闭与同状态消息/设置清空/遗忘/账目成立。未发现新增产品代码finding。

此为r1真实观察；后继补证须绑定新proposal/hash重新给结论，不覆盖本文件。
