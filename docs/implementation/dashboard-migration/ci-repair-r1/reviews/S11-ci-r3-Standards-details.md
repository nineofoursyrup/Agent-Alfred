# S11 CI r3 Standards 细节

沿用 37cd→4da 的六文件完整独立 Standards 审查及十二项 smell 基线；固定 4da→065 三文件全部增量、最终 app/shell 组合 diff、mainbar-empty-identity 全文、shell 所有 mount 调用及 app restore/dispose 上下文均已阅读。其余三文件逐字节哈希相同；实际 tracked/untracked 工作区均 clean。未读取当前 Spec 轴结论、未运行任何产品测试，也未修改产品、测试、Git、frozen 或 canonical。

`onNavigate` 由 MainBar 绑定既有 retireLocation；shell 的非 initial mount 在任何 !started 返回前同步通知。在导航路由解析及 confirmLeave 成功、URL/history 提交后才进入此路径；同 URL、取消离页、仅面板历史及首次自动 mount 均不调用。浏览器跨页 popstate 和显式 restore 也统一经过该路径。原 page.dispose 重复耦合删除；正常导航仍先退休定位再释放页面其余资源。原 generation/Session/instance/abort 校验与 focus 条件保留，所以新读取开始后，旧请求的 catch/成功分支都会因旧 locateGeneration 退休，不会覆盖新的 loading、内容或焦点。

永久回归先由真实服务生成并保存 Run，再使用已有 legacy 空身份夹具；未替换生产定位、分类、路由和守卫。两分支共用真实 HTTP200 和首 SSE 屏障。navigate 分支在两者均未释放时先断言旧 loading 消失、定位 requestfailed 一次，直接证明即时 abort；随后首 mount、发起新精确定位、编辑 SQL，释放旧取消 handler，断言新 loading 保留且旧消息不存在；新真实200 status applied、合法内容与保存状态可见、SQL 焦点和原草稿保留、零 POST、两次显式读取。same-page 分支保留首自动 mount 后合法空 ID 成功、真正交付 oldDelivery=delivered、完整正文/已保存和回到最新语义。

封存 RED patch 与本次固定测试差异完全一致：旧产品 1 FAIL/1 PASS，失败发生在 releaseEvents 之前；修复后包含旧/新定位、普通导航、Inbox 往返、重连和创建/焦点/click/tap 的 16 例全部 PASS。新增逻辑没有 sleep、超时或重试预算变更。typecheck04、29 assets/protocol03 也通过。原外部 probe 的 route.fulfill 异常边界仍保留，新永久回归无需假设旧 HTTP200 成功投递即可证明取消与隔离。两项真实 P2 均有明确修复与行为证据，不留 residual finding。

81 项只读审计覆盖精确 commit/tree/src/package tree、六文件总范围、三文件增量、固定测试 patch、交接日志哈希、此前全部首败证据及 r2 FAIL 报告的完整性、4da 原交接归档和中止 full-browser exit130。JSON 保存各路径/哈希及检查结果。审计不代替尚未完成的最终产品门禁。

本次 onNavigate delta 不改变 CSS、几何、普通输入/发送、鼠标/键盘缩放路径；4da 原生200%证据可按明确的行为适用性继承，但不是精确本 src tree 重新执行的证明，最终 HTTP 资源字节仍需单独绑定。本轮未对未读原生附件签发新的执行 PASS。完整 browser、四安装变体、同状态升级/支持回退 G08、远端 CI 和待固定 portable addendum 均另行核验；IME 原生真实输入继续 BLOCKED，移动环境排除不变。
