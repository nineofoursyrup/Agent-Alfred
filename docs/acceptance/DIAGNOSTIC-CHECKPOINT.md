# Controlled diagnostics and checkpoint

`controlled_diagnostics.DiagnosticDriver` implements the judge-only stage of the
versioned controlled job. It uses the original `ControlledAuthority`, execution
store, independent anchor, run decision, activation time and money ledger.
`#102` storage implementations can replace the memory fixture store without a
new job or allowance. This controller is trusted infrastructure; workers receive
only their prepared one-shot `ControlledModelClient`.

```python
from agent_alfred.evals.acceptance.controlled_diagnostics import (
    DiagnosticDriver, diagnostic_operations,
)

# Include these 36 operations in the original signed execution_plan, alongside
# any later product/grade operations. Each Pro operation has a distinct instance.
operations = diagnostic_operations(batch)
# Submit the protected full material reference through ControlledAuthority first.
driver = DiagnosticDriver(authority, job_id, principal=authenticated_controller)
report = driver.run()  # 18 tests, then 18 blind reviews; stops at checkpoint
context = driver.checkpoint(adjudication_refs=trusted_dispute_source_refs)
# The source authenticates context["request"] as an exact separate human event.
status = driver.continue_with(
    checkpoint_source_ref, adjudication_refs=trusted_dispute_source_refs,
)
# phase=product is a continuation boundary. The driver has sent zero product calls.
```

Ordinary `authorized` plans and real-material `synthetic_replay` start in
`diagnostic`. A complete transition requires 18 frozen test objects, one original
and one independent review operation per object, all 36 settled Attempts, closed
diagnostic operations, anchored raw seals and local comparisons, and a verified
new EvidenceStore package. An arbitrary controller-supplied `all_pass` object
cannot advance the stage. Finishing a diagnostic operation is irreversible.
Calling `run()` after completion reads the frozen report and does not resample.

The actual full outbound wire is checked at admission. `test_input` is the only
original projection; `blind_input` is the only review projection. Expected labels,
failure modes, kind/answer encoding, family identifiers and paired answers remain
outside both payloads. Each request uses its signed independent instance as its
conversation identity. All originals finish before the first blind review.
Every raw review is anchored by `DIAGNOSTIC_RAW_SEAL` before `LOCAL_COMPARISON`
can access the original opinion. Both records retain the Attempt, prepared wire,
response, instance, times and ledger revision. Local comparison adds no model IO.

Strict judge parsing and `parse_review` retain empty output, duplicate keys,
invalid JSON, missing/wrong citations and original raw text. Malformed review text
cannot be rewritten into a valid schema4 review: it remains in its exact raw slot
with its error and a missing valid review. Valid records reuse `make_review` and
schema4 disputes. The conservative comparison verifies syntax and label equality;
it does not claim semantic entailment from a matching label or resolvable pointer.
Blind `unknown`/`unsupported` opinions remain unchanged. A blind `supported`
assertion is preserved in the raw opinion, while the comparison remains `unknown`
unless a later implementation supplies independently justified deterministic
relations. Current comparison never promotes semantic support to `supported`.

The complete report has all 18 result/review/comparison slots, raw opinions,
transport records, parse errors, original material disputes/summaries, required
new diagnostic disputes, exact remaining money/Attempt limits and original
10800-second deadline. Product sample count is zero and `admission_threshold` is
`null`. The canonical report is anchored in `phase_facts["diagnostic"]`. Valid
schema4 records are appended with `EvidenceStore.revise` into
`<received job>/evidence/<job_id>-diagnostics/batch.json`; the original parent,
material content, previous opinions and summaries remain unchanged. Invalid raw
slots live in the immutable execution object store and report. Partial stopped
runs expose their attempted transport rows and missing slots without inventing
outputs or another allowance.

`checkpoint()` returns a #100 `checkpoint_request`, separately binding the job,
original run event, exact diagnostic summary, each required adjudication and the
remaining ledger. Adjudications are read from the current source, bind one exact
diagnostic dispute and manifest, use the installed subject, have valid citations,
and cannot predate the dispute. Confirmed violations produce `FAIL` even alongside
missing evidence. Required pending/insufficient decisions or malformed slots block
continuation. Decisions do not repair original output or convert uncertainty into
semantic evidence. `judge_quality_not_approved` remains visible; there is no
circular requirement for the future full product/calibration summary.

`continue_with()` verifies the fresh exact checkpoint event and each required
adjudication. Wrong job/summary/run/remaining scope, future or premature decisions,
rejection, revoked or unavailable source, and expiration all leave product sends
at zero. The saved checkpoint and adjudication source references are read again
by `_current_permission` for every later action and privileged Dispatch preflight.
Source loss or withdrawal after continuation closes new dispatch. Waiting consumes
the original deadline; counts, cap, spending, liabilities and finished operations
are never reset. Product results, numeric policy and release have no approval here.

## Explicit fixture separation

`execution_mode="synthetic_capacity"` is a generic protocol fixture mode for
single-Attempt and #102 capacity tests. Only exact `SyntheticRuntime` with original
`binding.simulation=True` accepts it. It may use generic phase facts and initial
phases, and cannot create diagnostic evidence with this driver. Real materials,
`InstalledRuntime`, and `synthetic_replay` cannot use this mode to skip the stage
policy. Ordinary entry points and the real runtime remain closed by default.

For unchanged frozen r7, use `execution_mode="synthetic_replay"`, retaining
`simulation=False`, the complete ancestry and exact protected package. The
separate mock source may issue only explicit synthetic replay/checkpoint events.
Synthetic adjudications apply only to newly generated mock diagnostic disputes;
the original source disputes are retained and never bulk-dismissed. Reports keep
`SOURCE_UNVERIFIABLE_AUDIT_ONLY`, the exact replay mode, real source/quality
`BLOCKED`, zero product samples, and `online_executable=False` /
`release_eligible=False`. A derived schema4 package must travel with its linked
execution report; its unchanged material simulation flag is not proof of a real
execution. Fixture continuation demonstrates protocol only. #104 must preserve
these distinctions while exercising product/grade stages separately, using its
final integrated runtime candidate and the same original ledger/clock.

Focused checks: `python -m pytest -q
src/agent_alfred/evals/deterministic/test_controlled_diagnostics.py` plus affected
controlled/source tests. All model transport is `httpx.MockTransport`; these checks
supply no real identity, authorization, hard-cap, model-quality or release evidence.
