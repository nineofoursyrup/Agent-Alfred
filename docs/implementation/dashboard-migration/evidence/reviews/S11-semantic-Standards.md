# S11 semantic repair — 独立 Standards review

**PASS：0 项文档硬违规，0 项可证 bug，0 项新增 optional smell。仅覆盖下列窄修复。**

- Base：`8f579a4712075721728b2043d9f626689ff377a8`。
- Repair：`c8397368074dacfc9837599b195e0b0dfc686b7a`；tree `5eb14d976843ce916d5e319e38679ab1ae98703b`。
- 测试 successor：`0758e468c009524d2c14f9545786fe9b93b768bf`；tree `f0046246f133c2f26dab97e615783b87cab42188`。

审查 `base..repair` 四文件及 successor 单行；其父 `593564fc4b6ed08bc332bbfcf76dcf67107929c0` 的入口变更、可变安装 harness 均排除。

Models 将冻结 submitted 与已保存 baseline／CAS 分开：pending／unknown 后改回旧 A 仍受守卫，回到 submitted B 无新增未提交输入；明确拒绝退休 submitted，确认回执或显式采用才更新对应基线。Memory 优先比较原提交请求，普通 dirty、迟到回执、版本及遗忘清理边界保留。符合 #90 R02／R04、#91 R01／R03、ADR-0046／0022。

四份封存日志 hash 全匹配：原 3 FAIL、24 PASS／1 STD02 FAIL 保留；successor 3 PASS，typecheck PASS。原 trace 确认 STD02 等待不应出现的放弃按钮；新增后继输入后，原离页、迟到回执、焦点、三次 POST 与零模型调用断言全部保留。未重跑适用产品测试；41 项只读身份／字节检查通过。

可作为保留语义修复及测试 successor 的独立 Standards 依据；不代表 supported target 已更新、G08 或整体 S11 通过。未读另一当前评审轴。详见 [判断明细](S11-semantic-Standards-details.md) 与 [验证记录](S11-semantic-Standards-verification.json)。
