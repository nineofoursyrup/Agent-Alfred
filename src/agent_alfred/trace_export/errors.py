"""Closed, body-free export failures."""

DETAILS = {
    "unknown_run": "找不到持久运行。",
    "not_stopped": "运行或记录尚未稳定，请收尾后手动重试。",
    "trace_pruned": "该运行的追踪已由系统裁剪。",
    "trace_missing": "追踪文件缺失，未发现系统裁剪证据。",
    "unsupported_format": "追踪格式不受支持。",
    "corrupt_trace": "追踪结构损坏，不能生成。",
    "unsafe_source": "来源身份或路径安全检查失败。",
    "source_changed": "来源发生变化，请清理后重新生成。",
    "invalidated": "记忆、保护规则或实例已变化，请重新生成。",
    "busy": "已有导出任务占用资源，请等待其清理。",
    "source_limit": "源文件合计超过 512 MiB。",
    "output_limit": "净化后的未压缩文件合计超过 512 MiB。",
    "zip_limit": "ZIP 文件超过 512 MiB。",
    "disk_full": "磁盘空间不足。",
    "io_failed": "文件读写失败。",
    "generation_timeout": "生成超过 60 秒预算。",
    "download_timeout": "下载超过 120 秒预算，可能已有文件残片。",
    "expired": "文件等待下载超过 5 分钟，请重新生成。",
    "cancelled": "导出已取消。",
    "disconnected": "下载连接中断，请重新生成；已有字节无法撤回。",
    "credential_invalid": "下载凭据无效、已使用或属于旧实例。",
    "cleanup_failed": "资源清理受阻，新导出暂停。",
    "invalid_request": "导出请求不被接受。",
    "shutting_down": "服务正在关闭。",
    "not_found": "找不到当前实例的导出任务。",
}


class ExportError(Exception):
    def __init__(self, code):
        assert code in DETAILS
        self.code = code
        super().__init__(code)

    def response(self):
        status = 404 if self.code in {"unknown_run", "not_found"} else 409
        if self.code == "invalid_request":
            status = 400
        if self.code in {"io_failed", "disk_full", "cleanup_failed", "shutting_down"}:
            status = 503
        return status, {"code": self.code, "detail": DETAILS[self.code]}
