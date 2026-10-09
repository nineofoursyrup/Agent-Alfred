# S07 r5 — exact accepted Memory/listener sync

已将指定的已验收 `da7d6b8` 合入 S07，自动合并无冲突，固定候选与必要消费者检查完成。r4 的 root Standards PASS 0 继续保留；本轮不自评，首次完整 r3 Spec FAIL 仍等待 r3→r5 独立增量复核。原始失败、有界 PASS 和原件全部保全，whole source／AC／G 仍 NOT RUN。

- Worktree: `/Users/nineofour/.codex/worktrees/dashboard-s07/Agent-Alfred`
- Branch: `codex/dashboard-s07`
- Head: `7b1a31a6f4690b90b224c84787efd40dde0766a4`
- Tree: `754b99a69c0c4ade34ee4c6e1df2241d23508634`
- Accepted base: `da7d6b84899b19d690d574c74397a45b0239078b`
- Base tree: `20821980c0bb7977c157416a52db5ac80327b307`
- 工作树 clean，相对 accepted 仍为完整 **12 files**。
- `candidate.diff` SHA-256：`50b7ecfe3a397f82d1650ba79b0dd8063427e57c2b75aa8c1d6833b92c4a5c3b`。

## 同步内容与保留

本轮只有一笔 exact accepted merge，没有新增功能或修复。accepted S05 带入 Memory 页面、memory.css、对应测试和已证因的 listener backlog 修复。`consumer-preservation.json` 确认其六个非共享登记文件均与 accepted 字节相同；S07 Tools JS、所有 Tools migration 用例、MCP fixture 与 MCP／integrations 测试保持 r4 字节。

完整10个 S07 leaf 文件不变，见 `s07-leaf-byte-equality.json`。共享 HTML／asset registry 保留 Memory、Tools、Behaviour、Database 等全部资源；相对新 accepted 各只有 Tools.css 的一处增加。MCP details 展开、Tools→Connections→Tools→identity Ops→snapshot Run→Back 与 nested cleanup 未被替换。没有导入 S08 private CI repair 或 S09 private candidate。

`merge-first.log`、首次 status 与空 stage inventory 保留真实无冲突观察。r3 的历史 HTML 首冲突及原三个 stage 仍在旧证据中，不因本轮无冲突而改写。

## 必要组合结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 定向真实浏览器组合 | **10 PASS，20.3s** | `combination-01.log` |
| Typecheck／完整差异 whitespace | **PASS** | `typecheck-01.log`、`diff-check-01.log` |
| 当前完整 HTTP 引用闭包 | **34 PASS**，normal_close true | `resources.json`、`resources-01.log` |
| wheel／sdist 当前资源 | **各34资源逐字节 PASS** | `package-resources.json`、`build-01.log` |
| 端口释放 | **17980–17989，10/10无 reuse标志 bind PASS** | `port-release.json` |

十个场景包括：

- Tools 注册来源／草稿／焦点、掉真实200回执且无后继输入的正常导航、F2 未知回读后的新输入和原 CAS、普通 MainBar Persona 更新／版本冲突／下一 Run 生效。
- Memory 真实保存→下一 Run 命中→source→编辑冲突→删除重查；只读 Skill catalog 覆盖与重启边界；原 R10/R14 外部镜像编辑／确认恢复／reload；在途保存和后继输入离页区分。
- 实际 MCP 授权与调用、完整身份链接及原 snapshot Run／Back；全程一次 tools/call。
- 新 CSS union 下 Tools 四视口、中央679/680与壳层1099/1100、完整长文本及键盘控制。已查看1440初始和320长标识展开截图；没有整页横向溢出或新 Memory CSS 污染。

这是当前候选的有界消费者组合，不是新的稳定性样本或全部 G03 验收。S05 的 backlog真实故障／首败／因果对照／RED→GREEN、20次 reload与230生命周期结果仍由 accepted S05 的 `r5/HANDOFF.md`、`DIAGNOSIS.md` 及其证据负责；本片只在新组合运行一次原 reload 场景。

当前 HTTP 闭包通过 HTML/script/import/CSS 实际递归发现，并逐个读取验证 MIME、CSP、no-store、nosniff和源字节。相对 r4 **新增 memory.css 1项，index及memory.js变化2项，31项未变，无移除**。两包资源清单均使用这份当前闭包，没有沿用旧33项。四种安装变体的实际 HTTP 验收仍属 S11。

## 原件与检查继承

修复前封存完整 r4 证据及 source archive；`r4-seal.json`／`r4-preservation-readback.json` 确认原46文件逐字节一致。r1/r2/r3 的正式 FAIL、首观测、先前有界 PASS、r4两次RED与13项GREEN均继续保留。完整 candidate／peer sync／r3-to-r5 差异和环境、命令、输入适用性见本目录 JSON 与 `checks.json`。

本轮未机械重跑全21／209套。未变 r4 的输入身份、拒绝／未知写入、F1和退休等确定性检查仍按原范围复用；关键未知导航和 F2 已在新 listener 的组合重验。旧209是历史后端观察，不能覆盖新 listener；listener 已接受的独立 peer 检查和本轮真实 HTTP／浏览器组合分别记录。未变其它后台逻辑没有被宣称重新全量认证。

`source-coverage.json` 完整继承 **39 source／20 AC** 原文档／行／hash／owners／AC/G，39段原文及18份冻结输入再次一致。对 Skill／Persona 的新增记录只限当前候选中两个真实消费者，不把多 owner source 或 G03 自动转为 PASS。

远端 `18d2ac9` S08 lost-MCP-receipt 的 CI 首 FAIL 继续单列；其待审私有修复不在本候选。S09 迁移后完整 Tools→Ops 整链也未被本轮既有 Ops 检查替代。

## 后续责任

- root 安排 accepted-sync 的 Standards 增量及 r3→r5 Spec 增量；本片不自行变更评审结论或 ledger。
- S11 最终 all-owner source／AC／G、最终CI、四种安装HTTP、升级／正常退出／受支持且保留数据的回退。
- 原生桌面200%／真实中文IME仍 NOT RUN；viewport/touch不称真机；真实iPhone/iOS/移动键盘范围外，历史BFCache BLOCKED不变。
- 后续 S08 测试修复必须与现有 details expansion／nested cleanup 合并；回退保持完整 Tools资源与消费者闭包以及已接受 Memory/listener 等 peer修复。

仅本地授权 merge、隔离检查和证据交接；没有 push、GitHub／ledger／integration main写入、部署或真实 provider调用。
