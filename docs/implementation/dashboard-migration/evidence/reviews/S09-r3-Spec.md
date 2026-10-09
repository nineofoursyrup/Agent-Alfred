# S09 r3 — independent incremental Spec review

**PASS（本次片内增量）；0 个新 finding。`S09-r2-Spec-F1 / P2` 在当前固定候选上已修复。** 原 r2 FAIL、首个 JSON／PNG 及完整报告原样保留。

固定 head `3139d8a1055619af4c71d89e780633effbb9f595`，tree `8c1cad94125d6d5680ef570e40a36643aa9b671f`，base `da7d6b84899b19d690d574c74397a45b0239078b`。原 7 文件／41 source／23 AC 完整独立审查按相同原文与元数据继承；本轮完整检查两文件修复及 S05 同步影响，未读取当前 Standards 结论。

`accounting.js:125–130,145–163` 以原 trigger 的相对视口锚点恢复位置，先核验 snapshot 与 Run 明细，再滚动并聚焦；`scrollTop:0` 防止 Shell 同时重放旧绝对高度。新增数据只有非正文数值位置，符合 I04。pointer/key/focus 与 wheel/touchstart 新意图可取消迟到恢复，卸载清除监听。

最终真实 Host／SQLite／HTTP 证据：1280×800 与390×844 的正常返回均可见且获焦，相对位置误差均0.375px；新 Run-ID 输入保留草稿与焦点，held detail 期间真实 wheel 后240→240不被覆盖。六次往返各1个原 snapshot 的 offset50 GET、5行、0 POST。已查看两张最终正常返回截图。原 RED 两视口失败与 GREEN／最终通过具有同一行为断言。

身份审计 PASS：152份r1、96份r2、74份r3、12份原Spec及34份同步前资料匹配；41 source完整对象／原文SHA、23 AC原文、30个peer和20项同步前后输入匹配。最终34资源HTTP闭包是accepted33＋accounting.css。S05 Memory样式局部限定，listener只增加backlog；原检查按实际适用边界复用，未伪称新全套。

本轮未重跑行为测试。实现方8个不同相关用例已覆盖变化；最终3次重复不增加覆盖数。明细与完整元数据见 `S09-r3-Spec-details.md`、`S09-r3-Spec-machineverification.json`。

全部whole source／AC／product仍 **NOT RUN**。S11完整G链、Models清除／探针、四组隔离安装、最终CI、升级／回滚、native200%／中文IME继续开放；BFCache **BLOCKED**及父任务peer失败不由本评审关闭。无产品、Git、ledger或GitHub修改。
