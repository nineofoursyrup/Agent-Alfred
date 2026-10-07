> 当前交付范围为 [Agent-only 工程关闭](../../docs/acceptance/AGENT-ONLY-CLOSEOUT.md)。本页保留本地边界设计和历次失败／实测限制；真实安装、认证、权限／出口探针和实际隔离证明属于独立运行守卫，不再是本轮 Issue 关闭前提，本轮不执行它们。源码／mocked-C／静态通过不能改写历史原生失败或签发许可。

# 本地原生候选

该目录提供编译、打包及离线检查代码，不代表 #105 的实际 macOS 隔离、#106 的金额/运行准入或真实校准通过。主合同为 `V1-LOCAL-REAL-CALIBRATION-SPEC-r1` 和已确认的 `docs/design/local-calibration/BOUNDARY-SEATBELT-r1.md` 补充；机主、macOS 和可信宿主处于信任根内。

当前两个 runner 入口均不带 App Sandbox entitlement。可信 host 只启动固定 `AlfredRunner`；它清理 registered、bootstrap 和异常端口继承入口，再生成全新 helper。固定 C 初始化在 `PyConfig`、Python 和任何 agent 输入之前，通过 `sandbox_init` 一次性加载已封存的 deny-default `runner.sb`。外层 `sandbox-exec` 原型无法兼顾初始化所需 getter 与后续取权限制；嵌套加载也实际报错，这些首次失败保留。custom SBPL 为 Apple 已弃用且未公开支持的接口，按具体 OS build 绑定，不兼容时停止且无不受限 fallback。

策略只允许本 app 代码及必要系统库只读、当前案状态读写、fork、限定祖先目录 metadata 和无凭据的系统 sysctl；禁止新的 exec、special-port 导出和通用系统代发。根目录仅 `literal "/"` 读取，不能递归读取 home。策略和 native loader 的字节、OS build/kernel/架构均绑定 v3 manifest 的 boundary v3；每次验证拒绝变化，不把新版系统自动视为兼容。boundary v2 的选择性 Mach 拒绝不能被重新封存为当前准入。

`AlfredRunner` 只接受匿名管道 stdin/stdout，固定启动 `AlfredPython`，使用 `POSIX_SPAWN_CLOEXEC_DEFAULT` 只继承 0–2 描述符，stderr 指向 `/dev/null`，环境为空。`AlfredPython` 通过 isolated `PyConfig` 固定捆绑 stdlib、扩展库、生产依赖和 Alfred 搜索路径，禁用 site、用户 site、环境导入、字节码写入及 cwd 导入。子进程的实际拒绝及可继承能力仍须真实实测，策略文本和 codesign 本身不签发隔离结论。

helper 在 policy 前后核验单线程、registered/完整异常掩码、bootstrap 缓存及自身 Mach namespace：self task/thread 按实际 right 对应；普通 HOST 与 HOST_PRIV 分开；本地 RECEIVE 要求精确 RECEIVE 或 SEND|RECEIVE 类型、队列/send-once/通知为空、发送权布尔与本空间 SEND 一致；未知外部 send-only、send-once、其他 kernel 类型均拒绝。初始化/显式witness最多容忍一个semaphore和一个普通clock，这不是之后创建数量的连续限制；类型不证明其来源/owner，clock也不是只读对象。最终策略统一拒绝Mach MIG，保留七种自身metadata查询，并只增加 `host_get_clock_service`、`semaphore_create` 两个创建入口以支持固定libc的fork-child初始化；它们也可被受限代码调用，不能声称只允许libc调用。未列出的MIG routine和Mach trap仍由deny-default拒绝；这不是全部clock/semaphore使用路径的拒绝证明，BSD `__semwait_signal` 等路径尚未实测。外部task/privileged HOST、voucher、PID、dyld notifier、I/O取权及能力导入仍受原约束。Mach trap允许列表不变。原生birth另外要求内核保存的dyld notifier查询成功且count=0，不能只检查用户空间port names。没有销毁全部namespace，也不要求macOS不支持清空的每个槽都为NULL。

这两个MIG是明确的策略变化，替换此前“创建入口也全部拒绝”的实现保证；此前“clock/semaphore全部使用拒绝”的表述也取消。允许取得内核clock service和在已有自身task capability上创建semaphore，不增加可信主体、bootstrap或任意系统服务代发，也不增加文件/网络/exec范围。这些本地内核对象可能被受限代码使用；其实际能力及AC4/AC5的保护目标仍须当前候选实测，不能仅凭策略名认定。r18实际CHANNELS和EXTENDED fork-child在`_init_clock_port`中SIGABRT，原件保留。固定C对照中原策略child仍SIGABRT，仅增加两项后child正常退出，原完整birth/父子guard成立；这是有限诊断事实，不是产品安全证明。新源码、完整策略、安装物与许可必须重新绑定、独立核对两项的实际权限语义、完整受影响产品实测，不延续r18的安装身份或旧grant，也不把原失败改成PASS。

自身 RECEIVE 队列核验的 `mach_port_get_attributes` 先经过同名 Mach trap；有限允许表包含该 metadata trap，不能只允许 MIG fallback。公开实现的 raw trap只读 current task 的 READ/CONTROL metadata，不制造或返回 send right；整个 API 的 MIG fallback仍受已有 typed target right 限制。该依据不替代固定本机 before/after 和当前产品实测；r16 的精确 `namespace_receive_call/status53` 启动失败原件保留；r14 的首 EOF/78 与 r15 的 namespace/null 仍保持各自未知原因范围，不倒推为该查询的实测53。profile新增这一项不会自动让旧安装/许可兼容，精确源码及策略字节变化须重新绑定、独立复审和真实验收。

出生核验在可信单线程 C、policy/Python/agent 输入之前两次读取全局发送权计数，未知或额外计数拒绝。普通 RECEIVE 的计数必须为0，含本空间 SEND 时为1（不是 urefs 数）。唯一第二份例外须钉住自身 `TASK_DEBUG_CONTROL_PORT`：getter 成功、返回 SEND 已释放、与同一自有 SEND|RECEIVE 相同、严格 guarded 元数据且全局计数精确为2，以已纳入可信基的 kernel 持有一份与本空间持有一份解释；不允许任意 count2 或仅凭 flags6。非空 slot 未对应自身 receive、读取/释放失败、其它 sender、通知或状态不符均停止。这个组合是信任 macOS/kernel 的出生快照，不证明每个 sender 的 PID 或连续生命周期；policy 后不调用被拒的计数 MIG 或 debug getter，不增加许可。当前固定 OS 的有限 C 元数据/slot 组件已观察该差异，但完整 ffi 前后出生、产品及继承子进程仍须新候选实测，不能沿用旧 guard 的 PASS。

首次完整 birth 检查后，可信 C 固定链接系统 libffi，只做一次 `sizeof(ffi_closure)` 分配/释放，再重复完整 birth 检查（包括 dyld SUCCESS/count0），然后按原顺序进入完整策略、postguard、Python。NULL 立即 exit80；新增能力或未知状态仍停止，不销毁它们洗绿。该初始化不准备/执行 callback、不读取输入，也不让 worker 选择库或初始化参数。libffi 有意留存首 trampoline 表，free 不表示回收全部映射；策略后的缓存耗尽不允许重试预热、放宽 MIG 或无沙箱 fallback。系统库身份随固定 OS/SDK、原生 binary 依赖和候选字节重新绑定，不能沿用旧包或许可。

固定 C 冷启动对照已观测：未预热时策略后分配失败，固定可信预热后分配成功；两边原完整 birth/postguard 均成立，legacy/modern self remap 仍为53，七 MIG/23 trap 未增加。该原型不初始化 Python，因此不是纯 `_ctypes` 或正常产品链通过证明。历史可信 stderr 启动诊断保留 `_ctypes` 导入 MemoryError 的实际栈；它不证明所有旧 EOF 的原因。`managed_state` 的 ctypes 原子 no-replace 机制保留，正常产品启动、父子权限/出口及完整 #105 验收仍须在新精确候选上实测。

进入Python前ACCESS、DEBUG/NAME/CONTROL的self PID getter、dyld notifier export、普通HOST的I/O getter均必须实际返回 `KERN_DENIED`，所有返回right必须为NULL；dyld错误的未改变count不充当空数组证明。暂存right只释放不调用。固定OS的七种原metadata routine、两个新增libc创建routine及`syscall-mach`/`machtrap-number`均随完整策略封存；原型不能替代当前产品的安装、运行和fork实测。旧I/O getter成功、选择性策略、编译器符号拒载、框架中止及首次失败原件保留。有限的可达能力证明、当前候选真实出生/注入/出口和生产运行仍是#105必要证据。

`AlfredOwnerApproval` 是可信宿主启动的独立非沙箱 helper。它读取一个有界的 `V1-LOCAL-OWNER-AUTH` v1 帧，拒绝重复 JSON 键，验证精确 request 字节的 SHA256、nonce 和最多 300 秒期限，完整展示对象/理由/用途/摘要。只有用户选择批准且 LocalAuthentication `deviceOwnerAuthentication` 成功后才返回实际 UID、当前时间和原 challenge 的回执。取消、超时或失败没有成功事件。回执不是 grant、供应商计费证明或远端不可否认签名。

只有可信源主动生成的 challenge 与它直接启动的已验证 helper 回执可进入批准日志；没有 worker 导入任意回执的 API。同 UID 或回执内的主体字符串本身不是调用者认证。当前 helper 不独立认证任意 Python 父进程；#105 必须验证 runner 无法影响可信源、读取 challenge 或借此发起/注入批准。Keychain 按实际条目和宿主身份验明，不从策略名称泛化。历史来源认证的真实结果保存在独立证据包；构建和导入不会打开认证 UI。

在 Python/输入之前 postguard 失败时，helper 只输出一个有界的
`V1-LOCAL-NATIVE-BOUNDARY-FAILURE` v2 帧并保持 exit78：固定 check 标签、
实际 PID/PPID/UID、已有负向 getter 的原 kern status 和返回值是否非NULL。
复合 bool 检查的底层状态未知，两个原始字段写 null，不能伪造具体 rc。
namespace 记录原分支的首个失败子项；原有查询/释放调用的 status 已知时
保留该原返回码，其他谓词仍为 null。namespace 子项不返回单个 port，
`returned_nonnull` 为 null，不能伪造有/无返回 right。后续清理错误不覆盖
首个失败，不重复查询；bool wrapper 仍不输出。原 v1 失败记录保持原件。
帧不包含 port name、kernel 地址、描述、路径、批准或新 native selector；
成功路径不输出它，也不改变 guard/调用顺序。只有 failure 分支临时忽略
SIGPIPE；普通写失败仍退出78，进程终止/缺帧保持 unknown。宿主须按已封存
C/包身份独立核对失败帧，产品协议不能把它当成功或批准。

## 构建与检查

在最终集成 worktree 使用已有 CPython 3.14/development 环境，明确指定 Python prefix 和 site-packages：

```sh
PYTHONPATH=src python scripts/build_local_macos.py \
  --output /absolute/new-candidate-directory \
  --python-prefix /absolute/cpython-3.14-prefix \
  --site-packages /absolute/existing/site-packages
```

默认构建 30 个单次产品 app 和独立 `Probe.app`；`--slots 1` 仅做构建检查，不能被真实 installer/runner 准入。`--with-mcp` 仅在最终运行需要 MCP schema 验证依赖时使用。工具不下载、安装、访问 Keychain、启动 app 或调用 API。

打包依据 `pyproject.toml` 和 `uv.lock`，只复制当前环境中满足锁定版本的生产 distribution RECORD 文件，并核验 RECORD 的 SHA256。可选 AWS、pytest、Ruff、editable `.pth`、未知 site 文件和 `direct_url.json` 不进入 runner；旧 AWS 代码留在仓库。stdlib 的 site-packages 和构建用 config 目录不复制。所有非系统 Mach-O 依赖被复制并重定位到 bundle，未知依赖/RPATH 拒绝；候选用本地 ad-hoc 签名，不申请付费签名或公证。

`V1-LOCAL-MACOS-BUNDLE` v3 manifest 在 package 外封存，覆盖所有字节、权限、每案固定策略、OS/loader、30 个 app 的独立 bundle ID 与 entrypoints，以及 probe 和 owner helper。v2 属旧边界，不被新入口接受；原安装与原代码仍保留为历史。`package_id` 只在构建时产生并成为受审对象；运行时不能生成新 ID 重置许可。每个候选都要重新核对安装和精确运行对象，旧 job 不能换包续用。

`NativeBundleVerifier` 初次核验完整 inventory 与签名。之后在可信宿主生命周期内检查根目录、owner helper 和当前选中 app 的文件身份（device/inode/mode/uid/gid/size/mtime/ctime），选择下一 slot 时重新核验它的完整字节及签名。变化即拒绝，不重新封存为原候选。该缓存依赖实际 runner 不能修改受保护树；不防可信机主/kernel 伪造元数据。最终安装与实际进程身份仍由 #105 核验。

## 单次容器与保留历史

`prepare_runner_slot(..., slot_index, protected_root, job_id, operation_id, verifier=...)` 由可信 driver 根据冻结产品操作顺序选择，worker 无权选 slot。进入 context 时先取得宿主侧独占 lease，将 bundle ID 对应的 spent marker 原子创建并 fsync，然后才创建新容器的本案目录。已经存在的容器拒绝；失败也消耗 slot。不会删除历史或复用目录，不以新 job、重新封存 manifest 或修改时钟绕过。

本案目录固定在机主真实 home 的 `Library/Containers/<bundle-id>/Data/Library/Application Support/Alfred/current-case`；保留原命名布局，但新实现不靠 App Sandbox 自动容器授权，而由封存 profile 限定这个精确目录。不采纳 `HOME` 环境变量，也不把任意 cwd 当作沙箱权限。实际目录准备和启动属于 #105；源码检查不代表已执行。

`slot.launch()` 验选中 app/策略/OS/loader，以固定 loader 启动并返回新建 stdin/stdout 管道。关闭先关 IPC，再尽力终止原 process group 并等待；失败保留原异常、可重试 cleanup 和 lease。`killpg` 不证明杀死已 `setsid` 的子孙。30 份不同的目录策略用于阻止旧进程看到未来案例；不能把清理当作可复用容器的证明。原 job 继续使用原候选/原账，不自动获得新 slot 或新 grant。产品原始结果须由宿主封存到 runner 不可写的 EvidenceStore；保留容器不等于将它当可信审计库。

## 固定的实际 probe 入口

`prepare_native_probe(..., probe_id=...)` 仅由显式授权的机主验收流程使用，消耗独立 probe 容器，不占 30 案。它使用相同 launcher、Python、库和策略生成器，只绑定不同的自身代码/状态路径。原生 bootstrap 只在 `.probe` app 中接受 `V1-LOCAL-OS-PROBE`；产品 app 不能选择 probe，probe 不能执行产品。

probe 输入是闭合结构：随机 nonce、专用 `alfred-local-probe-sentinels-<32hex>` 目录、六个固定 sentinel 文件的预期 SHA256、两个已授权的本机非计费监听端口。它验证文件 read/write、IPv4/IPv6 loopback TCP，并通过 fork 子进程重复检查；不重新 exec。子进程关闭 host 控制管道，以独立有界 JSON 管道返回，父进程核对实际 PID、结构、退出和回收。为避免在系统框架已启动线程后 fork，子检查先于父检查。输出只有状态、errno、hash、时间与 PID，不回传内容。只有 `EACCES/EPERM` 记为拒绝；缺文件、连接拒绝、超时或不支持都未验证。成功读写/连接是失败，意外写入保留原证据，不修复后重报。

probe 本身不自动产生隔离 PASS：宿主要证明 sentinel 存在及监听器有效、核对实际进程/签名/entitlements、检查原件和外侧观测。probe 不调用认证 UI，也不探测真实 provider。不得拿 fake socket/临时文件测试作为这些实际结果。

## 扩展的无计费探针

`V1-LOCAL-OS-PROBE-EXTENDED` v1 是单独闭合协议，旧 probe 不改标。产品 app 仍不能选择任何 probe。它仅接受明确 target 文件、非特权 loopback 端口和可选的无价值专用 Keychain 项，不接受 command、URL、header 或 provider key 值。文件读/写权限测试均只尝试打开并关闭，不截断、写入或读取内容；尤其真实生产凭据即使隔离意外放行也不读首字节。原生 FD 197/198 的实际注入由宿主配对，probe 检查它们是否已关闭。

`candidate/read` 仅当目标精确等于当前 Probe.app 中的捆绑 `native_probe.py` 才是代码可读正向对照，记为 `expected_allowed`；相同路径的写打开必须拒绝。其它受保护目标成功打开仍是失败。r2 首次实测中该自身源码读打开被旧协议记为 `unexpected_allowed`，原结果保留；新标记只用于后续新候选，不能改写旧证据。

固定动作补充路径穿越/symlink、金标/控制/代码/他案的读及写打开、TCP/UDP IPv4/IPv6、公开 resolver 地址的无用户数据探测、`example.com` 的系统解析、loopback 代理端口、setuid，以及 LaunchServices 打开宿主的无数据 loopback 页面。系统代发若成功可能打开一个该任务的浏览器页，宿主须观测实际请求并关闭本任务页面。父/继承子进程各自记录；不能因普通 DNS/系统错误就记为权限拒绝。

Keychain 未指定时明确 `not_run`；设置后也只允许命名为 `alfred-probe.keychain-db` 的任务测试库及绑定 nonce 的测试 service/account。它不枚举默认 Keychain，不改变搜索列表，不触发 Keychain 认证 UI；测试条目不替代真实 key 的实际策略检查。只有已知的缺 entitlement/权限状态属于拒绝；不存在、交互不可用和其它 OSStatus 保持 unverified。

宿主须提供存在性/权限、有效监听器、实际进程/安装身份及接收侧证据。探针始终输出 `REQUIRES_HOST_CORROBORATION`，不自行完成 #105、不产生运行许可。跨案存活、停止/恢复/磁盘/时钟故障、真实来源及生产凭据仍单独验收。

`V1-LOCAL-OS-PROBE-SYSTEM-DIRECT` v1 是额外固定探针模式，每个新原生 Probe 进程仅做一次 `browser` 或 `dns` 检查。请求只包含 nonce、固定动作及无价值 canary 的本机端口；浏览器 URL 和 DNS 名称由代码固定，不接收任意 URL/hostname，也不接收凭据。LaunchServices/libdispatch 在 fork 后的子进程可能崩溃，故此模式在新进程中直接调用系统框架，不把子进程异常当作权限拒绝。宿主仍须验证 canary 收发、进程退出、系统日志及相同目标正向对照；返回 `gaierror`、崩溃、超时或未命中监听器均不能单独证明无代发出口。每次模式消耗独立 probe 容器，不占 30 个产品槽。
The fixed `V1-LOCAL-OS-PROBE-SYSTEM-DIRECT` `capabilities` action reads only the
current task's bootstrap/access/debug-control getters and cached bootstrap
presence. It records kernel return codes and presence booleans without exporting
Mach names. Only `KERN_DENIED` with no returned right is marked denied. Null
rights and other errors are not permission proof. Host positive controls,
startup inheritance verification and the rest of #105 are still required;
historical LaunchServices crashes remain unverified.

The closed `capability_family` action forks before the fixed libSystem queries,
then reads the same getters in parent and child. It also queries the ordinary
HOST's `host_get_io_main` path, immediately releasing any returned rights, and
attempts creation of IPv4/IPv6 TCP/UDP and Unix stream/datagram sockets without
connecting or sending. Descriptor 197/198 checks contain metadata only. The
child frame has a fixed schema and actual PID/parent binding; neither arbitrary
targets nor contents are accepted. A successful getter remains a raw success,
and release errors cannot become permission denials. The trusted host must
corroborate these facts and bind any reused observations to the current loader,
policy and fixed macOS build before claiming a combined capability boundary.
If a fork snapshot fails, its closed metadata frame retains error types, errno,
cause/context/group links and incomplete rollback errors before the owned child
exits. The family is explicitly incomplete; it does not report denial or
resource recovery. Missing frames remain unknown. Socket creation uses the
native `_socket.socket` (`SocketType`) primitive rather than a Python factory.
Socket creation alone is not connection or send permission. Its raw success
must remain visible; it does not by itself establish a communication bypass.

The fixed `V1-LOCAL-OS-PROBE-CHANNELS` supplementary Probe-only handler pairs
Unix stream connect/send and datagram sendto with two task-owned, fixed-name
canaries. It reuses the original owned fork before DNS or system frameworks.
The native `_alfred_boundary_witness.witness()` builtin takes no arguments and
returns only seven boolean observations from the unchanged guard after Python
startup and in each inherited child. False, missing or partial observations are
not permission proof. Port names, kernel addresses and descriptions never leave
C. Products cannot select the handler; no new service or policy grant is added.
Bounded frames retain family results, DNS and before/after CoreFoundation,
LaunchServices, CFURL creation and LSOpen stages before later aborts. Their
nonce/request digest/sequence/PID metadata still needs host corroboration. A
last stage alone does not identify a particular MIG call or prove all channels.
