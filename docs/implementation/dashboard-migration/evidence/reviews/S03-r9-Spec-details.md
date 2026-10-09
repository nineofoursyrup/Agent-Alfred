# S03 r9 Spec review evidence

结论 PASS，0 finding。检查范围为 r8 `342e77b28f967fc7291229805778d679cdb1b85d` → r9 `1379bcf45c32319ada4473bcc1c211cf10516c46`，修复提交 `6269a713363f5e1bfb989d2a1f31fda909ec396f` 与 accepted base `18d2ac94743af0df50c5893862cdd8d8d054a142` 的合并。未读取当前另一评审轴报告；旧报告仅检查保全字节。

## 行为判断

`docs/design/issue-87/SPEC.md:177–183` 要求局部滚动不改变阅读位置、异步结束不夺焦、重要状态与按钮不裁掉、可用高度变化不丢输入和选择。`docs/design/issue-89/DESIGN.md:66–67` 要求新建后的明确入口及原守卫；`SLICES.md:63–69` 保持只读预览与显式 Session 操作边界。

`app.css:37–40` 仅在 MainBar 的 shell-status 直接包含 deferred receipt 时允许该状态区收缩并局部滚动，保留 48px 最小高度及 block padding，同时减小这一状态下两个相邻区域的 margin。未改字体、正文／ID／操作 DOM、路由、切 Session 或焦点逻辑。宽屏状态区不位于 MainBar，不受该选择器影响。

`session-create-race.spec.js:161–197` 经真实 Host 创建 A，暂停 B 的真实成功 201，继续编辑 A 并设置选区；回执到达之前及之后均要求输入框、Send 完全在视口内。真实 wheel 后两个回执入口完全可见，仍保留输入焦点与选区；真实 Tab／Shift+Tab 可到 Send、查看和打开回执入口，关闭后导航可达；只记录一个 Session POST，没有 Run POST，保持单 EventSource。首败测试的原断言均保留，最终额外加强键盘可达验证；截图保存方式变更不替换断言。

逐张查看 `r9/visual/integrated/` 六图：320×360 初始输入及 Send 完整可见，回执上部通过局部滚动查看；wheel 后操作完整可见。320×800 与 1440×360 布局正常。局部视口无须同时容纳全部回执文字；这不等于删去事实。此证据只覆盖桌面 Chromium 的指定视口、wheel 与键盘。

## 合并与适用性

三个冲突原 base／ours／theirs 与固定 Git 对象一致。assets 注册为两端并集；HTML 同时加载 Inbox 与 Settings 样式；pages 删除双方已迁移的 legacy 实现，保留 S08 的两个 re-export 和其余 Behaviour 原文。除三冲突与组合 startup 测试外，21 文件 accepted-sync 中的其余文件逐字等于已接受 S08。accepted base tree 与本人已审 S08 r4 tree 相同。

app.js、shell.js、Inbox／Runs／Source／Run evidence 与 r8 字节相同。原 race 测试完整作为最终文件前缀保留。startup 最终文件等于 r8 加入 S08 三处可见 model row＋隐藏字段 attached 断言，原 S03 的真实断线 Enter 不提交断言仍在。`c34d8462ae6cdf3eb332784972db9521ebf8c070` 仍是祖先，settings_commands.py 与该提交及 accepted base 字节相同。

## 检查及保全

实际读取 `09-integrated-shell-layout.log` 的 15 PASS、`10-integrated-page-consumers.log` 的 2 PASS、`08-integrated-typecheck.log`。不将较早的 3／6 项重复累计。首个固定 r8 的 320×360 输入 viewport ratio 0 FAIL，以及随后动作边缘 ratio 0.9831932783126831 FAIL 均保留；文件名含 green 的第二份日志仍认定 FAIL。

独立脚本核验完整 30 文件、23 文件增量、21 文件同步 diff 与内容哈希；118 source／25 AC 原图 SHA 未变，31 份已完整阅读过的依据字节未变，故不重复遍历完整 source。核验 r8 档案 38 副本、29 retained、7 prior manifest、12 对原始／复制文件与 r9 17 个首败文件。自行从 HTML／JS／CSS 发现闭包 29 资源／63 边，核对注册及 MIME。15 条真实 HTTP 记录绑定固定字节；HTML 只记录 status／body SHA，不推断其未记录的 headers。

本人仅运行只读绑定核验，未运行新产品测试、启动浏览器／服务、占用探针端口或修改仓库、ledger、tracker。核验工具最初误用了 settings_commands.py 目录，随后误假定 startup 等于 accepted S08 全文件；两次工具结果分别保存在 `S03-r9-Spec-verification-first-error.json` 与 `S03-r9-Spec-verification-first-comparison.json`。按实际文件路径及双方独立增量纠正后，最终核验 PASS／errors=[]；不将工具假设错误计为产品失败。

本轮未提升 whole source／AC／G；最终 CI／安装矩阵、S11、native200%／真实中文 IME／实际移动键盘／触屏 NOT RUN，历史 BFCache BLOCKED。先前 Spec 修复保持 `verified_repair_pending_integration`，原 FAIL 证据不覆盖。
