S09 r3 独立 Standards 增量：**PASS，0 项硬缺陷，0 项可选 smell 建议**。

固定 head `3139d8a1055619af4c71d89e780633effbb9f595`、tree `8c1cad94125d6d5680ef570e40a36643aa9b671f`、base `da7d6b84899b19d690d574c74397a45b0239078b`，clean。继承原7文件/41source/23AC完整Standards及本人r2增量。全文审查本轮两文件差异、source capture/restore/read/detail/close与Shell滚动调用方、原始日志/观察和同步适用性。

相对锚点只存非正文数值，仍以snapshot/instance/Run核验后的明细为恢复条件；scrollTop=0退出Shell旧绝对位置重放。有限高度裁剪保证来源可见，focus preventScroll避免再移动；pointer/key/focus及新增passive wheel/touchstart退休迟到恢复，close成对解除。没有金额/正文缓存、额外全历史查询或替代snapshot POST。原分页代际及新请求忙态边界保持。

真实旧候选两视口RED保留；修复与相关场景共8个独立用例通过，最终S05同步后3次重复不增加覆盖数。1280/390的正常返回锚点误差均小于0.5px；新输入焦点与真实wheel后的240位置保持，每次一个offset50 GET、零POST。实际查看390正常返回截图，链接和对应Run上下文可见。没有用模拟触摸冒充真机。

已核对唯一HTML冲突的accepted资源并集及34 HTTP字节/安全头；74份新证据、41完整原metadata/hash、23 AC原文、13冻结输入匹配。Memory/listener来自accepted，未变原确定性证据有明确适用性；未为评审重跑产品。完整12项Fowler启发式无新增可操作建议。

原F1在本轴确认机制修复，正式Spec增量与串行集成仍待；原FAIL及全S11/source/AC/G、CI/四安装/升级回退/native缺口保持。Standards：0项；Spec：本报告未评。核验见 `S09-r3-Standards-verification.json`、`accepted-sync-Standards-audit.py`。
