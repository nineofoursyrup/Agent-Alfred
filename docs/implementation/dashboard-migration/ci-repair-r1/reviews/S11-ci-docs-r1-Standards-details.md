# S11 CI 文档 r1 Standards 细节

增量只读审查代码065→文档865→c96。精确 tree3d5f0997f26921056db04e224a6e7fa9ad417f8d，src0eaae1fbd39ec02656f4a61982ec73a06c46fe2f 与上一轮代码PASS一致；27个新增文件均在 ci-repair-r1 范围，原产品/测试/frozen设计/验收proposal字节未变。原代码review及相关确定性证据直接复用，没有产品测试或仓库写入。

## 唯一 P2：未绑定候选的整体状态

ci-repair-r1.md:3 以“整体仍 BLOCKED”概括整项；ci-repair-r1.json:5 同时给出未限定的 overall_result BLOCKED。下文确实保留原PR522/5、push525/2 FAIL，也明确修复代码065双轴PASS和最终门禁尚未完成，但顶层标量仍会被读取为当前整项已由FAIL变成BLOCKED。

固定 docs/design/dashboard-implementation/VALIDATION.md:102 明文要求“必需范围中已有 FAIL 不能被其他 BLOCKED／环境缺口掩盖”，且:100要求结果绑定真实候选。最小修复是明确：原37cd的CI/组合FAIL保留；065仅代码审查PASS，最终门禁pending；真实IME子责任BLOCKED；整项未完成。不要把这种限定改成新的最终产品结论或重写原proposal/canonical。此finding针对候选/状态混用，不因已明确标注的 pending 本身判FAIL。

## 已核验的资料

150项只读核验全部通过，详同名JSON。24个评审/原生附件既符合manifest SHA，又与原E文件逐字节一致；本次没有读取同轮文档Spec结论。29个recorded-outputs条目 UTF-8还原后 SHA正确且字节等于原文件，包括带BOM的两个CI日志。原PR五项、push两项、合计六个不同用例以及限定修复日志可复核；16 PASS/typecheck/29 Python仅属于065。4da的10场景×5=50、四安装各100条HTTP检查正确绑定历史头；b214和4da的full browser均保留exit130和中止原因，不冒充完整通过。

865 raw unified patch中的两个单空格context行触发git diff --check exit2，原记录与固定Git重算完全一致。c96将该patch精确文本纳入JSON，UTF-8还原等于865原patch、原native附件以及4da→065的Git差异，SHA768b2b936e0831bbb0b2952d4e8c1378683f3641edacd64d23c00aa615ad095e不变。没有新增.gitattributes豁免；最终固定diffcheck通过。

11项source mapping与原309提案的source ID、文档/起止行/hash、owners、AC完全一致，D09仅关联及D04/D08组合影响、R07保留IME责任的限定清楚。原proposalSHA90bbc73416b33ac8428fb3f7122bea7c21c81e2eb8002b60cb5599773d174544与原字节一致；309身份唯一、26AC/12MCE/8G原清单不变。入口全部相对Markdown链接解析存在。

native200是4da的实际观察及065的有界适用性建议，明确不声称重放未测试startup race，要求最终安装HTTP字节、全browser和CI；原IME仍BLOCKED。支持回退须携带三笔共享语义修复并保留Inbox默认入口，989662及old22c职责区分正确；新完整升级/回退G08待执行。其余标准/完整smell基线未见新硬缺陷或有价值的可选建议。
