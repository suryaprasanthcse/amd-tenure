# STATE
current_module:   T00   status: blocked (D-band)
last_green_gate:  none
platform_facts:   S1: PENDING → mock key in .env (D-llm-mock); real TENURE_LLM_* not yet available
                  S2: – · S3: – · S4/S5/S6: –
data_facts:       42,157 cases with text (build_cases on data/raw, brl_per_eur 6.00 ASSUMPTION)
                  band shares (all cases, current edges 0/1500/4000/10000): V1 0.4035, V2 0.4334, V3 0.1248, V4 0.0383
                  order_total EUR quantiles: p50 18.39, p75 31.13, p90 54.16, p95 82.92, p96 96.85
                  split counts, delivered share: pending `tenure ingest` after D-band
runs:             –
open_questions:   1
  Q1 [BLOCKING] D-band: V4 holds 3.83% (< 5%). Options, shares measured on all cases:
     (a) V4 edge 10000 → 8000 (€80):        V1 .4035 V2 .4334 V3 .1106 V4 .0525
     (b) V4 edge 10000 → 7500 (€75):        V1 .4035 V2 .4334 V3 .1052 V4 .0580
     (c) V3 3000, V4 7500 (€30 / €75):      V1 .4035 V2 .3327 V3 .2058 V4 .0580
     (d) 0/1000/2000/4000 (€10/€20/€40):    V1 .2178 V2 .3262 V3 .2928 V4 .1631
     Changing edges touches tenure.yaml bands_cents and, for test_value_band_edges, domain.VALUE_BANDS
     (Appendix R.1) → needs approval as a DECISIONS entry before any edit (C1).
resume_point:     T00 STEP 2 done except D-band. All 44 other G00 tests pass (green-1.txt is the pre-decision run).
                  Next: apply D-band → `uv run tenure ingest` → record data_facts → commit feat(T00) → gate G00 → handoff.
                  Implementation is in the working tree, not yet committed (C3(g) waits for green).
module_table:     | module | gate | status | commit |
                  | T00 | G00 | blocked (D-band) | 0ded756 (test) |
