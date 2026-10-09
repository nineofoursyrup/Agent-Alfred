PASS：Standards 硬违规 0，可选启发式 0。

固定 base `bba439d7faeedde71970a74cea146a5e896e5345` → head `552ab48fc1d757ea881107827e583d592a5403b1`，tree `e4fa378f6e161834d528769865ab97ee1bc4517f`。实际仅 `tests/browser/shell-startup.spec.js:9–38` 新增30行；生产字节与已审 r7 完全相同。

完整 hunk 已审。真实 SSE 请求门控只改变到达顺序；首态前先编辑、等待实际 Inbox 挂载，再通过原生 viewport 切换验证消息框可见、焦点、选区[2,5]、输入内容及宽屏偏好。没有伪造 snapshot、强制恢复焦点、固定 sleep 或削弱旧断点测试；`finally` 释放门控，路由随 Playwright 页面销毁。符合 `VALIDATION.md:7、27、35` 的真实接缝、独立失效方式及保留首次失败规则，也覆盖 #87 `SPEC.md:183` 的断点输入不变量。

已核对旧CI源码1 FAIL、只撤首挂焦点保护1 FAIL、原样r7 1 PASS和最终18 PASS；次数不相加。三份trace里的测试源码／观察附件与候选一致；1210导出文件、单变量diff、原17个CI artifact及30份保全副本通过独立hash核验。原CI FAIL保留，本地因果复现不冒充云端插桩或统计稳定性结论。12项Fowler启发式无新增可操作建议。

未重复运行测试。20资源／42边及完整164 source／25 AC复用已审未变证据，仅核验R02/R10新增片段。whole source/AC/G、最终CI／安装、原生200%及真实IME未升级；BFCache原限制保留。trace未保存所服务JS正文，源码绑定依据固定导出、实际服务配置与请求记录。

详见 [verification](S02-r8-standards-verification.json) 和 [details](S02-r8-standards-details.json)。
