受影响 Standards 独立复审：PASS。文档标准违反 0，实际缺陷 0，可选 smell 0。

固定候选 `13aa84fe2c6a273e3c8d2857983159271ac10c4f`，相对已审 `fca0b7194791d264adfa73897746d750176e166f` 仅一测试文件 5+/1-；实现 worktree 干净，两者 `src` 树对象相同。时间边界：2026-10-10 16:34 UTC（北京时间 2026-10-11 00:34）；下述检查不预判完整后继浏览器结果。

- `tests/browser/accounting-migration.spec.js:297–304`：0 项。新断言将恢复前的可视锚点与恢复后的内容坐标转换为理想滚动目标，再要求实际 `scrollTop` 精确等于其最近整数。锚点错位超过 0.5 CSS px 仍失败；并未改成只判断可见或放宽到多像素。焦点、完整来源身份、读取次数、零 POST，以及 focus／scroll 更新意图分支均未改。未发现命名、重复逻辑或其他基线 smell；符合 SPEC 第 5 节保留焦点及局部位置的合同。
- `first-full-browser-failure/`、manifest、`04-browser.log`：0 项。11 个原始文件的大小和 SHA 全部匹配，完整日志 SHA 也匹配；保留 fca0b719 的 541 passed／2 failed 和失败 `.last-run.json`。1280／390px 原始观测分别为理想 1558.5／1936.5、实际 1559／1937，焦点和可见性均为 true，确为半像素边界，没有覆盖首失败。
- `target-checks/source-return/`：0 项。13 项原流／结果的尺寸与 SHA 全部匹配 MANIFEST；固定测试 blob `7ec1deec1e62d5956bc6583db41b7bf1e7410bf1` 与新提交相同。日志为 2 passed（7.1s），两视口观察均含 none／focus／scroll；回程 JSON 均为零 POST。提交前测试与提交后文件未变的边界明确记录。
- `.gitattributes`：0 项。新增四例外只精确匹配两个冻结错误上下文、`04-browser.log` 与 `04-typecheck.log`，符合现有冻结日志规则；没有通配符，先前三例外保留，产品、测试和撰写文档未豁免。

本次没有运行测试／服务器或读取 Spec reviewer。source-return 归档在检查时尚未暂存；完整后继 05 浏览器仍待协调者完成，定向复测不替代全量 PASS。Standards 总计 0 项，无最高严重项。

2026-10-10 16:35:52 UTC（北京时间 2026-10-11 00:35:52）补充核对：source-return 的 13 项原始文件现已暂存，尺寸／SHA 全部匹配原 MANIFEST，固定候选与测试 blob 不变。`source-return/typecheck.log`、`gates/05-typecheck.log` 和既有 `gates/04-typecheck.log` 均为 29 bytes、SHA-256 `8fa1cf5506304e8abac55868e7f1a136c9b1dde57a3981a382da4c21ea129a6f`，保留相同 npm EOF 空行；前两条新增 `-whitespace` 为精确已完成原流路径，符合现有冻结日志规则。该补充标准违反／实际缺陷／可选 smell 均为 0。协调者已记录 cached diff-check PASS，本次复用而未重跑；`05-browser.log` 没有豁免，完整后继仍 RUNNING，不计为 PASS。
