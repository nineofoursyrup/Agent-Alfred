# Dashboard 公共视觉配方

契约：`DASHBOARD-CLAUDE-DESIGN-RECIPES-v1` · #123 · 2026-10-10。视觉权威为 [Claude Design 原稿 version 1786950414092924](reference/README.md)，取舍见 [ADR-0049](../../adr/0049-claude-design-original-is-dashboard-visual-authority.md)。公共样式全部由现有 `/assets/app.css` 提供；以下 `sy-` 类名是后续十个并行 agent 的稳定接缝。页面只添加所需类，不复制配方、不在页面 CSS 中重定义公共类，不添加行内 `style` 或远程字体／资源。页面原有数据、标题层级、可访问名称、region 和按钮名称由所属页面保留。

## Token 角色

各页引用 CSS 自定义属性，颜色和字级不另写字面值。现有 `--secondary`、`--muted`、`--border`、`--control` 保持名称，含义按新权威切换。

| 类别 | 稳定 token |
| --- | --- |
| 背景 | `--canvas` 画布；`--rail` 导航／MainBar；`--surface` 面板／指标卡；`--raised` 输入／芯片／控制台；`--hover` 悬停；`--selected` 选中；`--bubble-border` 用户气泡边。 |
| 边线 | `--shell-border` 壳层分隔；`--border` 卡片；`--control` 控件／芯片；`--chip-emphasis-border` 强调芯片；`--panel-divider` 面板头；`--row-divider` 行；`--line` 为行分隔兼容别名；`--bar-track` 条形轨道；`--scrollbar` 滑块。 |
| 文字 | `--text` 正文；`--secondary` 次级正文；`--tertiary` 三级；`--muted` 说明；`--weak` 弱说明；`--metadata` 眉标／元数据；`--faint` 表头／路径。 |
| 语义 | `--accent`、`--accent-hover`、`--success`、`--attention`、`--error`、`--focus`；`--diagram-line`、`--diagram-arrow` 只用于图示。颜色之外必须保留文字事实。 |
| 字体 | `--font-sans` 系统无衬线；`--font-mono` 系统等宽。数字、标识、眉标、元数据、按钮用等宽。 |
| 字级 | `--font-size-title` 27px、`--font-size-brand` 19px、`--font-size-body` 13px、`--font-size-row` 12.5px、`--font-size-input` 12px；`--font-size-eyebrow` 10px、`--font-size-panel-eyebrow` 11px、`--font-size-list-header` 10.5px；`--font-size-meta-small`／`--font-size-meta`／`--font-size-meta-large` 为 10.5／11／11.5px；`--font-size-table` 13px、`--font-size-table-header` 10.5px。 |
| 指标 | `--font-size-metric` 23px（总览）、`--font-size-metric-memory` 20px、`--font-size-metric-usage` 22px。 |
| 圆角 | `--radius-nav` 7px；`--radius-control` 8px；`--radius-input` 9px；`--radius-console` 10px；`--radius-metric` 11px；`--radius-usage` 12px；`--radius-panel` 14px；`--radius-pill` 20px。 |
| 间距／动效 | `--gap-small`／`--gap-medium`／`--gap-large` 为 12／14／16px；`--selected-transition` .18s；`--pulse-duration` 1.4s。后两者不在各页覆盖。 |

眉标、元数据和表头采用原稿低对比度色。此处不恢复 #86 的 4.5:1 或 14/13/12px 下限；错误色与 3px 焦点继续有效。

## 基础控件

原生 `button`、`input`、`select`、`textarea`、`table`、`dialog`、`a` 自动使用同源公共样式。按钮采用等宽 11.5px，单行字段按原稿搜索框采用等宽 12px；文本框采用正文 13px，SQL／代码字段用 `--font-mono` 与 `--font-size-row`。控件边为 `--control`，输入焦点边为 `--accent`，键盘焦点为 `3px solid var(--focus)`，两者并存。占位符不替代标签，禁用原因继续在邻近文字中显示。强调操作可给按钮添加 `sy-button-accent`；不会改变点击、禁用或保存行为。表格保留 `table` 语义，表头为等宽 10.5px 最弱色，正文 13px、行分隔为 `--row-divider`；局部滚动继续使用现有 `.table-scroll`。对话框不改变现有确认流程。

## 面板

| 类 | 用法 |
| --- | --- |
| `sy-panel` | 面板外层：卡片底／边、14px 圆角。 |
| `sy-panel-header` | 面板头，15×18px，左右信息可换行，底部标题分隔。 |
| `sy-panel-eyebrow` | 标题或眉标，等宽 600 11px、2px 字距；默认强调色。 |
| `sy-panel-description` | 头部说明，12.5px、说明色。 |
| `sy-panel-body` | 面板内容，17×19px；内部布局由页面负责。 |

```html
<section class="sy-panel" aria-labelledby="page-owned-title">
  <header class="sy-panel-header">
    <div>
      <h2 id="page-owned-title" class="sy-panel-eyebrow">现有区块名称</h2>
      <p class="sy-panel-description">现有事实说明</p>
    </div>
    <!-- 现有搜索或操作；没有事实则不增加装饰占位 -->
  </header>
  <div class="sy-panel-body"><!-- 原有内容 --> </div>
</section>
```

## 指标、行列表、标签和芯片

| 类 | 用法 |
| --- | --- |
| `sy-metric-card`、`sy-metric-value`、`sy-metric-label` | 14×15px 指标卡、23px 等宽数值与 11px 说明；数值来自已有真实指标。 |
| `sy-metric-memory`、`sy-metric-usage` | 加在指标卡外层，分别取 20／22px；用量卡另取 12px 圆角。 |
| `sy-row-list`、`sy-row` | 列表容器与带分隔的行，正文 12.5px；保留现有列表或链接语义。 |
| `sy-row-content`、`sy-row-meta` | 可换行正文与等宽 11px 元数据；结局、记录状态等关键文字不截断。 |
| `sy-list-header` | 等宽 600 10.5px、2px 字距、眉标色的列表头。 |
| `sy-tab` | 原生标签按钮；选中用已有的 `aria-selected=true`（真实 tab）或 `aria-pressed=true`（切换按钮），不由类伪造语义。 |
| `sy-chip`、`sy-chip-emphasis` | 抬升面、控件边、8px 圆角、等宽 10.5px；强调变体取强调芯片边。 |

`sy-row` 的选中外观由已有 `aria-selected=true`、链接 `aria-current=page` 或页面事实 `data-selected=true` 激活。不要仅为样式给普通列表行增加不适用的 ARIA 属性。选中底以伪元素显示，左侧 2px 内嵌强调条；行和标签按钮仅对选中层透明度作 .18s 过渡，不过渡尺寸、位置或状态颜色。`sy-row` 不自动添加悬停动作、选择行为或数据读取。

## 横向条形

`sy-bar-row` 容器、`sy-bar-heading` 标签与数值行、`sy-bar-track` 7px SVG 轨道、`sy-bar-fill` 填充。页面依据现有计数与明确分母计算宽度，只写 SVG 呈现属性；没有数值或分母时显示现有未知文字，不把缺失替换为零，不画推测条形。文字数值始终可见，SVG 是辅助显示，可以 `aria-hidden=true`。

下例只说明结构；`42` 与 `100` 必须由页面真实事实替换，不是产品默认数据。

```html
<div class="sy-bar-row">
  <div class="sy-bar-heading"><span>现有类别</span><span>42 / 100</span></div>
  <svg class="sy-bar-track" aria-hidden="true">
    <rect class="sy-bar-fill" width="42%" height="7" />
  </svg>
</div>
```

## 状态点与语义色

`sy-status-dot` 提供 6px 静态点；`sy-tone-accent`、`sy-tone-success`、`sy-tone-attention`、`sy-tone-error`、`sy-tone-muted` 可加到文字、眉标、条形轨道或状态点上。默认中性色不证明成功，点旁保留现有状态文字。

`sy-status-dot sy-pulse` 仅提供原稿 `sy-pulse`（透明度 .35↔1、1.4s）样式。#123 不将它绑定到生产 DOM，不自动点亮任何状态。后续壳层票只能依据已有的已连接或当前 Run 事实添加 `sy-pulse`；断连、未知、空闲时移除，不能由计时器或装饰推断事实。静态架构说明不使用脉冲。

```html
<!-- 后续票根据真实观测选择颜色、文字和是否添加 sy-pulse -->
<span class="sy-status-dot sy-tone-muted" aria-hidden="true"></span>
<span>现有状态文字</span>
```

`prefers-reduced-motion: reduce` 下全局规则同时覆盖元素和 `::before`／`::after`，停止全部动画与过渡；文字、选中外观与事实不变。后续页面不覆盖此规则。

## 责任与验证

页面的栅格、顺序、断点和业务绑定由后续票实现；本票保持页面与壳层现有结构。公共配方不引入运行时依赖、样例业务数据或新 HTTP/SSE。`tests/browser/tools-visual.spec.js` 延续宽度、长内容、完整身份、键盘和 3px 焦点断言，并验证原稿字级、选中态过渡限制及状态点／减少动效；原稿每个 token 的逐项永久断言不增加。
