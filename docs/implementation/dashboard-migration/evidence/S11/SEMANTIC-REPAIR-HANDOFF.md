# S11 semantic repair, fixed review unit

Production commit: `c8397368074dacfc9837599b195e0b0dfc686b7a`, tree `5eb14d976843ce916d5e319e38679ab1ae98703b`, parent `8f579a4712075721728b2043d9f626689ff377a8`.
Test-only successor: `0758e468c009524d2c14f9545786fe9b93b768bf`, tree `f0046246f133c2f26dab97e615783b87cab42188`; its parent is entry-only `593564fc4b6ed08bc332bbfcf76dcf67107929c0`.
Review repair with `git diff 8f579a4 c839736` plus `git show 0758e46`; supported target should retain repair and focused-test successor, not require the default-entry change. Current uncommitted work is installation harness only (and test-generated tmp); no additional product bytes changed.

Models retains `submitted` separately from original `baseline` and CAS revision. Pending or unknown receipt owns submitted B; successor A remains dirty even when A equals the pre-submit saved value. Returning to B has no new dirty input. Definite refusal retires submitted identity; success updates baseline only from confirmed persisted field; explicit adoption alone takes current CAS version. Memory compares current edit request to the frozen operation request before falling back to original saved value. No server/save protocol changed.

Behavior references: tests/browser/models-migration.spec.js S11 pending/unknown tests capture actual POST [B, revision1] once, actual persisted B, no model calls; existing SPEC F1 cases preserve stale revision0 and require explicit adoption before revision1/2 resend, cover lost receipt, failed/pre-failure reads and instance change. Memory existing editing-during-save now submits B then restores A, asserts leave cancellation, original receipt keeps A and focus, persisted B, explicit later save creates version3 A. Existing late-save/create and unknown receipts remain covered.

Commands (Node22 PATH, CPython3.14.7):
- `ALFRED_BROWSER_TEST_PORT=17900 npm run test:browser -- tests/browser/models-migration.spec.js tests/browser/memory-migration.spec.js --grep 'S11 model|editing during a save' --output=E/S11/submitted-red-artifacts`: exit1, all three actual dirty-guard regressions FAIL before product change.
- same two files without grep, output submitted-green-artifacts: exit1, 24 PASS /1 FAIL. First three regressions PASS. Existing STD02 expected discard after a submitted-only action, contradicting correct guard semantics; its hard wait timed out. Original trace retained.
- `ALFRED_BROWSER_TEST_PORT=17909 npm run test:browser -- tests/browser/models-migration.spec.js --grep 'S11 model|STD02' --output=E/S11/submitted-green-02-artifacts`: exit0, 3 PASS. STD02 now types a successor before exercising original discard/navigation/late-focus assertions. This isolated command temporarily used auto-offset fixture port17917 and exited normally; subsequent commands stay in reserved block.
- `npm run typecheck`: exit0, typecheck-01.log. Product identical between initial green and focused successor; entry-only changes were already present throughout both.

Logs and trace directories are immutable. SHA256 for each log is in semantic-repair-evidence.json. Whole-source browser/Python gates still running; this handoff does not promote G08 or whole acceptance.
