# 首轮完整浏览器门禁失败

候选 `fca0b7194791d264adfa73897746d750176e166f`，端口 `17810`，执行 `npm run test:browser`。结果 **541 passed / 2 failed（11.5m）**，exit 1。完整原始输出见 [04-browser.log](../gates/04-browser.log)，双失败结果树和 `.last-run.json` 逐字节复制，SHA／尺寸见 [manifest](../first-full-browser-failure-manifest.json)。

两个 `S09 migrated Run explicit return restores its visible source ... and honors newer intent` 在 1280px 与 390px 的 `none` 意图阶段均保持焦点和来源可见性，但旧 `toBeCloseTo(...,0)` 在差值恰为 0.5px 时失败。理想 `scrollTop` 为 1558.5／1936.5，实际为 1559／1937；分数内容坐标经当前 Chromium／DPR 1 滚动位置取整后产生半像素边界。原始 `source-position-observations.json`、截图、错误上下文和 trace 保留。

这一批次没有执行剩余 `git diff --check`，不是七项门禁 PASS。修复与后继验证单独记录，不覆盖该首失败。
