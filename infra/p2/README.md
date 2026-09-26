# P2 synthetic AWS execution package

Baseline: `b7826a12144ec77e8656b01b02e49e0ce740f01e`. Scope is a new,
synthetic-only canary environment. The default product path remains
`UnconfiguredAuthority` and `approval_source_unverifiable`. No provider model
credential, real user grant, P3/P4 batch, or v1 release decision is installed.

## Execution gate

`config.example.json` must be copied to an ignored local file and completed
with **actual** three distinct account IDs, one Region, deployer and auditor
role ARNs, an A operator role, a reviewed account/Region quote, approved
currency/amount/tax basis/duration, deployment expiry, CloudWatch retention,
and C Object Lock retention. Example numbers in older P2 planning documents
are not approvals. `ctl.py` refuses placeholders, an expired window, a quote
over the approved amount, a wrong current STS role, or any mutation without
`--execute`. It does not read credential files or print an environment dump.
This candidate targets the commercial AWS partition; a GovCloud or China
Region needs a separate ARN and endpoint review before any deployment.
CloudWatch log retention must use a supported day value (1, 3, 5, 7, 14,
30, 60, 90, 120, 150, 180, or 365 within this candidate). A/B runtime
checks the approved expiry before new ISSUE, submit, invoke, Dispatch send,
or canary receipt; REVOKE and status remain available until traffic is
stopped. Runtime expiry does not shut down billable infrastructure.

Authority fixes `budget_started_at` when the anchored ISSUE activates the job.
Reserve, SEND_INTENT and Dispatch preflight use the earlier of that start plus
the proposal's `total_seconds` and the issuer's `activation_deadline`; a new
handler or role cannot reset it. Product requests precede optional auxiliary
requests, which share the product operation. `finish(product)` closes both
request roles before judge may start. A judge-only grant needs no product
operation. A possibly sent request can still settle after expiry or revocation.

Each time check first commits and independently anchors a `SUSPENDED` fence
with `stop_reason=time_check_pending`, before sampling the clock. Only the
same uninterrupted check can clear that exact revision after verifying time,
as part of the next RESERVE, SEND_INTENT, PREFLIGHT or FINISH ledger action.
That action also validates its own commit-time sample and records the exact
validated sample as the event timestamp. An invalid sample appends a stop
instead of applying the action, even if the preceding check passed.
Expiry or a clock before the original budget start appends `TIME_STOP` with
`batch_deadline` or `authority_clock_unverifiable`. A failed stop write leaves
the fence; a partial B/C commit leaves a mismatch. A lost acknowledgement after
a successful reservation/intent commit still leaves that attempt occupied.
Neither a new handler
nor a corrected clock clears these conditions. The fence event has `at=null`
because time has not yet been checked. Unresolved sends preserve the first
stop reason, and existing in-flight usage can still settle without reopening
admission or changing the original deadline. There is no worker/issuer resume
API: an independent administrator must reconcile trusted records; if continuity
cannot be proved, revoke and obtain a new independent approval for a new job.
The C event bound is 256 to accommodate these additional checks at the existing
16-request limit; request, token, duration and fee limits are unchanged.

C's Anchor uses regional S3 path-style addressing, including in `us-east-1`,
to match its exact DNS allowlist. Readback indexes Lambda functions by the
returned `FunctionArn`, so service outputs can join directly to those records.
The `dev` extra includes the AWS SDK for offline tests in the normal `dev+mcp`
CI environment; those tests intercept requests before network transmission.

An independent A admin installs the signer and operator access; B admin owns
Authority, Dispatch, worker and network; C admin owns the high-water table,
locked audit bucket and its policy. C also owns a separate P2 preflight KMS
signing key: the named C admin signs, C account root retains key management,
and A/B deployers can verify through
the C key policy plus cross-account IAM `kms:Verify` permissions installed by
their administrators. No KMS grant is used for either signing key. C's
auditor stays read-only. Each account uses a separate auditor role.
The task roles cannot deploy, assume admin roles, change IAM or routing, read
the secret, or write the anchor. Account and Organizations/SCP ownership must
be read back by the administrators; three role names in one management domain
do not establish separation.

AWS Budgets and expiry reminders do not enforce a hard spend stop. The
reviewed quote must cover all three accounts, paid interface endpoints,
Fargate, Lambda, API Gateway, DynamoDB/PITR, S3 versions/Object Lock,
CloudTrail data events, CloudWatch/Flow/DNS logs, ECR, taxes if applicable,
both A/C KMS signing keys, the synthetic B secret, and retained B/C resources
after shutdown. Include five additional interface
endpoints for same-role IAM probes. No model calls occur in P2; P4's
future model fee cap is a separate condition and is not represented by the
P2 infrastructure budget.

## Build and deployment order

All commands below run from the repository root with `uv run --frozen --extra
p2 python -m ...`. `build/p2` and the local config/state files are ignored
artifacts; keep an external hash and a new evidence path for each trial.

1. Render and lint the six templates: `python -m infra.p2.render` then
   `cfn-lint infra/p2/generated/*.json`. The Python renderer is local only.
2. Build the Lambda artifact on any host with `uv`:
   `python -m infra.p2.build --lambda-only --output build/p2-lambda-candidate`.
   For an ECS image, use a Linux Docker host and an **actual digest-pinned**
   Python 3.14 base: `python -m infra.p2.build --base-image
   python:3.14-slim@sha256:<VERIFIED_DIGEST> --output build/p2`.
   Review `manifest.json`, package hashes and the Docker image before upload.
   A Lambda-only build cannot be pushed as an ECS image. The currently frozen
   offline candidate contains only the Lambda build and cannot pass the live
   acceptance gate until the digest-pinned Docker build is completed.
3. With independently scoped deployer roles, run `ctl` phases with
   `--config <LOCAL_JSON> --state <SHARED_NON_SECRET_STATE_JSON> --execute`:
   `foundation-b`, `foundation-a`, `foundation-c`. The B foundation gives C
   the B Authority role ARN and A the B issuer queue. Each admin reads back
   the returned outputs and transfers only non-secret IDs through the shared
   state; no role in one account is granted deploy access to another.
4. Run `upload-c`, then `service-c`. This creates the compliance Object Lock
   bucket; its approved retention cannot be shortened after object writes.
   Run `upload-b`, `push-image`, then `service-b` with traffic off.
5. Run `upload-a`, then `service-a` with traffic off. B admin runs
   `allow-issuer-b` to add A's **actual** interface endpoint ID to B's API
   policy. Auditors read back all policies, routes, endpoint policies, DNS
   Firewall `FirewallFailOpen=DISABLED`, Flow/CloudTrail delivery, KMS key
   policy, C bucket lock and three actual identities before activation.
   Freeze a reviewed deployment candidate containing the exact Lambda
   manifest, source-file hashes and `deployed_image_uri` from `push-image`.
   Transfer its SHA256 separately from the deployer. `accept.py` checks the
   A/B/C Lambda `CodeSha256`, ECR digest and running task image/package
   against that candidate; an image tag alone is insufficient.
   Run all eight negative probes while A/B traffic remains off, collect fresh
   independent A/B/C readbacks and run `accept.py --candidate <REVIEWED_JSON>
   --candidate-sha256 <REVIEWED_SHA256> --state <STATE_JSON> --output
   <NEW_PREFLIGHT_JSON>` with the required config, readback and probe files.
   A failed gate writes a new FAIL receipt and remains a stop condition.
   C administrator independently reviews the PASS receipt and its input
   hashes, then runs `python -m infra.p2.sign_preflight --config <CONFIG>
   --state <STATE> --preflight-result <PREFLIGHT_JSON> --preflight-sha256
   <REVIEWED_SHA256> --output <NEW_SIGNATURE_JSON> --execute` under C's
   configured admin role. Transfer the signed receipt and its independently
   reviewed SHA256 to A/B administrators. If any prerequisite differs, stop.
6. B admin runs `activate-b`, then A admin runs `activate-a`, each with
   `--preflight-result <PREFLIGHT_JSON> --preflight-sha256 <REVIEWED_SHA256>
   --preflight-signature <C_SIGNATURE_JSON>`. Each activation calls C KMS
   `Verify`; the receipt expires after one hour and is bound to the config.
   The A operator
   uses `operator view` with `--job-id --output <FRESH_HTML>` to review the
   synthetic proposal. `operator issue` requires that page's SHA256 and an
   activation deadline; `operator revoke` is always available for an active
   synthetic grant. B admin runs `ctl run-task --mode submit`, then reads
   its stopped result with `task_result.py`; use the returned job ID for
   `--mode invoke --job-id ... --attempt-id ...`, `status`, or `finish`.
   Never re-run `invoke` after `SEND_INTENT` if the result is unknown.

`ctl` writes resource outputs to the state file. It contains no canary token;
the synthetic token is generated once at B stack creation and passed as a
NoEcho parameter. B Dispatch alone can read the resulting secret. The A
operator reviews a read-only HTML projection and emits synthetic signed SQS
events; it does not establish real user authorization.

## Real boundary acceptance

Use a new evidence directory for each run. A/B/C auditors run
`python -m infra.p2.readback --account a|b|c --config ... --output <NEW_JSON>`.
For each synthetic job, B and C auditors also supply `--job-id` and compare
B's strong-consistency grant state with C's locked high-water chain. B admin
runs task probes `ctl run-task --mode probe|probe-iam|probe-executor|probe-executor-iam`
under the actual worker and executor roles. The `-iam` modes use isolated
probe security groups with access to protected AWS endpoints. B admin runs
`ctl probe-dispatch`, `probe-dispatch-iam`, `probe-authority` and
`probe-authority-iam`, each with a fresh `--probe-output`, under the actual
Dispatch and Authority roles. The CLI saves an unexpected OPEN result before
exiting nonzero. B auditor uses `task_result.py` to read four stopped tasks,
their logs and CloudTrail `RunTask` network evidence; `dispatch_result.py`
reads four Lambda invocation results from actual logs. `accept.py` requires
all eight files: `--worker-probe`, `--worker-iam-probe`, `--executor-probe`,
`--executor-iam-probe`, `--dispatch-probe`, `--dispatch-iam-probe`,
`--authority-probe`, `--authority-iam-probe`, plus `--state`, `--candidate`,
the independently reviewed `--candidate-sha256` and a fresh `--output`.
The protected service probes must return `AWS_ACCESS_DENIED` for their tested
operations, including both A issuer and C preflight key `Sign` and B grant
`GetItem/PutItem`; a network timeout is insufficient for the isolated IAM
test network. C anchor table `UpdateItem` uses a condition that can never be
true, so even an unexpectedly authorized call cannot write; any conditional
failure is a boundary failure. B's DynamoDB endpoint policy only permits the
B grant table, so C table `AWS_ACCESS_DENIED` proves that endpoint route is
closed, **not** that the role's IAM policy denies C table writes. The receipt
records `anchor_table_route_probe=PASS_ENDPOINT_DENIAL_ONLY` and leaves
`C_anchor_table_role_IAM_direct_write` pending. An AWS AccessDenied may arise
from IAM, an endpoint/resource policy, key policy, or SCP; policy readback is
needed to attribute it.
A canary business-layer 403 means the protected application was reached and
fails the boundary. A/C auditor readback lists KMS grants; any grant on either
signing key blocks the canary preflight. A successful receipt is labeled
`PASS_SYNTHETIC_CANARY_ONLY` with `identity_probes=PASS_TESTED_PATHS_ONLY`.
It does not mean P2 acceptance passed: C anchor table IAM and S3 direct writes, IAM
role/trust changes, `ecs:RunTask`/`iam:PassRole`, task definition changes,
and VPC/SG/DNS policy mutations remain
`identity_privilege_expansion=PENDING_REAL_EVIDENCE`. The independent
administrators must provide a safe same-role test path and evidence for
those actions before declaring P2 complete. Fault scenarios likewise stay
`PENDING_REAL_EVIDENCE` until separately observed.

| Scenario | Required observed result |
| --- | --- |
| Worker/executor/Authority/Dispatch unauthorized secret, signer, grant DB, C anchor, admin role | Actual task/Lambda probe denied for each role's forbidden operations; IAM trust, inline/attached policy, permissions boundary, endpoint resource policies and SCP path independently read back. Authority's permitted grant/anchor writes are never run as negative probes. A failed network call alone does not prove IAM denial. |
| Public IPv4, IPv6, direct DNS, proxy and arbitrary S3 | Task probe denied; no IGW/NAT/default route, exact SG egress, worker ECR-layer-only S3 endpoint policy and DNS block-all read back. |
| B state tamper or rollback | On a fresh synthetic job, B admin snapshots the first failure, mutates only the B test state at fixed revision, and verifies `authority_event_unverifiable` or B/C mismatch stops all methods. Do not repair the original failed job; use a new job for later cases. |
| C high-water rollback, S3 version removal attempt, B→C partition | C admin controls fault injection. B `status/invoke` must fail closed while C chain or readback is unavailable. Preserve C Object Lock and original failure evidence. |
| Revoke versus reserve and `SEND_INTENT` | Use distinct fresh jobs and a controlled barrier. Revoke before intent yields no canary receipt; an already anchored intent may settle one in-flight request, but no next attempt. Compare B revision, C chain and canary receipt by attempt ID. |
| Lost Dispatch response or unknown usage | Inject only after anchored `SEND_INTENT`; observe `MAY_HAVE_SENT`/`SUSPENDED`, occupied request budget and no retry/new product or judge dispatch. Preserve first failure and unknown liability. |

The current offline tests cover the protocol race, unknown send, partial
commit and same-revision state tamper. They are not evidence of an AWS trust
boundary. `accept.py` cannot turn the scenario table into PASS by assertion.

## Stop and cleanup

On any unexpected allow, missing C readback, route drift, unknown send,
insufficient logs or budget overrun: A operator revokes synthetic active jobs;
A admin runs `stop-a`, B admin runs `stop-b`, then B auditor confirms no
running tasks. While B still processes delayed `REVOKE`, B admin runs
`finalize-stop-b` only after the issuer queue and DLQ are drained and the
strongly read grant ledger has no `ACTIVE` job. Preserve any unresolved
`MAY_HAVE_SENT` liability for human adjudication. If C is unavailable or
revocation cannot commit, B auditor first saves a fresh B readback and the
original failure. B admin then runs `emergency-finalize-stop-b
--auditor-readback <FILE> --first-failure <FILE>`; this stops traffic while
recording both hashes and the unresolved liability. Capture fresh A/B/C
readbacks and CloudWatch/canary evidence. `finalize-stop-b`, emergency
finalize and `delete-service-b` also require a fresh, separately supplied
`--a-auditor-readback <A_JSON>` proving A traffic is off. Do not replay a
failed attempt.
Run `delete-service-a`,
`delete-service-b`, then `delete-service-c` under each owning admin.
Finally run `delete-foundation-a`, `delete-foundation-b`,
`delete-foundation-c`; the script empties only the dedicated code buckets
and B ECR repository. A failed CREATE/UPDATE stack can use the same delete
phase while live traffic parameters are off; its cleanup record lists prior
resources, retained physical IDs and any `DELETE_FAILED` error. A successful
stack requires completed stop and live traffic readback before deletion.
`delete-service-c` requires fresh `--a-auditor-readback <A_JSON>` and
`--b-auditor-readback <B_JSON>` proving both A/B service stacks are absent;
the CLI verifies each readback's sidecar SHA256, account, role, five-minute
freshness and observation after the matching A/B `DELETED` receipt. If A/B
were never deployed, readback must follow C service creation. If the CLI
was interrupted after creating C but before persisting `P2CreatedAt`, it
fails closed; an independent administrator must recover the C creation
time and state before C deletion can proceed.
The administrators independently check the delivery channel and identity;
a local hash alone cannot prove who produced a file. Failed deletion
attempts append a new cleanup receipt without overwriting the original
service state or first failure.
Revoke the temporary deployer IAM permissions separately and
read back their removal. Do not delete unrelated IAM roles or credentials.

C's Object Lock bucket and high-water table, plus B's grant table and issuer
queue/DLQ and C's preflight signing key, use `Retain`; deleting CloudFormation does **not** remove them or
automatically stop storage/PITR charges. B/C admins must record resource IDs,
owner, approved retention end, remaining cost and later disposal. C admin
must separately schedule KMS key deletion when no longer needed and record
the pending deletion window and any continuing cost. Queue
message retention is 14 days; a retained queue does not prove a pending event
committed. Verify billing and resource closure rather than equating task stop
with charge stop.

## Least privilege check

| Principal | Needed | Forbidden |
| --- | --- | --- |
| A operator | Invoke A synthetic review/issue/revoke API | KMS `Sign` directly; B DB/Dispatch; deployment |
| A issuer | KMS `Sign` for one synthetic key; SQS `SendMessage` to B queue; B proposal API | Provider keys; B/C writes; IAM/EC2 admin |
| B executor | B private `submit/status` API | Dispatch, secret, KMS signer, DB, anchor, `iam:PassRole` |
| B worker | B private `invoke/finish/status` API | Same as executor; direct canary API |
| B Authority | B grant `GetItem/PutItem` transaction; A key `Verify`; C anchor and B Dispatch invoke; B issuer queue receive | KMS `Sign`, synthetic secret read, task/admin permissions |
| B Dispatch | Single synthetic secret read; B preflight and private canary API invoke | Grant DB write, signer, C anchor write, arbitrary outbound URL |
| C anchor | C high-water `GetItem/UpdateItem`, locked S3 anchor prefix read/write/retention | B grant DB, signer, provider |
| C preflight signer | C administrator only: KMS `Sign` on one P2 key after independent PASS review | Runtime agent, B Dispatch and A issuer cannot sign preflight |
| A/B/C deployers | Short-lived stack, code artifact, network, IAM, test/cleanup privileges in **their own** account | Cross-account admin assumption by runtime agent |
| A/B/C auditors | Read-only policies, routes, logs, stacks and synthetic B/C state | All mutations and secrets |

CloudFormation bootstrap requires `CAPABILITY_IAM`. A/B/C administrators
must scope `cloudformation:*`, `iam:CreateRole/PutRolePolicy/PassRole`, VPC,
API Gateway, Lambda, S3, DynamoDB, KMS, SQS, ECR, ECS, CloudTrail and log
operations to these named P2 stacks/resources and revoke temporary grants
after cleanup. Effective permissions also depend on resource policies,
permissions boundaries, SCPs and role trust; `accept.py` only checks a subset
and independent readback plus actual negative calls remain required.
A/B deployer roles additionally need `kms:Verify` on C's preflight key;
neither role receives `kms:Sign`. A missing Verify permission is a stop, not a
reason to bypass C's signature gate.
