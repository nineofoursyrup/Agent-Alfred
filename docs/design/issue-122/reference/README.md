# Claude Design 原稿身份

当前 Dashboard 视觉权威为 Claude Design 项目 **Syrup agent repository** 的 [Syrup Dashboard.dc.html](https://claude.ai/design/p/6094306e-4393-4c71-865d-5cdfb003d56c?file=Syrup+Dashboard.dc.html)，version **1786950414092924**，原稿更新于 **2026-08-17T07:06:54Z**。用户于 2026-10-10 在 [#122](https://github.com/nineofoursyrup/Agent-Alfred/issues/122) 确认此版本；本目录于同日从本机 `~/Downloads/Syrup agent repository/` 归档。复制前、复制后均核对 SHA-256，保存逐字节副本。

| 文件 | SHA-256 | 用途 |
| --- | --- | --- |
| [Syrup Dashboard.dc.html](<Syrup Dashboard.dc.html>) | `54807adb38541fa2966e5b0c334b9d0e8d71c60b814245601bc4f35b8ec54b84` | 视觉原稿，不修改。 |
| [support.js](support.js) | `8fe7df74405f3c55f49b7249c74ea1397e65d07dea2b1bd3b4a489bec2e28cbe` | 原稿预览运行时，不修改。 |

`support.js` 仅供查看原稿，包含从 unpkg 取得 React、ReactDOM、Babel 的引用，原稿预览不保证离线可用。这两份归档都不是生产静态资源；其中行内样式、运行时、演示数据、Syrup 专有概念和远程依赖不进入 Alfred。生产只使用包内同源 CSS 和系统字体，原稿行内样式以公共类与 token 表达。

本权威取代 #86 的视觉基线，可读性代价与保留语义见 [ADR-0049](../../../adr/0049-claude-design-original-is-dashboard-visual-authority.md)。#86 历史资产不覆盖。后续实现统一引用 [RECIPES.md](../RECIPES.md)；#123 只提供全局外观、基础控件和公共配方，页面重排与状态绑定属于后续票。

核对命令（从仓库根目录运行）：

```sh
shasum -a 256 'docs/design/issue-122/reference/Syrup Dashboard.dc.html' docs/design/issue-122/reference/support.js
```
