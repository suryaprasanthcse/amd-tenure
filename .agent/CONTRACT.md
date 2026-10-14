```text
TENURE AGENT OPERATING CONTRACT (applies to every module)

C1  SOURCE OF TRUTH. TENURE_master_blueprint.md defines behaviour; policy/refund_rules_v1.md defines
    the refund rules. Appendix R modules are NORMATIVE: port them verbatim into src/tenure/. Any change
    to an Appendix R module needs a DECISIONS entry (id, reason, approver) before it is made.
C2  READ BUDGET. Read .agent/CONTRACT.md, .agent/STATE.md, your module block, and only the master
    sections listed under INPUTS. Follow a cross-reference only to that section.
C3  TESTS FIRST. For each batch: (a) write the tests; (b) run them and confirm they FAIL for the
    right reason (missing in-scope module or attribute, or an assertion), not a harness error;
    (c) save the output to .agent/evidence/<module>/red-<n>.txt; (d) commit "test(<module>): <batch>";
    (e) implement; (f) run until green and save green-<n>.txt; (g) commit "feat(<module>): <batch>".
C4  NEVER WEAKEN TESTS. Do not delete, skip, xfail, loosen or rename a test. If a test is wrong, stop
    and add an open question to STATE.md.
C5  SCOPE. Create or modify only the files in your module's SCOPE, tests, and .agent/*.
C6  NO PLACEHOLDERS. No TODO, FIXME, NotImplementedError or pass-only bodies in src/.
C7  DETERMINISM. Seeds come from tenure.yaml. Clocks are injected. Iterate in sorted order wherever
    output is hashed or recorded.
C8  SECRETS. Read keys only from environment variables. Never print, log or write them anywhere except
    .env. Never commit .env, data/raw, data/derived or var/.
C9  MEASURED, NOT CLAIMED. Every number meant for the README, slides or video must come from a run
    manifest, the ledger or a spike record. Never type such a number by hand.
C10 HANDOFF. Before ending: update STATE.md, run scripts/gate.py <G> (writes gates/<G>.json), append
    one line to DEVLOG.md (module, model, start/end time, gate result, measured numbers), and commit
    "gate(<G>): green".
C11 STOP. Stop and record an open question on: a spec ambiguity that changes behaviour; a failed
    precondition; a spike result that contradicts the spec; any need to edit an existing test; or
    context near exhaustion (record the exact resume point in STATE.md).
```
