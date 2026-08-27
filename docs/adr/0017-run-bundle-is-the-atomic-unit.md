# Run bundle 是 trace 的原子单位，经 staging 原子发布

一个 Run 的全部落盘事实——`meta.json`、`trace.jsonl`、`artifacts/`——装进**同一个目录**，
即 Run bundle：`traces/<UTC-date>/<run_dir_name>/`。

理由不是"删除因此变原子"（`rmtree` 不原子），也不是"避免多个 Run 交错"
（`RunCoordinator` 保证任意时刻至多一个 Run）。理由是**三条边界同构**：
Run 的完整性边界、工件的生命周期边界、导出的边界本来就是同一条线。
让它同时也是文件系统的边界，三者就不可能各自漂移——
[#4](https://github.com/nineofoursyrup/Agent-Alfred/issues/4) 那条「工件与 trace 同生命周期、同保留策略」
于是不再是一条需要被遵守的约定，而是目录结构本身。

顺带消掉一处竞态：文件边界即 Run 边界，于是「轮转不能发生在持久性屏障中间」
（[ADR-0004](0004-flush-barrier-at-run-end.md)）在结构上不可违反，不需要任何互斥逻辑。
按天追加的方案则必须显式禁止屏障期间切文件，是一处自找的竞态；
而且 [#3](https://github.com/nineofoursyrup/Agent-Alfred/issues/3) 的判读规则
「trace 末尾缺 `run.finished` 表示该 Run 未正常收尾」只有在一个 Run 一个文件时，
「末尾」才是个有定义的词。

## 发布必须经 staging

直接 `mkdir` 最终目录再写 `meta.json`，留着一个崩溃窗：`mkdir` 之后、meta 落盘之前崩溃，
盘上就多一个有目录、无元数据的路径。它不可识别，于是按裁剪规则只报告不删——
**永久残骸**，正是「结构可识别即可到期裁剪」想避免的东西从另一个门进来。

因此：在**同一个日期目录**下建 `.staging-<run_storage_id>/`（同目录才谈得上跨文件系统之外的原子改名），
写入并 `fsync` 不可变的 `meta.json` 与空 `trace.jsonl`，`fsync` staging 目录，
**无覆盖原子改名**为最终目录名，再 `fsync` 父目录。**发布**就是 bundle 从不可见变为可识别的那一刻。

点号前缀让 staging 不可能匹配最终目录名的正则，于是「可识别的托管 bundle」与「staging」
是两个不相交的集合，识别器不需要任何额外条件。

## Consequences

残留 staging 的回收放在取得进程锁之后、裁剪之前，但**不是无条件 `rm -rf`**：
仍须验证精确正则、是普通目录而非符号链接、内部结构在允许的形状内、且不是本实例正在使用的。
回收独占，且残留 staging **绝不复用**——复用等于把上一次崩溃的中间状态当成干净起点。

代价是每个 Run 多一次 `mkdir` + 改名 + 两次目录 `fsync`。这是常数开销，
换掉的是一整类"半个 bundle"状态。
