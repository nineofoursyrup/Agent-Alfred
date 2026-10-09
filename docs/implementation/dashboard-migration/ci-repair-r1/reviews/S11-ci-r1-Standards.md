# S11 CI r1 Standards — FAIL

候选 `b21451fb4674ce693cec2d53d16bc94647fa4965`，tree `0b0bef55e1de4f33891abc08ae33f3f03128985f`。6文件完整增量已审；**1个P2硬问题，0新增optional smell**。

**S11-CI-STD-01：触控打开MainBar仍错误退休新建意图。** `app.js:204`在pointerup清除opener；真实Chromium hasTouch+tap事件为pointerdown→touchstart→pointerup→touchend→mousedown→focusin→click。第212行于是把同次激活focus误作独立移焦。held真实201后created=`6d5d795a…`而selected仍null，用户需额外打开回执。违反#89 DESIGN D04:66“点击新建成功后打开新 Session 的 MainBar”及#87 R03的Session/呈现所有权分离。须覆盖触控兼容focus，同时保留独立focus、关闭和导航守卫。

证据：`S11/ci-r1/touch-probe-01.log`与原trace；测试只延迟真实201，无替换结果。现有22定向PASS、40次有界重复PASS、typecheck/29 Python PASS仍有效，但未覆盖此失效。原CI/RED/中间首败保留。完整browser、安装、native200、G08和CI另待新候选，不宣称整体通过。
