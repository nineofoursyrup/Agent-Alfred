# 首次 Python 全量失败

02 批次原流在 `../gates/02-pytest-output.json`：3 failed、6442 passed、1 deselected、14 warnings（47m41s）。三个失败：

- `test_acceptance_execution_policy.py::test_public_execute_and_grade_never_retry_a_failed_sdk_dispatch[timeout]`
- `test_acceptance_supplement_boundaries.py::test_schema4_dispatch_only_uses_bound_mock_transport`
- `test_acceptance_supplement_boundaries.py::test_formal_execute_rejects_empty_approved_policy_before_mock_call`

三个实际失败都为 `candidate_changed`；第三个因此未到达预期的 `approved_aggregation_missing`。失败块原文以 JSON 字符串无损存放于 `failure-context.json`，完整原流和 SHA-256 不被后继结果覆盖。

`evals/acceptance/candidate.py` 的 `capture()` 采集 HEAD、tree、全部 tracked／未忽略 untracked 文件，`verify()` 比较它们。首轮期间本票写入证据、生成截图并提交产品，违反该测试的候选冻结条件。Python 文件未改不足以满足整树冻结；这是本票检查安排的错误，不把它记为纯计时或已知基线失败。

后继将先复测三个原用例，再在固定候选上重跑全量。运行时日志只写入已由 candidate 排除的 `output/`，测试退出后才归档；不修改 Python、断言、retry 或 timeout。

08 三项原用例复测：3 passed（9.49s），HEAD／tree／全部文件在执行前后完全相同。见 `../gates/08-python-targets-output.json` 与 `08-python-candidate-stability.json`。09 全量运行期间不再修改本 worktree 或提交。

09 完整复测已通过：6445 passed、1 deselected、14 warnings（43m43s）。开始／结束 HEAD、tree 与全部 1630 文件完全相同；原始 02 失败仍单独保留。见 `../gates/09-pytest-output.json` 与 `09-python-candidate-stability.json`。测试退出后才归档本次结果。
