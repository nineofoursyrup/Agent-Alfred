# Fixed local tokenizer data

`deepseek_v4_tokenizer.json` is the data-only tokenizer JSON downloaded from the
official DeepSeek documentation's V4 tokenizer archive on 2026-10-07.

- Documentation: https://api-docs.deepseek.com/quick_start/token_usage/
- Archive: https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip
- Archive SHA256: `e7310d1dafe0a86d8a5629fe78a7c763760f651db9b8682718a1781dcd6fe495`
- JSON SHA256: `89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614`
- Loader: `tokenizers==0.22.2`, `Tokenizer.from_str`, local JSON only.

The accompanying downloaded Python example is not included or executed. No
remote code or model loader is used. The complete canonical request is counted
without truncation and receives 256 planning tokens of overhead. This is a local
estimate, not actual provider usage or a billable-input upper bound. Only V3
judge/review operations use it; product/auxiliary operations retain their original
UTF-8 byte estimate. Installation/candidate verification binds these exact bytes.
