# S04 r4 independent Spec review

- Result: **PASS — S04 当前片内 Spec**；findings: **0**；worst: **none**。
- Base: `77a51d08917dd37de9c98ae677aa7196b043d4e0`
- Head: `267a17ea73e353c6425e004c47e06fb319f49c83`
- Tree: `0de2736d42da93209172cb3d05b3796088ca1072`

完整审查 r3→r4 三文件增量，并沿用已完成的完整源合同审查。未发现本片新增缺失／部分实现要求、额外范围或已实现但行为错误。

**N1 在 r4 关闭。** `issue-88/DESIGN.md:107` 要求“必要时扩展显示以区分碰撞”。`overview.js:161-182` 独立派生可见 current＋history 身份集合，两类卡共享 peers；当前绘制签名包含完整 peers ID，来源接受后再重算。因此当前 identity／recording 均不变时，新 peers 也能扩展冲突标签。完整 href 与不透明 ID 沿原逻辑保留。

原 F1/F2 保持关闭：`:200-207` 仍在成功读取时固定五条，显示集合不补入第六候选，符合 `INTERFACES.md:82`；来源 section＋anchor＋href、真实重读和新输入退休逻辑未变，符合 `issue-89/DESIGN.md:54`。当前槽继续来自权威 Host 投影，`:243-245` 的同 Run recorded 不降级仍在。

验证：读取原 red、修复 green、受影响 4 PASS 与更强 stable-current-peer-refresh 1 PASS；后者替代四项中的旧 recorded-overlap。最终用例覆盖初次读取失败、当前已 recorded 后新 peers、六链接可区分／完整 href、旧 pending 不降级、idle 释放后五成员不变／零 GET、显式刷新恰一 GET。复用其余 10 项及适用共享／Python／assets 证据；typecheck、ruff 通过。本轮未额外重跑产品测试。

独立核验 39 source 定义／hash／owners、22 AC、9 文件、6 不变文件和 20 资源；tree／manifest／闭包一致、工作树 clean。见 [verification](S04-r4-Spec-verification.json)。

整体 source／AC／G 保持 NOT RUN；S03/S05/S09 最终组合、S11 全量 CI／安装及默认入口、原生 200%／IME 仍待验，历史 BFCache BLOCKED 不变。原 first FAIL 保留；未修改产品、未读取 Standards 报告。
