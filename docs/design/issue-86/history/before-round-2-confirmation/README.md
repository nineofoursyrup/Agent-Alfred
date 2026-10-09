# #86 视觉原型 r1

这是正在访谈中的原型；Q1–Q3 已确认，A/B/C 及最终视觉基线等待用户审阅。

- [评审入口](review.html)：可选择 1440、1280、390、320 CSS px 画布；底部切换 A/B/C、总览/运行/组件与常规/边界状态。
- [单文件原型](dashboard-prototype.html)：可直接打开；无网络或业务服务依赖。
- [已确认决策与待定项](DECISIONS.md)
- [原型检查记录](QA-r1.md)
- [参考与候选资产清单](manifest.json)

本地启动（在仓库根目录）：

```sh
python3 -m http.server 18786 --bind 127.0.0.1 --directory docs/design/issue-86
```

打开 `http://127.0.0.1:18786/review.html`。此服务器仅供查看静态设计文件，与产品 Dashboard 的 7717 端口及状态数据独立。

`reference/` 保存原始 HTML 与原始 WebP 缩略图的逐字节副本，SHA-256 与父地图一致。源 HTML 引用了未复制的 `support.js`，仅作只读参考来源；实际评审使用本目录的独立原型。

`previews/` 提供已检查的代表性截图。原始参考、原型候选和将来用户批准的验收基线是不同身份；当前没有宣布任何候选已获批准。长期提交或远程分享仍待授权。
