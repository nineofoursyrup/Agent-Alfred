# S02 r5 — 独立 Standards review

**结论：PASS；0 项硬违规，0 项新增 optional smell。** 复用 [r4 Standards](S02-r4-Standards.md) 未变范围；S01 既有可选 P3 不变。

- Base: `a4168881bd9057a900e8d353002307adff714c42`
- Head: `5ceee180cf9f54f336017fb88998d2b11f91a186`
- Tree: `2b90301c4ea0bea96a1b2ca377163177c86def75`

审阅 r4→r5 全部四文件、78+/4−。产品仅 `app.js:212,215,288` 三处身份判断；其余为隔离 fixture 和回归用例。按完整 Standards／12 项 Fowler baseline 检查适用增量，未发现新问题；未打开另一 reviewer 报告。

## 合同与 fixture

三处判断以 null／string 类型区分缺失和合法空 Session／Run，URLSearchParams、Map 身份及响应严格比较不变；符合 `INTERFACES.md` I00 与 `docs/design/issue-89/DESIGN.md:50`。没有放松 process/action 身份、忙态、提交或保存守卫。

`chat_server.py:77–98` 只新增显式 `legacy-empty-identities` 命令分支；新测试先等待真实 Run 记录与调度完成，再在独立临时数据库准备历史空 ID。旧命令、默认启动、调度屏障和关闭流程不变，未调用新分支的既有确定性证据仍适用。精确定位使用真实 Host／HTTP／SQLite；只有初次普通历史 IO 故意失败。

## 首次失败及验证

保留 `empty-identity-first-fail.log` **2 FAIL**：核对按钮零请求、空 Run 缺口链接缺失。修复后 `r5-mainbar.log` **23 PASS / 1 FAIL** 也保留。剩余失败要求在普通历史被主动 abort 后，仅凭退出精确定位就确认未读；与 I05 独立 cursor／locationTarget、回到最新不证明历史连续性，以及实际末尾可见才确认的规则不符。

测试修正加入同一空 Session 的显式普通历史回读，保留空参数存在、完整正文、单条身份、一次定位、草稿和零 POST 断言；产品未改动该确认保护。后继 **11 browser PASS** 与空／特殊 ID **2 guarded HTTP PASS** 覆盖受影响路径。typecheck／Ruff／固定 diff --check 通过；不累加分轮数量或重跑无影响广测。

[候选核验](S02-r5-candidate-check.json) 确认 r5 交接身份、47 文件范围、18 资源 hash／未变 38 边闭包，以及 164 source／25 AC 原身份。完整 suite、S03 来源返回／长历史和 S11 安装／native zoom／真实 IME 仍待验；真实 BFCache BLOCKED 保留。
