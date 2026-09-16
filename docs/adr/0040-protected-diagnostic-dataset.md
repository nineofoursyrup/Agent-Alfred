# ADR-0040：用户 SQL 只查询预先保护的短命诊断数据集

**状态：** 已接受（#65 Resolution r1）

## 决策

用户从 Dashboard Database 提交的 SQL 只查询一次完整构建、预先保护的短命诊断数据集，不把用户 SQL 交给源库。准备完成后立即结束源库读事务。

## 理由

输出后再脱敏会被表达式（hex、substr、JOIN、聚合）绕过。先保护再开放 SQL，使切片、编码和统计只能接触保护后的值。

## 代价

准备成本、引用对象整表失败、以及统计描述的是保护后的值。`memory_revision` 或保护规则版本变化使副本失效。受管清理要等 worker、源连接、数据集和发送缓冲实际释放，不能只隐藏 UI。

## 规范

#65 Resolution r1：<https://github.com/nineofoursyrup/Agent-Alfred/issues/65#issuecomment-5686473812>
