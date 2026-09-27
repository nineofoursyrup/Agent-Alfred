# 受保护材料预检（#99）

`agent_alfred.evals.acceptance.materials` 提供离线、有界的大对象接收路径。
完整 manifest 内的文件按原字节复制，接收端重新核对固定 manifest SHA256、
每文件 digest 和长度、成员顺序、允许对象与 scope，再通过原 `EvidenceStore`
读取完整父链并在全新 `evidence/` 中导入。原目录不执行脚本、不写报告、不更新指针。

执行元数据使用闭合 `V1-CALIBRATION-MATERIAL-REFERENCE` version 1。
未知字段或版本、重复 JSON 键、非有限数、任意 URL、路径越界、symlink、
缺失、替换、部分对象、长度或范围不符均拒绝；不得退回只验摘要。
旧 schema1/2/3/4 和 P2 canary 的闭合字段、256 KiB wire 与用途不变。
传输上限为 2048 个文件、单文件 32 MiB、manifest/对象索引各 512 KiB、
总内容 128 MiB；这些是存储资源边界，不是质量政策。

本地 vault 根必须为当前用户拥有的 private 目录（0700）；内容对象是只通过
固定 root 读取的 digest 名称，不是下载 URL。对象引用本身不授予读取权限：
接收策略另行配置 scope、精确对象允许清单和固定 manifest hash。
该离线本地适配不能证明真实账户间隔离；同一 OS 用户可以修改其文件，
真实部署的 ACL、独立身份和受控渠道仍须 #105 的独立证据。

公共 CLI（目录与 hash 均由操作者明确选择；下面仅为合成示例）：

```sh
python -m agent_alfred.evals.acceptance materials-export \
  --input /tmp/synthetic-materials --store /tmp/private-material-vault \
  --material-scope synthetic-calibration --manifest-sha256 <pinned-manifest-sha256> \
  --batch-path materials/batch.json --proposal-path plan/proposal.json \
  --evidence-store-path evidence --output /tmp/material-reference.json

python -m agent_alfred.evals.acceptance materials-preflight \
  --input /tmp/material-reference.json --store /tmp/private-material-vault \
  --material-scope synthetic-calibration --manifest-sha256 <pinned-manifest-sha256> \
  --allowed-material-object <receiver-approved-object-sha256> \
  --workspace /tmp/new-material-intake
```

Python 入口为 `ProtectedMaterialStore`、`export_materials`、`parse_reference`、
`read_reference`、`preflight_materials`。输出目录必须全新且位于原材料库之外，
`materials/` 保留完整传输内容，`evidence/` 是新导入库，
`material-admission.json` 最后落盘，含完整对象、manifest、batch、proposal 与报告自身
的不可变 digest。失败只可能留下新的不完整目录，不存在有效 admission 报告；
后续操作不得把该目录视为已接收成功。

本地读写沿用共享构造回滚机制管理文件描述符与借用它的 stream。
清理失败时，异常 cause 中的 `IncompleteRollback` 保留剩余资源及 `retry()`；
调用方应通过该 owner 重试，不自行关闭已消费的数字 FD。首次读写失败保留，
进程控制异常继续传播；重试不会关闭后来复用该数字的其他描述符。

报告的 `material_integrity=PASS` 只表示完整接收，始终
`online_executable=false`、`real_readiness=BLOCKED`、`v1_release=BLOCKED`。
因此 CLI 成功接收仍返回 exit 2，以沿用现有 release BLOCKED 约定。
原 `authorization` 资格投影原样保留，c25 及后代仍只能审计。
材料 hash 和本地允许清单不是用户批准、独立 DecisionSource 或发送能力。
后续真实运行绑定须独立核验；普通执行入口和底层客户端的凭据前拒绝继续适用。

冻结 r7 的一次性验收应在 repo 外保存源树前后快照，核对固定
`76b364052aabec12dbdf1717a33c1fa56f7edd6ab1f1c556298a178942fe0e4f` manifest，
复现 canonical `{batch, proposal}` 的 3349341 字节与旧 wire `invalid_wire_size`，
再完整读取全部 970 项。该材料及其原意见不会进入永久合成 fixture。
