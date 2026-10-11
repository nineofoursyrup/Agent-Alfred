# 首次总览定向失败

`01-overview-output.json`：13 passed / 1 failed。测试由 1440px 桌面直接缩至新中央容器断点 307px，穿过 1100px 壳层断点后导航／主对话收起，中央区实际扩至 883px。原有坐标推算只适用于同一壳层模式；改由 390px 窄屏开始测量 307／308px，保留精确宽度和 1／2 列断言。13 项行为测试均通过。

失败候选的三个文件 SHA-256 在初轮输出 JSON；原始 trace、失败截图保存。原始 `error-context.md` 含尾随空白，以 JSON 字符串无损保存，恢复 `content` 的 UTF-8 字节即得原文件，SHA-256 已记录；没有新增 `.gitattributes` 例外。
