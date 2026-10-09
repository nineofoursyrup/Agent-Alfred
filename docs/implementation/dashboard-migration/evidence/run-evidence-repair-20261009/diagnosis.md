# POST-120-01 diagnosis

Confirmed production race on main baseline `63fe1a6ac026084118cbc9585cedb30e7f03a0a2`. The Run path and process evidence are separate reads. Selecting a graph node before process evidence arrives creates unavailable reference paragraphs. When the process evidence later renders valid Step / Attempt targets, the graph detail does not reconcile those references.

## Original failure and causal evidence

- Actual merge CI: `37935569616`, `tests/browser/run-path.spec.js:57`, CE-11/AC-19 keyboard graph/text equivalence 1280. The first failure was the visible node-reference anchor assertion at original line 79.
- Original retained trace: `../postmerge-first-failure-artifacts/run-path-CE-11-AC-19-keyboard-graph-text-equivalence-1280/trace.zip`. Sanitized public failure context is under `first-failure/`.
- Trace monotonic milliseconds: the browser `run-path` request finished at `610540.473`; `run-evidence` started at `610580.563` and finished at `610630.298`. The classify button was focused at `610618.641` and activated by Enter at `610624.776`, while evidence remained outstanding.
- The selected-node detail rendered `Step 1 · 关联证据不可用` and its Attempt equivalent. Process evidence subsequently contained the real Step 1 and Attempt. The node detail retained unavailable paragraphs. Zoom changed only the SVG viewBox; it did not remove a previously existing link.
- Baseline `topology.js` decided target availability only inside `showNode()` using `document.getElementById()`. Baseline `runs.js` rendered process evidence without notifying the selected path detail. This explains the exact ordering and persistent symptom without requiring a focus or zoom failure.

## Controlled reproduction

The added browser test uses the real routing server and actual `run-evidence` HTTP response. It temporarily holds that response, opens the path, selects classify, then releases the response. It proves that `#step-1` exists before asserting the selected node exposes its Step link. No response payload is manufactured and no sleep, retry, timeout increase, or existing keyboard assertion is used to hide the failure.

- Initial red: `../postmerge-controlled-red.log`, exit 1, missing Step 1 link at the added assertion. Its exact test source is preserved in `red-sources/controlled-red.spec.js`.
- Enhanced red: `../postmerge-controlled-red-r2.log`, exit 1 at the same missing-link condition, after confirming the expanded raw definition and its focus were retained. Its exact test source is preserved in `red-sources/controlled-red-r2.spec.js`.
- Both original red traces remain unchanged outside this directory. `red-sources/manifest.json` binds their source hashes.
- First green: `green.log`, 1 passed. Final lifecycle extension: `lifecycle.log`, 1 passed, proving late arrival, unchanged-reference focus retention, true trace disappearance, and restoration through real endpoint reads.

## Minimal repair boundary

`topologyView.syncReferences()` reconciles only references whose target availability changed. Unchanged links retain their DOM identity, and the selected node, SVG, raw definition, and viewport are not rebuilt. A reference whose focused link becomes unavailable retains focus at its corresponding unavailable text; restoration returns an ordinary tabbable link. Link activation rechecks the current target.

`runsPage.update()` invokes this reconciliation after the actual process DOM update and in the pre-existing snapshot-expired clearing branch. It does not read the graph, alter the snapshot, or change the endpoint protocol. Behaviour shares the topology component but has no Run references, so this synchronization is a no-op there.

The lifecycle regression also checks one path read throughout, unchanged viewBox and raw-definition expansion, no focus theft from the raw definition, stable focused links across unchanged evidence, link withdrawal when real trace targets disappear, and restoration with normal tab order. Existing 1280/390 keyboard tests remain unchanged.

The final bound candidate and full verification commands/results are recorded separately in `candidate.json`, `candidate.diff`, and `verification.json`. This report is diagnosis and local repair evidence; independent review, CI, publication, and issue closure belong to the root coordinator.
