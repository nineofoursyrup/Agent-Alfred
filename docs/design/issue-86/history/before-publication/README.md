# #86 视觉原型 r1

Q1–Q6 已确认，采用 **A（架构优先）**；本地视觉设计已冻结。B/C 保留为对照，远端资产发布与 Issue 收尾尚未执行。

- [评审入口](review.html)：可选择 1440、1280、390、320 CSS px 画布；底部切换 A/B/C、总览/运行/组件与常规/边界状态。
- [单文件原型](dashboard-prototype.html)：可直接打开；无网络或业务服务依赖。
- [完整设计与分票边界](DECISIONS.md)
- [批准记录](APPROVAL.md)
- [冻结的视觉基线](baseline/r1/manifest.json)
- [设计参数](tokens.json)
- [原型检查记录](QA-r1.md)
- [参考与候选资产清单](manifest.json)

本地启动（在仓库根目录）：

```sh
python3 -m http.server 18786 --bind 127.0.0.1 --directory docs/design/issue-86
```

打开 `http://127.0.0.1:18786/review.html`。此服务器仅供查看静态设计文件，与产品 Dashboard 的 7717 端口及状态数据独立。

`reference/` 保存原始 HTML 与原始 WebP 缩略图的逐字节副本，SHA-256 与父地图一致。源 HTML 引用了未复制的 `support.js`，仅作只读参考来源；实际评审使用本目录的独立原型。

`previews/` 保留访谈时的代表性截图；`baseline/r1/` 为获批 A 的固定资产包，包含独立源文件、tokens 和按明确视口捕获的截图。原型代码中的历史“尚未定稿”标记原样保留，批准状态由 `APPROVAL.md` 和 manifest 记录。`history/before-round-2-confirmation/` 保存批准前文档和清单。

原始参考、获批 A 与 B/C 对照分别标识；长期提交或远程分享仍待授权。后续修订另建基线版本，不覆盖 `baseline/r1/`。
