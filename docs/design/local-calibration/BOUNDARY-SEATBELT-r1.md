# 已确认的 custom Seatbelt 边界补充

2026-09-28，机主对具体替换提案明确选择：**“接受 A：custom Seatbelt，继续最小实现与完整验收（推荐）”**。提案与聊天选择记录在本机输出目录 `local-calibration-continuation-20260928-r3/reports/OS-CONTRACT-DECISION.md` 和 `local-calibration-seatbelt-implementation-20260928-r1/operations/OWNER-BOUNDARY-CHOICE.json`。本记录承接合同选择，不是本人认证回执、金额事实、run grant 或质量批准。

原 #105 AC1 的 App Sandbox 实际拒绝 TCP/文件后仍能经系统浏览器代发；首次失败保持 FAIL。叠加外层策略的两种原型在 main 前退出，也保留原件。独立 custom Seatbelt 原型已能运行有限 Python/SQLite 工作负载，并阻止有正向对照的浏览器 canary；它不构成完整 #105 PASS。

本补充只替换下列已确认要求；原条款快照不改标通过：

| 条款 | 当前要求 |
| --- | --- |
| #105 AC1 | 在固定 macOS build 上，确认 custom Seatbelt deny-default 策略先于不可信 runner 生效，子进程持续继承。明确接受 `/usr/bin/sandbox-exec` 已弃用、第三方 SBPL 没有公开支持的风险，不承诺 OS 升级兼容。加载失败必须停止，禁止无沙箱回退。机主、macOS 和可信 host 继续在信任根内。 |
| #105 AC3、#106 AC5 | 候选绑定完整策略、加载器字节、OS build/架构、固定原生入口、解释器/库/依赖及实际 IPC/环境。每案只读代码与独立可写状态分别限定。策略、系统或加载字节变化导致旧证据/许可不能继续准入；按影响重新审查、实测和批准。 |
| #105 AC4/AC5 | 原强度不变：凭据/机主资料/金标/他案/账/批准受保护；直接出口、继承能力与通用系统代发不能形成旁路。只有可复查的必要许可，不接受系统服务黑名单或仅 direct socket 拒绝。 |

原 App Sandbox 安装与源候选继续作为历史保存，不迁移旧一次性容器、不清除 spent marker、不复用旧 grant。新 `V1-LOCAL-MACOS-BUNDLE` v3 才表达此边界；原 v2 不得改名为 v3 PASS。

实现位置补充：实际验证发现从进程出生起限制全部 special-port getter 会阻止可信系统初始化，嵌套 `sandbox_init` 也失败。当前最小实现采用固定 C 初始化、清继承入口、在任何 Python/agent 输入前一次性进入完整策略，再读回受限状态；native loader 字节随包绑定，boundary v2 区分早期 `sandbox-exec` 原型。固定 C/macOS 已在可信基内，AC4/AC5 强度不变；新 exec 禁止，fork 继承最终策略。该实现位置调整不充当实际隔离 PASS。

USD25 严格最终模型责任、原 10800 秒、576/480/96、既定模型/端点/参数、synthetic 守卫、首次失败、未知负债、独立盲复核与精确诊断检查点均不变。真实 #105/#106、本人 run grant 和付费运行仍各自需要事实与事件；通用阈值、正式120案及发布没有因此获批。
