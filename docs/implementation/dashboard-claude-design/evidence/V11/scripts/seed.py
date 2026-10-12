import json
import sqlite3
from pathlib import Path
from agent_alfred import schema

state = Path('.scratch/v11/state')
state.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(state / 'db.sqlite3')
schema.migrate(conn)
titles = ['核对今日任务与记录状态', '整理本地文档并保留来源', '检查历史运行的边界状态',
          '长标题：保留完整中文与标识 Session / source?anchor#，在窄屏阅读和来源返回后仍可定位',
          '历史会话：相同内容有独立身份', '离线测试：确认显式继续会话']
for index, title in enumerate(titles):
    session = f'v11-session-{index + 1}'
    conn.execute('INSERT INTO sessions(session_id,created_at,activity_revision) VALUES (?,?,?)',
                 (session, '2026-10-12T01:00:00Z', schema.allocate_activity_revision(conn)))
    for run_index in range(3):
        identity = f'v11-run-{index + 1}-{run_index + 1}'
        outcome = ['completed', 'max_steps', 'failed'][run_index]
        prompt = title if run_index == 0 else f'{title} · 继续核对 {run_index + 1}'
        conn.execute('INSERT INTO runs(run_id,purpose,session_id,gateway,prompt_preview,phase,outcome,accepted_at,started_at,finished_at,activity_revision,admission_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (identity, 'chat', session, 'cli', prompt, 'finished', outcome,
                      '2026-10-12T01:00:00Z', '2026-10-12T01:00:01Z', '2026-10-12T01:00:02Z',
                      schema.allocate_activity_revision(conn), 'admitted'))
        for role, text in [('user', prompt), ('assistant', '离线模型回复：保留来源、运行结局和记录状态。\n详细信息继续由现有读取提供；只读预览不会切换主对话。')]:
            conn.execute('INSERT INTO agent_log(session_id,role,content,source,created_at,run_id) VALUES (?,?,?,?,?,?)',
                         (session, role, json.dumps([{'type':'text','text':text}], ensure_ascii=False),
                          'cli', '2026-10-12T01:00:02Z', identity))
conn.commit()
conn.close()
