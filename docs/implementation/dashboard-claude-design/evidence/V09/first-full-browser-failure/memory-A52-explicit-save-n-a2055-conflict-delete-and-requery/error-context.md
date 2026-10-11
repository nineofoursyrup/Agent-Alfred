# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: memory.spec.js >> A52: explicit save, next-Run hit, source, edit conflict, delete and requery
- Location: tests/browser/memory.spec.js:128:1

# Error details

```
Test timeout of 15000ms exceeded.
```

# Page snapshot

```yaml
- generic [ref=f1e1]:
  - banner "导航面板" [ref=f1e2]:
    - link "Agent-Alfred 本地助手" [ref=f1e3] [cursor=pointer]:
      - /url: /overview
      - text: Agent-Alfred
      - generic [ref=f1e4]: 本地助手
    - navigation "主导航" [ref=f1e5]:
      - paragraph [ref=f1e6]: 工作区
      - link "总览" [ref=f1e7] [cursor=pointer]:
        - /url: /overview
      - link "收件箱" [ref=f1e8] [cursor=pointer]:
        - /url: /inbox
      - link "运行" [ref=f1e9] [cursor=pointer]:
        - /url: /runs
      - link "记忆" [ref=f1e10] [cursor=pointer]:
        - /url: /memory
      - paragraph [ref=f1e11]: Agent 配置
      - link "模型" [ref=f1e12] [cursor=pointer]:
        - /url: /models
      - link "连接" [ref=f1e13] [cursor=pointer]:
        - /url: /connections
      - link "行为" [ref=f1e14] [cursor=pointer]:
        - /url: /behaviour
      - link "工具" [ref=f1e15] [cursor=pointer]:
        - /url: /tools
      - paragraph [ref=f1e16]: 诊断
      - link "用量账本" [ref=f1e17] [cursor=pointer]:
        - /url: /ops
      - link "数据库" [ref=f1e18] [cursor=pointer]:
        - /url: /database
    - paragraph [ref=f1e19]: 本地优先 · 共享运行宿主
  - generic [ref=f1e20]:
    - generic "壳层状态与操作" [ref=f1e21]:
      - button "打开主对话" [expanded] [ref=f1e22] [cursor=pointer]
      - region "运行与连接摘要" [ref=f1e23]:
        - paragraph [ref=f1e24]: 已同步
    - main [ref=f1e25]:
      - heading "记忆" [level=1] [ref=f1e26]
      - generic [ref=f1e27]:
        - tablist "记忆类别" [ref=f1e28]:
          - tab "语义记忆" [selected] [ref=f1e29] [cursor=pointer]
          - tab "情景记忆" [ref=f1e30] [cursor=pointer]
          - tab "Skill 目录" [ref=f1e31] [cursor=pointer]
        - tabpanel "语义记忆" [ref=f1e32]:
          - generic [ref=f1e33]:
            - heading "语义记忆" [level=2] [ref=f1e34]
            - button "新建语义记忆" [ref=f1e36] [cursor=pointer]
            - form "搜索语义记忆" [ref=f1e37]:
              - generic [ref=f1e38]:
                - text: 文本
                - textbox "文本" [ref=f1e39]
              - generic [ref=f1e40]:
                - text: 主题（精确匹配）
                - textbox "主题（精确匹配）" [ref=f1e41]
              - button "搜索" [ref=f1e42] [cursor=pointer]
              - button "显示全部" [ref=f1e43] [cursor=pointer]
            - generic "语义记忆列表" [ref=f1e44]:
              - article [ref=f1e45]:
                - heading "Zephyr" [level=3] [ref=f1e46]
                - paragraph [ref=f1e47]: Zephyr likes coriander
                - generic [ref=f1e48]: 版本 1 · 创建：工具写入 · 调用 memory-save · 2026-10-11T02:05:38.301274+00:00 · 最近修改：工具写入 · 调用 memory-save · 2026-10-11T02:05:38.301274+00:00 · 人工保护
                - button "查看详情" [ref=f1e49] [cursor=pointer]
        - heading "记忆维护" [level=2] [ref=f1e50]
        - generic [ref=f1e51]:
          - heading [level=2] [ref=f1e52]:
            - button "操作回执／遗忘进度" [expanded] [ref=f1e53] [cursor=pointer]: 操作回执／遗忘进度 −
          - paragraph [ref=f1e54]: 本标签页尚无记忆操作。
          - region "记忆操作回执" [ref=f1e56]:
            - paragraph [ref=f1e57]: 本标签页尚无记忆操作。
        - generic [ref=f1e58]:
          - heading [level=2] [ref=f1e59]:
            - button "提炼队列" [expanded] [active] [ref=f1e60] [cursor=pointer]: 提炼队列 −
          - paragraph [ref=f1e61]: 当前队列页 1 · 1 个会话／0 个批次 · 本页无批次 · 当前筛选已到末页；不是全局待办总数。
          - region "提炼队列" [ref=f1e63]:
            - paragraph [ref=f1e64]: 达到阈值后，于正常聊天保存后的空闲机会自动启动；不提供绕过阈值的立即提炼。
            - generic [ref=f1e65]:
              - generic [ref=f1e66]:
                - text: 按会话 ID 筛选队列
                - textbox "按会话 ID 筛选队列" [ref=f1e67]
              - button "筛选队列" [ref=f1e68] [cursor=pointer]
            - generic [ref=f1e69]:
              - button "上一页" [disabled] [ref=f1e70]
              - text: 第 1 页（每页最多 50 个会话与 50 个批次）
              - button "下一页" [disabled] [ref=f1e71]
            - paragraph
            - generic [ref=f1e72]:
              - article [ref=f1e73]:
                - strong [ref=f1e74]: 会话 c932601e76854a48b9a7d0ee6263c674
                - paragraph [ref=f1e75]: 有效未处理 2 / 阈值 10
              - paragraph [ref=f1e76]: 当前队列页暂无提炼批次。
        - generic [ref=f1e77]:
          - heading [level=2] [ref=f1e78]:
            - button "检索统计" [ref=f1e79] [cursor=pointer]: 检索统计 ＋
          - paragraph [ref=f1e80]: 已读取 · 全部会话 · [2026-10-04T02:05:39.942103+00:00, 2026-10-11T02:05:39.942103+00:00)
        - generic [ref=f1e81]:
          - heading [level=2] [ref=f1e82]:
            - button "Markdown 镜像" [ref=f1e83] [cursor=pointer]: Markdown 镜像 ＋
          - paragraph [ref=f1e84]: 事实：已同步并核验；情景：已同步并核验
  - region "MainBar" [ref=f1e85]:
    - generic [ref=f1e86]:
      - heading "主对话" [level=2] [ref=f1e87]
      - button "收起主对话" [ref=f1e88] [cursor=pointer]
    - generic [ref=f1e89]:
      - generic [ref=f1e90]: 会话
      - button "新建会话" [ref=f1e91] [cursor=pointer]
    - region "主对话" [ref=f1e92]:
      - generic [ref=f1e93]:
        - article [ref=f1e94]:
          - paragraph [ref=f1e95]: 请记住 Zephyr：Zephyr likes coriander
          - paragraph [ref=f1e96]: "已记住。 系统操作回执： {\"operation_id\": \"4445c6e8ed6f566eb10e760b78c61dcb\", \"action\": \"save\", \"kind\": \"semantic\", \"status\": \"saved\", \"memory_id\": \"91ac08e5-9892-4221-871a-4a14ef413b1b\", \"record_version\": 1, \"affected_count\": 1, \"committed_at\": \"2026-10-11T02:05:38.301634+00:00\"}"
          - paragraph [ref=f1e97]: Skill 已降级：部分流程未使用或选择器不可用；请查看运行详情中的本次输入。
          - generic [ref=f1e98]: 已保存
        - article [ref=f1e99]:
          - paragraph [ref=f1e100]: Zephyr likes what
          - paragraph [ref=f1e101]: 离线模型回复
          - paragraph [ref=f1e102]: Skill 已降级：部分流程未使用或选择器不可用；请查看运行详情中的本次输入。
          - generic [ref=f1e103]: 已保存
    - generic [ref=f1e104]:
      - generic [ref=f1e105]: 消息
      - textbox "消息" [ref=f1e106]:
        - /placeholder: 写下消息；/skills 指定流程…
      - button "发送" [disabled] [ref=f1e107]
    - paragraph [ref=f1e108]: Enter 发送 · Shift+Enter 换行
  - status [ref=f1e109]: 就绪
  - alert [ref=f1e110]
```