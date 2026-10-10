# V01 滚动断言修复的受影响 Spec 复审

固定候选：`13aa84fe2c6a273e3c8d2857983159271ac10c4f`；比较对象：已审 `fca0b7194791d264adfa73897746d750176e166f`。仅 `tests/browser/accounting-migration.spec.js` 5+/1−；impl 工作树干净，两个候选的 `src` tree 相同。未读 Standards reviewer，未启动服务器／执行测试。

结论：**0 findings**。符合 #123「改为按新权威断言，不降低其他断言」的范围。

- `accounting-migration.spec.js:297–302` 保留来源焦点、可见性，只把半像素锚点断言换成精确最近整数滚动目标。设实际滚动值为 S、返回后锚点偏移为 A、返回前偏移为 B，目标为 `round((S+A)-B)`；`S+A` 是由 DOM 实测的返回后内容坐标。实际滚动错移 1px 时 A 相应改变，目标不会随错误一起移动，严格相等仍失败。该式不是恒真，也未引入任意误差容限。原失败目标 1558.5／1936.5 对应实际 1559／1937，符合最近整数取值。
- 身份、快照／筛选、触发链接、焦点、可见性、用户更新焦点／滚动意图、每次返回一次读取和零 POST 断言原样保留；业务与壳层交互没有改变。
- 首次完整浏览器批次仍绑定旧候选：**541 passed / 2 failed，exit 1**。暂存的 11 个原始结果文件及 `04-browser.log` SHA 全部匹配 manifest，两视口原始 `none` 观察均 focused／visible 为 true；首次失败未被覆盖。
- 新候选定向复测归档绑定测试 blob `7ec1deec1e62d5956bc6583db41b7bf1e7410bf1`；13 文件 SHA／尺寸全部匹配。原流 **2 passed（7.1s）**，两视口均有 none／focus／scroll 三意图、零 POST 与三次读取记录。归档说明明确提交前验证后未改所测文件。本次读取时 source-return 已归档、尚未暂存。
- 四个新增冻结规则分别精确命中两个原始 `error-context.md`、`04-browser.log`、`04-typecheck.log`，先前三条保留；05 日志、产品、测试和 V01 文档没有新增空白豁免。

时间边界：报告时后继 **05 完整浏览器门禁仍待协调者完成**；定向复测不等于完整门禁或 #123 全部验收 PASS。

补充记录核对（2026-10-10 16:37 UTC）：source-return 的 13 个原始文件现已暂存；后续仅为 `target-checks/source-return/typecheck.log` 与 `gates/05-typecheck.log` 追加两个精确 `-whitespace` 例外，原文「05 日志没有新增空白豁免」对应此前评审时点。三份 typecheck 原流（含既有 04）均为 29 bytes、保留 npm 终端空行，SHA256 均为 `8fa1cf5506304e8abac55868e7f1a136c9b1dde57a3981a382da4c21ea129a6f`。`05-browser.log`、产品、测试和 V01 文档仍无空白豁免；复用协调者已完成的 cached diff-check PASS。当前 05 结果只记录 typecheck exit 0，尚无完整浏览器结果，仍待协调者完成。**0 新增 findings**；未重跑测试或修改仓库。
