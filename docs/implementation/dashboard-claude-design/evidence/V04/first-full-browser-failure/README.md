# 首次完整浏览器失败

04 批次 `540 passed / 3 failed（14.3m）`；三个失败均在既有 accounting 用例：S09 的新实例撤回在途读、A24/A32/A33 离线副本、A07 重新连接核验。只归档每个失败用例自己的错误上下文、失败截图、trace；没有提交全部 artifact 压缩包或通过用例目录。原流在 `../gates/04-browser-output.json`。

04 在初版视觉输入上启动；运行期间只调整静态 SVG 标题字级 13→14.5px，开始时的 SHA-256 和结束 HEAD 均留在原流里。该批次没有作为最终候选 PASS 复用，后继会在固定的 70b6fae 实现候选上重跑。错误上下文无损编码为 JSON，以避免增加 `.gitattributes` 例外。

后继 06 在固定 `70b6faea2e13d7254a62836c3e1becd3129cb7b9` 上完成：543 passed（12.1m），见 `../gates/06-browser-output.json`。本目录原始失败文件未覆盖。
