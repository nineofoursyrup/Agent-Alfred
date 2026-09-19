"""Chinese reading aids frozen with each workflow, never a topology source."""

ROUTING_PRESENTATION = {
    "nodes": {
        "classify": {
            "name": "判断消息类型",
            "description": "仅分类当前任务，分类是控制信息；它不生成用户回复",
        },
        "project_context": {
            "name": "整理已准备的上下文",
            "description": "校验并投影已准备的上下文，不在此重新检索或产生模型请求",
        },
        "recover_context": {
            "name": "标记上下文不可用",
            "description": "处理投影故障，不能解释为上下文恢复成功",
        },
        "join_route": {
            "name": "决定处理分支",
            "description": "综合分类、守卫、Skills 与上下文情况选择分支",
        },
        "full_agent": {
            "name": "完整处理",
            "description": "进入图内 Agent 完成实质任务",
        },
        "full_result": {
            "name": "返回完整处理结果",
            "description": "结果出口，交付 Agent 的输出",
        },
        "quick_result": {
            "name": "返回固定短回复",
            "description": "合格问候/感谢返回固定短回复，不是生成式回答",
        },
        "context_failure_result": {
            "name": "返回上下文失败提示",
            "description": "结果出口，产生固定失败提示",
        },
        "no_reply_terminal": {
            "name": "按要求不回复",
            "description": "无动作出口，保留用户记录，不生成助手消息或长期记忆",
        },
    },
    "branches": {
        "join_route": {
            "quick": (
                "分类为问候/感谢、匹配有限短语且没有已加载 Sk"
                "ills，使用固定短回复"
            ),
            "full": "分类要求完整处理",
            "fallback": "分类未知、短语守卫不匹配或已有 Skills，改由图内完整处理",
            "no_action": (
                "分类为无需回复且匹配守卫；该判断先于上下文不可用"
                "和 Skills 分支"
            ),
            "context_failure": "上下文不可用，返回固定提示",
        }
    },
    "policy": (
        "图外策略：关闭分流走普通对话。图不可用或失败时，"
        "普通循环回退受副作用、上下文、安全停止和剩余预算"
        "等条件限制；图内 fallback "
        "仅表示进入完整处理。"
    ),
}

AGGREGATION_PRESENTATION = {
    "nodes": {
        "aggregation_input": {
            "name": "接收本次聚合请求",
            "description": "使用已固定的目标会话、目标、关键词及来源选择",
        },
        "semantic_source": {
            "name": "读取语义记忆",
            "description": "关键词检索获准使用的事实，受容量限制",
        },
        "semantic_recovery": {
            "name": "记录语义来源读取失败",
            "description": "保留该来源降级事实，允许其余资料继续汇合",
        },
        "episodic_source": {
            "name": "读取情景记忆",
            "description": "关键词检索获准使用的经历摘要，受容量限制",
        },
        "episodic_recovery": {
            "name": "记录情景来源读取失败",
            "description": "保留情景来源降级事实，允许其余资料继续汇合",
        },
        "history_source": {
            "name": "读取目标会话近期对话",
            "description": (
                "获准使用、已完成普通 chat 的完整轮次，不做"
                "关键词检索，不含聚合草稿"
            ),
        },
        "history_recovery": {
            "name": "记录会话来源读取失败",
            "description": "保留会话来源降级事实，允许其余资料继续汇合",
        },
        "join": {
            "name": "汇总并整理资料",
            "description": "汇合可用资料，按既有规则裁剪；决定综合还是无动作",
        },
        "synthesis": {
            "name": "综合为草稿",
            "description": "使用本次剩余资料生成候选草稿",
        },
        "validate_result": {
            "name": "检查草稿与引用",
            "description": (
                "检查非空、引用格式与编号存在性；不验证事实真伪；"
                "成功为草稿结果出口"
            ),
        },
        "sources_not_selected": {"name": "未选择来源", "description": "无动作出口"},
        "sources_unavailable": {
            "name": "资料为空且有来源读取失败",
            "description": "无动作出口，不表示所有来源都失败",
        },
        "capacity_excluded_all": {
            "name": "资料为空且存在容量裁剪",
            "description": "无动作出口，含容量、轮数上限和完整请求裁剪",
        },
        "sources_excluded_all": {
            "name": "资料为空且存在使用许可排除",
            "description": "无动作出口，不表示全库均被禁止",
        },
        "no_matching_sources": {
            "name": "没有匹配资料",
            "description": "无动作出口，前述原因均不成立",
        },
    },
    "branches": {
        "join": {
            "synthesize": "仍有可用资料，进入综合为草稿；部分来源失败不妨碍继续综合",
            "sources_not_selected": (
                "未选择来源；无动作，不生成草稿。空结果按此顺序选"
                "择主原因，不是全部空缺因素清单。"
            ),
            "sources_unavailable": (
                "资料为空且有来源读取失败；无动作，不生成草稿。空"
                "结果按此顺序选择主原因，不是全部空缺因素清单。"
            ),
            "capacity_excluded_all": (
                "资料为空且存在容量裁剪；无动作，不生成草稿。空结"
                "果按此顺序选择主原因，不是全部空缺因素清单。"
            ),
            "sources_excluded_all": (
                "资料为空且存在使用许可排除；无动作，不生成草稿。"
                "空结果按此顺序选择主原因，不是全部空缺因素清单。"
            ),
            "no_matching_sources": (
                "没有匹配资料；无动作，不生成草稿。空结果按此顺序"
                "选择主原因，不是全部空缺因素清单。"
            ),
        }
    },
    "policy": (
        "图外策略：聚合失败不进入普通 Agent 回退。"
        "来源选择不改变声明结构，不表示本次实际执行或跳过"
        "；所有无动作均不生成草稿。"
    ),
}
