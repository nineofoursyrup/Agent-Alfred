# POST-120-01 独立 Spec 复核

request_id `post120-independent-spec-r1`；generation `1`。**PASS，0 项发现。** 裁定为 `verified_repair_pending_ci`；不构成整体完成或关票批准。

仅绑定新三文件候选：base `63fe1a6ac026084118cbc9585cedb30e7f03a0a2`，tree `123caa591cf8053a9cfbafa7fdbe2e9865e77686`，candidate SHA256 `6150b3a441729251e24ce9834ca5d9017b88fdee3258f5acd7331f8bfbf8e07b`。支持 tree `19be93d8a59b0cb463f7fb2fbcbc2d0d371699b0`。两工作树全部1270/1036个跟踪文件、三文件hash及diff均对应冻结身份；规格未变。旧r2 proposal不用于本结论。

直接合同为 `issue-89/DESIGN.md:195–197,221,264,291,318` 的D10/M18/P14/CE-18、Dashboard AC09及#111 AC06。首败CI37935569616的原trace确认选点发生在过程响应完成前；两版受控RED均在真实Step已出现后仍缺链接，source/trace hash绑定成立。修复在过程DOM更新后只同步可用性改变的引用；不重读图、不改快照/节点/viewport，未变引用保留DOM，失效与恢复保持对应焦点。原始定义展开及阅读焦点不被重建。M14和AC10是相邻未变边界，不列为直接失败责任。

共享Behaviour在非path模式无新增行为；符合 `issue-90/DESIGN.md:119–126`、P11及#114 AC02/AC06。最终1项完整引用生命周期＋137项相关回归与typecheck日志/hash有效；原1280/390键盘断言未改，无等待/重试放宽。未重跑产品检查。

新四安装各620包文件、34资源、16路由、100HTTP与冻结源码/产物一致。新G08以old22c→current123caa→supported19be93核对source/wheel/installed字节；三次正常close0，同状态消息/记忆/清空值/遗忘/账目/草稿/深链保持，显式刷新零POST。公开副本仅删除三处CSRF；原件与hash保留。旧包package subtree与src tree已分别核验，未混作完整候选。

采用 `APPLICABILITY.md` SHA256 `ac789882e0571f05699fcd43e6d4a4934b9bc4be33262e8c3030b2dfc925ff3c`。原81条矩阵原件未改；`../issue-audit-delta.json`仅含#111 AC06、#114 AC02/AC06与#119的12行后继说明。历史原生IME/200%及BFCache边界保留，不冒充本次重跑。修复PR完整CI、预期head合并、实际merge SHA CI及最终逐票写回仍待；原main CI FAIL不被局部PASS覆盖。未修改产品或远端。

Spec发现0项；Standards由另一独立报告裁定。详细绑定与只读核验诊断见 `spec-verification.json`。
