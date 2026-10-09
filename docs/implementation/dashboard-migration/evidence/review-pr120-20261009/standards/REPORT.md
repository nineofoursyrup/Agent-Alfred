# PR #120 Standards independent review

Candidate: base `22c8720e1ec874fd012cc79b086cd8aace4c79b6`, head `11dcc66f18ceb264ade1c1bcc1b48287dc4f1516`, tree `41adbb9ad2479d16228c68f8c228882c6cd6ae91`. Worktree `/Users/nineofour/.codex/worktrees/dashboard-integration/Agent-Alfred` clean before and after review. No repository or GitHub mutations.

Scope: the supplied 397-file three-dot diff. Direct review concentrated on new shared HTTP reads, RuntimeHost/recording/reply/source-location integration, accounting and settings changes, shell/MainBar/source ownership, page migrations, corresponding tests, assets and installed-package/lifecycle scripts. Applicable domain docs and ADRs read directly; archived review conclusions were not used to establish correctness. No full-suite rerun; the only browser execution was the isolated unpin reproduction and production purpose-renderer check below. This is not independent verification of all archived acceptance claims.

## STD-120-01 — P2 — unknown unpin receipt permanently disables model fields

Location: `src/agent_alfred/ops/static/models.js:223-224`; cause completes at `349`.

Trigger: pin a non-assigned model; begin Unpin; disconnect before receiving its response; reconnect and explicitly read current settings, where the model is still pinned. The disconnect handler resets `receipt.pending` but retains `receipt.kind === 'unpin'`. The editor tests only `kind`; successful comparison reads do not retire that lock. All display/style/price editors remain disabled indefinitely in this page even though connection and current settings are verified. A page remount or unrelated mutation is required to unblock them.

Expected: preserve the original unknown outcome as a receipt, but give an explicit, current-state-qualified path to resume editing. Do not use operation kind as a permanent pending lock. This follows the input/request ownership separation of ADR-0046 and `docs/design/issue-91/DESIGN.md` R03 items 3, 4, 8, 9 (lines 66-72). This is a concrete defect, not an optional smell. The old `pages.js` model editor had no such persistent unpin lock; `models.js` is new in this candidate.

Reproduction used the existing `tests/browser/settings_server.py` with isolated TemporaryDirectory, ScriptedModelFactory and stub Catalog transport. Its terminal was started with:

```sh
ALFRED_BROWSER_TEST_PORT=18876 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B tests/browser/settings_server.py
```

Observed origin `http://127.0.0.1:18884`, control `http://127.0.0.1:60365`. A named CLI browser `pr120-standards` opened `/models`, and the `opencode:qwen3.7-max` details were opened. Run the retained `models-unpin-repro.js` with `playwright_cli.sh -s=pr120-standards run-code --filename=<script>`. It gates and aborts only `/api/settings` after the initial pin, then dispatches the browser offline/online events that invoke production disconnect/reconnect handlers. The original reproduction script logs rather than returns its local summary; no captured result was fabricated. `current-proof.js` subsequently reads the observed state and returns the portable result in `current-proof-output.txt`.

Observed: `displayDisabled=true`, `priceDisabled=true`, `pinned=true`, `connected=true`, settings revision 1. Offline fixture `model_calls=[]`; the catalog URL shown in proof was received by the fixture's in-process stub, not a network provider.

Repair check: same lost-unpin receipt/reconnect/read case must retain unknown receipt without a permanent edit lock; ordinary in-flight unpin should still block replacement edits, and no automatic command replay should occur.

## STD-120-02 — P2 — known inference probes rendered as unknown purposes

Locations: `src/agent_alfred/ops/static/run-fields.js:40-48`, `src/agent_alfred/ops/static/overview.js:264-270`.

Both new purpose maps use `probe`; the real closed domain value is `inference_probe` (`src/agent_alfred/_schema/contracts.py:47-50`; CONTEXT.md defines inference probes at lines 161 onward). Consequently genuine inference-probe rows and Overview cards take the unknown-purpose branch. Production `runFields({purpose:'inference_probe',purpose_known:true,filter:'system'})` returned `inference_probe（未知用途）` in `current-proof-output.txt`.

Expected: recognized inference-probe purpose must retain its known classification and Chinese label. Applicable standards: `docs/agents/domain.md:41-45` glossary vocabulary and ADR-0045 domain-evidence preservation. Correct the actual key in both maps; consider a shared mapping to prevent the same drift. This is a demonstrated semantic defect; shared-map extraction alone is optional. Repair check: known domain purposes and a genuinely unknown purpose preserve the corresponding distinction in Run details/list and Overview.

## Cleanup and result

Named CLI browser closed normally (`Browser 'pr120-standards' closed`). Verified fixture process PID 38414 from the owned 18884 listener and exact Python command, then sent SIGTERM; its registered normal-close handler ran and exec session exited 0. Worktree status remained empty. No real provider call or user business state accessed.

Standards result: 2 concrete findings, worst P2; 0 optional smell findings. Spec axis belongs to the independent Spec reviewer.
