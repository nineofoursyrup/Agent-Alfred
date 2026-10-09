# POST-120-01 repair handoff

request_id `post120-run-evidence-repair-r1`; generation `1`. The production race is reproduced, repaired, locally verified, and frozen for independent review. No commit, push, PR mutation, or issue mutation was performed by this executor.

Main worktree: `/Users/nineofour/.codex/worktrees/dashboard-run-evidence-fix/Agent-Alfred`.

- Baseline HEAD: `63fe1a6ac026084118cbc9585cedb30e7f03a0a2`.
- Frozen temporary-index tree: `123caa591cf8053a9cfbafa7fdbe2e9865e77686`.
- Full binary diff SHA-256: `28bd8f28f77cf96b2a1e57be0fdcb7756b6700ae42f2b32a961740da7179d64d`.
- Three changed paths, all mode `100644`: `src/agent_alfred/ops/static/runs.js`, `src/agent_alfred/ops/static/topology.js`, and `tests/browser/run-path.spec.js`. Individual blob/SHA-256 identities and exact source copies are in `candidate.json` and `source/`; complete diff is `candidate.diff`.

The topology component now synchronizes only Step / Attempt references whose target availability changed when Run process evidence finishes rendering or is cleared by its existing expired-snapshot branch. It preserves unchanged reference elements, selected node, manual path snapshot, viewport, raw-definition expansion, and focus. It never adds a path request. Behaviour has no Run references and receives no new reads or business-control changes.

Verification passed on the same final candidate: the new actual-response lifecycle regression (1 test), all 137 existing tests across run-path, topology, runs, inbox-run-sources, inbox-run-navigation, run-purposes, and behaviour, `tsc --noEmit`, and `git diff --check`. The 137-test command excludes the unchanged new test whose 1-test pass is reused. Original 1280px and 390px keyboard cases both passed without modification. Exact commands, raw log hashes, environment, and explicit non-run scope are in `verification.json`.

Original merge CI failure and two controlled red runs remain preserved. `diagnosis.md` explains the trace ordering and causal chain; `red-sources/manifest.json` binds the exact failing test source from each retained trace. No first failure was overwritten.

The support worktree was confirmed clean at HEAD `3f0588d3693d598cc1785c7eee82e3c52707dca0`, branch `codex/dashboard-supported-rollback`, before copying the same three files byte-for-byte. Its frozen tree is `19be93d8a59b0cb463f7fb2fbcbc2d0d371699b0`; `support/candidate.json` and `support/candidate.diff` bind it. Comparing all 620 tracked source files confirms only the original `index.html` and `shell.js` default-Inbox differences remain between main and support. `support-comparison.json` records that check.

Product and test writes are paused at these identities. Independent Standards / Spec review, remote CI, wheel / four-installation / G08 validation, publishing, and acceptance / issue closeout remain with the root coordinator. Full local Python tests, unrelated local browser suites, and a separate support browser run were not repeated for this frontend-only repair.
