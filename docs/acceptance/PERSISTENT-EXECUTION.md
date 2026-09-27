# Controlled execution persistence and recovery

This is the #102 engineering contract over #101's exact grant, plan, clock,
`SEND_INTENT`, monetary reservation and settlement. Local demonstrations are
synthetic. They do not establish real identity, isolation, deployed cloud
capacity, billing terms, USD25 enforcement, quality or release authority.

## Installed service and storage

`controlled_persistence.PersistentControlledAuthority` inherits
`ControlledAuthority`, including its phase and checkpoint policy. Its constructor
takes the same trusted-host configuration. The worker still receives only the
bounded dispatch interface; it cannot select storage, register a payload, alter
the phase, or access credentials.

Backends implement `controlled.persistence.DurableExecutionStore` and
`DurableExecutionAnchor`:

- `SQLiteExecutionStore(path)` and `SQLiteExecutionAnchor(other_path)` are durable
  **local fixtures**. They explicitly reject combination with `InstalledRuntime`.
  Two files do not prove independent administration or executor-unwritable storage.
- `controlled.aws_persistence.AwsExecutionStore` and `AwsExecutionAnchor` accept
  separately installed `dynamodb`, `s3`, `table`, `bucket`, and `retention_days`.
  Constructors create no SDK session/client, discover no credentials and make no
  cloud call. The real host supplies fixed clients and authenticated B/C service
  ownership. Worker input must never supply those arguments.

Each Dynamo table has string `PK` and `SK`. Each state/event/Attempt is a separate
document with its canonical SHA256. State, event, Attempt and first consumption
of a prepared payload commit in one conditional transaction. All reads are
strongly consistent; queries follow every pagination cursor. Objects live in
content-addressed S3 keys, with insert-only writes, a pinned version and checked
COMPLIANCE retention. C separately appends each anchor to its own table/bucket.
A partial B/C or C table/object outcome remains unverifiable, never silently
repaired into an active grant.

Committed event/anchor rows must be immutable under installed IAM. New handlers
verify complete historical chains; a running handler verifies its own CAS
appends. Complete audit readback always scans and verifies the full chain and
S3 version inventory. Real policy/role probes, independent governance, object
retention, SDK configuration, service authentication and cloud throughput still
require #105 evidence. The adapter code and pure SDK-stub contract tests are
engineering evidence only.

## Recovery, settlement and first failure

`handoff(job_id, principal=controller)` releases a healthy handler with no
in-flight Attempt or prepared request. It commits `HANDLER_RELEASED` into B/C
and returns one opaque capability; its digest, not the capability, is persisted.
The old handler immediately loses execution ownership. The replacement calls
`recover(job_id, principal=controller, handoff=capability, material_output=None)`;
one CAS commits `HANDLER_ACCEPTED` and consumes that capability. A replay is
rejected. The original grant, plan, start, counts and money remain unchanged.

Every new Authority instance has a new handler epoch. A replacement without a
verified clean handoff permanently stops an ACTIVE job with
`handler_recovery_unverifiable`; existing terminal states and first stops remain.
Direct prepare, invoke, finish and phase calls also require current ownership.
Status, original settlement and receipt readback are available for audit, and
status exposes `handler_access=AUDIT_SETTLEMENT_ONLY` for a non-owner.

Recovery reads both full chains without re-submitting a grant. An interrupted
time fence stays stopped. A recovered in-flight
request becomes `MAY_HAVE_SENT`, or `CANCELLED_BEFORE_SEND` when its original ledger
proves the final send latch was never consumed. Both retain the original worst
liability. Recovery never calls the model.

`restore_materials(job_id, new_output, principal=controller)` rebuilds a handler's
local cache from the same protected material reference. It verifies the complete
manifest, exact binding and approved ancestor before updating the cache pointer.
Once diagnostics are sealed, their derived EvidenceStore remains at its original
stable path because the checkpoint binds that exact evidence path and digest.
Restore validates that diagnostic batch, its full ancestry and any published
first-results child before changing the material-cache pointer. A successful
restore therefore keeps reporting and subsequent phases on the same immutable
derived evidence. The original derived store must remain available: rebuilding
the material cache does not reconstruct missing diagnostic or product evidence.
Missing or corrupt derived evidence suspends the job without adopting the new
cache pointer or claiming successful recovery.
The original job, source request, plan, clock and money remain. It supports a new
handler with a different local cache path. Loss of the authoritative material
object/source fails closed; reconstructing an already stopped cache never
reactivates it. Local simulator source files cannot restore an issuer: process
tests relay fresh reads to the **same live** source instead of copying its key or
inventing another approval.

Integrity failures are recorded as irreversible fault observations in each
available domain. A restored B store is still blocked by C's observation, and a
restored C connection is still blocked by B's observation. When both domains are
unavailable there is no claim that either saved the failure. A live handler keeps
its stop witness and cannot issue a clean handoff. If that process disappears,
the replacement's missing handoff prevents the old apparently healthy disk state
from becoming execution permission. Restoring both
domains from mutually rolled-back local files cannot prove independent trust;
real cross-domain retention is a #105 requirement.

All Authority and diagnostic/control driver persistence operations use
`persistence_call(job_id, operation, ...)`. The scope comes from an authenticated
controller or endpoint, never from object contents. Controller identity and
request validation precede IO; rejected control IDs, unknown jobs and ordinary CAS
losses do not create integrity faults. Nested guards retain the same exception
once. `read_object(reference, job_id=...)` binds execution reads; an unscoped
legacy audit read does not infer job ownership or confer permission.

Fault observations retain a causal first-fault witness across alternating domain
outages. Neither wall-clock order nor concatenation of B and C decides the first
cause. If a replacement could not observe a prior isolated failure, distinct
first-fault candidates remain visible and the reason is
`fault_order_unverifiable`. Later observations cannot overwrite a known first stop.

`settle_attempt(job_id, attempt_id, response_ref, principal=controller)` verifies
the original response and trusted billing observation. A repeated settlement
returns its original result without a second charge. A different response cannot
replace original raw evidence. Unknown response/usage or unverifiable settlement
retains debt, with no manual zeroing, automatic refund or model retry. Original
errors remain after a later verified charge settles. Post-stop settlement cannot
reopen dispatch.

`record_observation` appends failure, blocker, retained-resource and cleanup facts.
`stop_report` includes first-stop and subsequent stop events, original errors,
current readback blockers, spent amount, pending liability and resource facts.
It distinguishes reservation, confirmed cancellation, possibly sent and settled
requests. An unreadable Attempt inventory produces `send_accounting=null`, never
zero sends. It can show locally readable evidence when the independent anchor is
unavailable, but marks that evidence **unverified**. Stopping a job never implies
resource deletion or final billing reconciliation.

## Control transport

`controlled.control` defines a new `V1-CONTROLLED-CONTROL` envelope, separate from
the unchanged P2 private canary's 8-second timeout, 256 KiB wire and 256 revisions.
An envelope binds request ID, job, operation, Attempt ID, prepared/payload digests,
per-Attempt timeout and absolute deadline. Messages are bounded to 64 KiB;
potentially large results use digest-verified immutable references.

`ControlService.call(envelope, principal=authenticated_worker)` durably claims a
control request before invoking its original action. A retry of the same envelope
only returns that claim/result; a changed envelope under the same ID is rejected.
Completed receipts remain readable after STOP/deadline. If a handler dies before
writing its receipt, `reconcile(envelope, principal=controller)` can construct the
receipt only from the same original terminal Attempt/operation. It never invokes
the model. An unresolved request stays `PENDING`.

`ControlClient` and `HTTPControlTransport` allow up to the remaining 120-second
Attempt plus a 30-second control-response margin. The model deadline itself is
never increased. Timeout, connection loss, server 5xx and client cancellation
retain an explicit uncertain-delivery identity; none proves zero cost or retries
the model. Once a matching COMPLETED receipt arrives, a later result-read failure
raises `ControlResultUnavailable` with the same request ID, receipt,
`delivery=COMPLETED`, and `result_state=UNREAD`. `read_completed(envelope, receipt)`
performs an explicit read-only retry without another POST or model invocation.
A real control server must derive principal from authentication and
scope result access to the authenticated job. The HTTP client installed by the
host authenticates the control channel; it never receives the model credential.
`HTTPControlTransport.retry_cleanup()` resumes release of the original response
and underlying stream after a close failure or interruption. Ownership is
established before `client.send`; HTTPX's `is_closed` flag cannot retire an
unreleased stream. Cleanup retries do not issue another control request.

The final time sample, after source/quote/store reads, validates the original
price and identity expiration, Attempt deadline and original batch clock before
privileged IO. A transient clock rollback or a quote expiring during readback
permanently suspends the job; a later good time sample cannot clear the stop.

## Capacity and offline demonstration

SQLite backends accept a construction rollback owner before opening a connection.
The capacity and replay factories transfer their store, anchor and runtime
cleanup as one aggregate across the factory return boundary. Demonstration entry
points retain that aggregate through construction, submit and handler replacement
failures. Cleanup waits for runtime resources before closing the two persistence
backends; incomplete release retains retryable progress in `IncompleteRollback`.

The ledger has 16384 event slots, separate 256 KiB state and 64 KiB event/item
bounds, 8192 immutable objects of at most 8 MiB, and 4096 control requests. These
are new protocol bounds, not a change to the P2 canary constants. Exhaustion
fails closed without truncation or a new job.

The normal #101 Attempt produces 14 anchored events. This demonstration adds two
control events per Attempt, all 156 operation finishes, four phase transitions,
activation, two clean-handoff events, final stop and four audit observations:
**9544 events for 576 sends**. The earlier 9542-event measurement is historical
evidence for the protocol before explicit clean handoff.
The 576 limit still shares Flash480 / Pro96, each product case16, concurrency1,
the original10800 seconds and one money ledger. A 3600-second synthetic user wait
and a handler reconstruction at Attempt288 preserve that same window.

Run against a new output directory from the repository root:

```sh
.venv/bin/python -m agent_alfred.evals.acceptance.controlled.capacity \
  --output /absolute/new/synthetic-capacity-output
```

The signed `synthetic_capacity` mode permits generic phase fixtures only with
exact `SyntheticRuntime` and synthetic materials. Authorized/real-material
business execution still requires the full diagnostic/checkpoint driver. The
command emits `candidate.json`, `result.json`, the complete `events.json` and
`anchors.json`, and retained fixture databases/objects. The result records actual
object/message sizes and all event kinds. Its `offline_engineering=PASS` refers
only to that persistence/control demonstration. Real cloud capacity remains
`NOT_VERIFIED`; real readiness and release remain `BLOCKED`.
