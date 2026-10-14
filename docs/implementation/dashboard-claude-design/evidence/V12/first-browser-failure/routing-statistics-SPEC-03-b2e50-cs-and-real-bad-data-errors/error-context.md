# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: routing-statistics.spec.js >> SPEC-03 CE-05/12/13/15 legacy counts, unknown metrics and real bad-data errors
- Location: tests/browser/routing-statistics.spec.js:263:1

# Error details

```
Test timeout of 15000ms exceeded.
```

# Page snapshot

```yaml
- generic [ref=e1]:
  - banner "导航面板" [ref=e2]:
    - generic [ref=e3]:
      - link "Alfred 本地助手" [ref=e4] [cursor=pointer]:
        - /url: /overview
        - text: Alfred
        - generic [ref=e5]: 本地助手
      - paragraph [ref=e6]:
        - generic [ref=e8]: 已同步
      - paragraph [ref=e9]: 127.0.0.1:18124
    - navigation "主导航" [ref=e10]:
      - paragraph [ref=e11]: 工作区
      - link "总览" [ref=e12] [cursor=pointer]:
        - /url: /overview
      - link "收件箱" [ref=e13] [cursor=pointer]:
        - /url: /inbox
      - link "运行" [ref=e14] [cursor=pointer]:
        - /url: /runs
      - link "记忆" [ref=e15] [cursor=pointer]:
        - /url: /memory
      - paragraph [ref=e16]: Agent 配置
      - link "模型" [ref=e17] [cursor=pointer]:
        - /url: /models
      - link "连接" [ref=e18] [cursor=pointer]:
        - /url: /connections
      - link "行为" [ref=e19] [cursor=pointer]:
        - /url: /behaviour
      - link "工具" [ref=e20] [cursor=pointer]:
        - /url: /tools
      - paragraph [ref=e21]: 诊断
      - link "用量账本" [ref=e22] [cursor=pointer]:
        - /url: /ops
      - link "数据库" [ref=e23] [cursor=pointer]:
        - /url: /database
    - paragraph [ref=e24]: 本地优先 · 共享运行宿主
  - main [ref=e26]:
    - generic [ref=e27]:
      - generic [ref=e28]:
        - paragraph [ref=e29]: BEHAVIOUR
        - heading "行为" [level=1] [ref=e30]
        - paragraph [ref=e31]: 查看与配置消息分流，区分流程拓扑、路由决策与执行事实。
      - generic "壳层状态与操作" [ref=e32]:
        - button "打开主对话" [expanded] [ref=e33] [cursor=pointer]
        - region "运行与连接摘要" [ref=e34]:
          - paragraph [ref=e35]:
            - generic [ref=e37]: 回复完成 · 已保存
          - link "查看最近运行结果" [ref=e38] [cursor=pointer]:
            - /url: /runs/25687677f57546578d3b088391a4f651
    - generic [ref=e39]:
      - region "消息分流" [ref=e40]:
        - heading "消息分流" [level=2] [ref=e41]
        - paragraph [ref=e42]: 持续设置 · CLI/Web 共享。默认关闭；保存后下一 Run 生效。回复进入当前会话，静默时只记用户消息，不生成助手消息或长期记忆。
        - paragraph [ref=e43]: 已保存的分流设置：开启 · revision 1。能查看结构不代表已经启用或可以成功执行。
        - paragraph [ref=e44]: 启用后识别纯问候、致谢及明确无需回复的消息；实际任务仍进入完整回答。
        - generic [ref=e45]:
          - checkbox "启用消息分流" [checked] [ref=e46]
          - text: 启用消息分流
        - paragraph [ref=e47]: 当前选择：开启 · 编辑基线 revision 1。
        - generic [ref=e48]:
          - button "保存设置" [ref=e49] [cursor=pointer]
          - button "刷新设置" [ref=e50] [cursor=pointer]
        - status [ref=e51]: 已保存；下一 Run 生效。
        - region "流程拓扑" [ref=e52]:
          - button "查看流程" [ref=e53] [cursor=pointer]
          - status [ref=e54]: 服务已连接；结构仅在手动读取时更新。
          - status [ref=e55]: 尚未读取；展开后读取。
        - group [ref=e56]:
          - generic "路由统计" [ref=e57] [cursor=pointer]:
            - text: 路由统计
            - status [ref=e58]: 正在读取统计…
          - paragraph [ref=e59]: 跨会话 CLI/Web 已准入聊天 Run；按准入时刻归窗，仅启用且终态已持久保存的 Run 进入主结果。未知不代表未发生。
          - generic [ref=e60]:
            - text: 统计范围
            - combobox "统计范围" [ref=e61]:
              - option "24小时"
              - option "7天" [selected]
              - option "30天"
              - option "全部"
          - button "刷新统计" [active] [ref=e62] [cursor=pointer]
      - region "手动聚合" [ref=e63]:
        - heading "手动聚合" [level=2] [ref=e64]
        - paragraph [ref=e65]: 单次动作 · 显式生成时固定目标会话与所选来源，草稿进入目标会话。草稿不自动进入工作窗口、后续聚合或提炼；再次生成会创建新运行。
        - generic [ref=e66]:
          - text: 目标会话
          - combobox "目标会话" [ref=e67]:
            - option "请选择目标会话" [selected]
        - generic [ref=e68]:
          - text: 聚合目标
          - textbox "聚合目标" [ref=e69]
        - generic [ref=e70]:
          - text: 聚合关键词
          - textbox "聚合关键词" [ref=e71]
        - group "本次资料来源" [ref=e72]:
          - generic [ref=e74]:
            - checkbox "语义记忆" [checked] [ref=e75]
            - text: 语义记忆
          - generic [ref=e76]:
            - checkbox "情景记忆" [checked] [ref=e77]
            - text: 情景记忆
          - generic [ref=e78]:
            - checkbox "会话窗口" [checked] [ref=e79]
            - text: 会话窗口
        - paragraph [ref=e80]: 未选择的来源不会读取；全不选来源仍可提交，将记录无动作。关键词仅用于所选记忆来源。
        - generic [ref=e81]:
          - button "生成聚合草稿" [ref=e82] [cursor=pointer]
          - button "刷新会话" [ref=e83] [cursor=pointer]
        - paragraph [ref=e84]: 表单没有新增的未提交修改。
        - status [ref=e85]: 本次读取 0 个会话。刷新列表不会改变已提交目标。
        - status [ref=e86]: 尚未提交聚合请求。
        - region "流程拓扑" [ref=e88]:
          - button "查看流程" [ref=e89] [cursor=pointer]
          - status [ref=e90]: 服务已连接；结构仅在手动读取时更新。
          - status [ref=e91]: 尚未读取；展开后读取。
  - region "MainBar" [ref=e92]:
    - generic [ref=e93]:
      - heading "主对话" [level=2] [ref=e94]
      - button "收起主对话" [ref=e95] [cursor=pointer]
    - generic [ref=e96]:
      - generic [ref=e97]: 会话
      - button "新建会话" [ref=e98] [cursor=pointer]
    - region "主对话" [ref=e99]:
      - article [ref=e101]:
        - paragraph [ref=e102]: 你好
        - paragraph [ref=e103]: 你好！
        - generic [ref=e104]: 已保存
    - generic [ref=e105]:
      - generic [ref=e106]: 消息
      - textbox "消息" [ref=e107]:
        - /placeholder: 写下消息；/skills 指定流程…
      - button "发送" [disabled] [ref=e108]
    - paragraph [ref=e109]: Enter 发送 · Shift+Enter 换行
  - status [ref=e110]: 就绪
  - alert [ref=e111]
```
