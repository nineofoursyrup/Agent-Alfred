"""Machine codes and secret-free HTTP mapping for the Database console."""

from __future__ import annotations

HTTP_STATUS = {
    "invalid_request": 400,
    "sql_rejected": 400,
    "sql_error": 400,
    "database_busy": 409,
    "handle_used": 409,
    "instance_changed": 409,
    "query_cancelled": 409,
    "data_invalidated": 409,
    "protected_identity": 409,
    "handle_expired": 410,
    "not_found": 404,
    "input_too_large": 413,
    "result_too_large": 413,
    "database_unavailable": 503,
    "resource_limit": 503,
    "cleanup_failed": 503,
    "data_invalid": 503,
    "query_timeout": 504,
}

SAFE_DETAIL = {
    "invalid_request": "请求不被接受。",
    "sql_rejected": "这条 SQL 不被允许。",
    "sql_error": "SQL 无法执行。",
    "database_busy": "已有 Database 查询在进行。",
    "handle_used": "这个查询句柄已经用过。",
    "instance_changed": "当前实例已更换。",
    "query_cancelled": "查询已取消。",
    "data_invalidated": "诊断数据已失效。",
    "protected_identity": "保护后无法保持身份关联。",
    "handle_expired": "查询句柄已过期。",
    "not_found": "没有这条查询记录。",
    "input_too_large": "输入超过上限。",
    "result_too_large": "结果超过上限。",
    "database_unavailable": "Database 当前不可用。",
    "resource_limit": "诊断资源不足。",
    "cleanup_failed": "诊断清理失败，已暂停新查询。",
    "data_invalid": "源数据无法安全读取。",
    "query_timeout": "查询超过执行期限。",
}


class ConsoleError(Exception):
    def __init__(self, code: str, *, query_id: str | None = None):
        if code not in HTTP_STATUS:
            raise ValueError(code)
        super().__init__(code)
        self.code = code
        self.query_id = query_id

    def http(self) -> tuple[int, dict[str, str]]:
        body = {"code": self.code, "detail": SAFE_DETAIL[self.code]}
        if self.query_id is not None:
            body["query_id"] = self.query_id
        return HTTP_STATUS[self.code], body
