# Controlled calibration execution contract, version 1

`controlled_execution.ControlledAuthority` is the trusted service implementation
of `AuthorityDispatch`. Default construction rejects before model credentials or
client construction. Existing `runner`, `online_judge`, `SimulationSession`,
`UnconfiguredAuthority`, and private P2 canary guards retain their old contracts.
This module does not install itself, register an environment switch, or add a
CLI source/factory override.

## Material and execution identity

`controlled.contract.execution_plan()` binds the complete #99 material reference
and #100 `material_binding`, worker/controller identities, object-specific
operations, exact pricing terms, limits and execution mode. `operation_for()`
constructs an operation from an actual frozen case or judge-test. An operation ID
is the worker's selector; kind, phase, case, model and instance come from that
plan. Pro instances are distinct. `initial_budget(plan)` provides the exact
budget object whose digest must be in the #100 run decision.

The material candidate and runtime candidate are separate: the original binding
and `material_candidate_id` retain the approved package, while `runtime_candidate`
contains the new candidate manifest. `candidate_id` and `candidate_change` bind
and expose the before/after identities. The fresh run decision covers the whole
plan digest, including the new code. Real installation verifies the loaded
package against `runtime_candidate`; an old material approval cannot authorize
new execution code. A final integration must capture its own final candidate,
then issue its separately authenticated exact run decision.

`submit_job({job_id, material_ref, plan, run_source_ref, approved_batch_id})`
receives the complete package into a fresh output namespace using #99, checks the
full #100 ancestry, and freshly reads material, dispute and run decisions. For a
real installed source, exact summary/dispute events may be read from the original
source even when an approval-era batch contains no event snapshot. They are
validated against the existing schema4 contracts and read again by source ref;
the original batch is never amended. Individual source-independence scope events
remain required. No local hash or report creates a grant.

## Controller and worker boundaries

The authenticated controller calls
`prepare_request(job_id, operation_id, ModelRequest, principal=..., data_scope_sha256=...)`
after constructing the allowed projection. #103 uses `test_input`/`blind_input`;
#104 constructs only the current case's approved Host input and local tool
results. The controller identity is supplied by the trusted service's identity
adapter, never by a worker request body. It is a separate installed principal.
The returned immutable prepared object binds the actual complete wire payload,
including system, messages, tool schemas, model settings, object, instance,
conversation and worker. Its digest is anchored by `BIND_PAYLOAD`. Only one
prepared request can be pending; only its anchored reference can be invoked.

Give the worker `WorkerAuthorityDispatch` or the one-shot `authority.client(...)`
ModelClient. These facades expose no payload registration, phase mutation, store,
credential, factory or send ticket. Changing any actual payload field, endpoint,
conversation, operation ID or prepared reference fails before privileged IO.
Python encapsulation is not an OS/IAM sandbox; #105 must verify process/role
separation and worker egress denial. The raw service object is trusted-side code,
not a worker API. Remote identity middleware must authenticate the principal
before dispatching controller methods.

The wire is always one `POST https://api.deepseek.com/chat/completions`, Flash
20000/8192 input/output bounds or Pro 24000/16384, thinking disabled, Pro
`json_object`, stream false, and zero application/transport retries, redirects,
proxies or fallback. No temperature/top_p/seed/effort parameters are added. The
exact serialized bytes are both hashed and sent. Complete-token proofs are
separate from size limits and billing; no character-to-token estimator grants
permission.

## Ledger, money and stopping

The store protocol is in `controlled/store.py`: `get`, atomic CAS
`put(state,event,old_revision,old_digest,attempt=None)`, verified attempt reads,
`list_attempts`, and immutable digest-verified object `put_object/get_object`.
Its transaction includes counts, per-case and operation quota, outstanding
liability and the Attempt row. The anchor uses the existing P2
`read(job_id)` / `commit({job_id,revision,previous_digest,event_digest})` shape.
Partial store/anchor writes fail closed. Memory implementations are explicit
fixtures; #102 supplies durable recovery and bounded capacity on this protocol.

State has bounded counters and references; payloads, full material receipts,
responses, quotes, settlements and phase evidence are separate objects. Attempt
rows are bound into events; the active row is also covered by anchored state.
An ordinary successful Attempt takes 14 events: bind 2, reserve 2, intent 2,
credential/client/send preflights 6, response receive 1, settlement 1. Activation
is 1; finish is 2 per operation; each phase transition is 2. Failure/revocation
adds events. #102 must size the actual full sequence, not merely 576 rows.

Amounts use integer USD picodollars (`10**12` units/USD). Terms cover complete
input/output, request/other charges, cache/reasoning, tax, FX, quantum, effective
window and source evidence. The exact rational amount is rounded upwards to the
billing quantum. The same atomic reservation enforces Flash 480, each case's
main/auxiliary combined 16, Pro 96, total 576, concurrency 1 and USD25 maximum.
Unknown or drifting terms/token proof reject before credentials. A signed real
billing bound and independently verified enforcement are still #105 evidence.

Every permission-bearing action anchors `TIME_CHECK` before sampling time or
reading a source. It clears the fence only with its action, after validating the
action's own final sample. The original 10800-second start never changes; product
case windows are shared 900 seconds, Attempt waits are at most 120 seconds and
shorter remaining windows win. Clock recovery cannot reactivate stopped state.
A deadline cannot prove a request was not sent. A bounded network thread may
finish cleaning up after the caller timed out; its response is not retried and
the full liability remains until a separately verified settlement.

The actual sender runs the final send preflight after thread scheduling and HTTP
request construction, immediately before admitting the underlying transport. It
consumes a one-shot `SENDING` latch and clamps the HTTP timeout to the remaining
absolute Attempt and batch deadlines. Caller timeout or interruption atomically
cancels a pending first send; a delayed worker cannot dispatch after that
cancellation. Pending cancellation moves potentially blocking close to a cleanup
worker; it never performs that close synchronously after the caller's deadline.
If even the cleanup worker cannot start, the same reachable owner retains the
client for explicit `retry_cleanup()`. Startup ownership remains protected
across process-control interruption. Close serialization follows the native
execution state of a retained one-shot generator: exceptional unwind releases
its running state without a Python flag-clearing step. Concurrent cleanup cannot
reenter a live close; an inactive failed attempt retains one retry successor.
`OwnedResource` and optional `CloseCompletion` facts preserve confirmed effects,
so a completed non-repeatable leaf is not closed again after interruption.
Withdrawal before the send boundary produces zero network sends.
An already admitted request may still return late: its cleanup owner remains
reachable, its liability stays unknown, and the late response neither retries
the request nor settles the ledger automatically. In-flight settlement after withdrawal
preserves the first stop reason. Received raw response is committed before
parsing/billing readback. Unknown usage, lost response, unverified settlement and
cancelled reservations are not refunded. Known billing settlement can release
excess reservation even for an invalid business response, which remains aborted
and closes new dispatch. If an attested bound is contradicted by actual billing,
the ledger preserves the actual charge and `billing_bound_violated`; it cannot
honestly claim that external billing respected the cap.

## Phase extension and immutable replay

`finish(job_id, operation_id)` is one-way. `phase_event` is a trusted controller
primitive committing `diagnostic -> checkpoint -> product -> grading -> closed`
into this same job. #103 must validate the complete 18+18 set and exact live
checkpoint permission before calling it; #104 owns the complete product plan.
It never resets counts, money, instance identity or the original clock. The real
readiness attestation includes candidate-bound `phases` and `capacity` evidence,
so partial local implementation is insufficient to activate real requests.

`execution_mode="synthetic_replay"` is a distinct fixture contract. It can replay
the complete unchanged frozen r7 package, including `simulation=False` and its
entire original parent chain, only through an exact `SyntheticRuntime` with
`SimulationAuthority`, `MockTransport` and `SyntheticCredentials`. Its synthetic
run event authorizes that mock replay only; it does not pass real material/source
admission. Status retains `SOURCE_UNVERIFIABLE_AUDIT_ONLY`, synthetic mode,
`online_executable=False` and `release_eligible=False`. Record the integration
candidate separately via `runtime_candidate`; never delete parent, relabel old
material as synthetic, or put synthetic execution events into the real package.

## Explicit real installation, not deployment evidence

`controlled.runtime.install_runtime(path)` accepts only a root-owned file and
root-owned, non-writable ancestors. It pins an AWS region, authenticated runtime
principal, independent source Lambda ARN, KMS signing key ARN, secret ARN and
deployment ID. There is no API accepting an arbitrary source/client factory.
`InstalledRuntime` cannot be constructed directly. AWS SDK retries are disabled;
model credentials are fetched only inside Dispatch after live preflight.
SDK response owners are registered before invoking the SDK. Returned streaming
bodies stay owned across caller-storage interruptions and failed closes.
`retry_cleanup()` reports incomplete while the SDK call or body consumer is still
active; after that scope finishes, it retries release of the same resource without
another SDK call. It cannot retire an empty holder awaiting a late response.

The fixed source service receives `V1-CONTROLLED-READBACK` challenges containing
purpose, deployment ID, nonce and the exact request. It returns `{body,signature}`;
body is `{challenge_sha256,at,expires_at,payload}`. Freshness is at most 60 seconds
and the signature is KMS `RSASSA_PSS_SHA_256` over SHA256 of canonical body.
Purposes are `decision`, `scoped_decision`, `runtime`, `complete_payload_quote`,
and `settlement`; request/response shapes are defined in `controlled/runtime.py`.
Readbacks must come from independently authenticated actual evidence, never
worker assertions or synthetic fixture output. Runtime proof separately binds
source, isolation, hard cap, deployment, capacity, phases, billing and model
identity evidence. Resolved Flash and Pro identities must differ and be fresh.

These are installable engineering adapters, not verified accounts, signatures,
provider identity, prices, infrastructure cost authorization or network policy.
No real adapter is installed or invoked by offline tests. #105/#106 own separate
authorized deployment/readback and actual run evidence. Status is an observation,
not a reusable permission or release result.
