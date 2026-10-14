# STATE
current_module:   T00   status: green
last_green_gate:  G00 (gates/G00.json; 45 tests, 0 skipped; G00 llm tests ran against the D-llm-mock endpoint)
platform_facts:   S1: PENDING → mock key in .env (D-llm-mock). Not verified against AMD Token Factory.
                      To verify: put real TENURE_LLM_* in .env, run `uv run tenure smoke --n 3`
                      (or scripts/spikes/s1_models.py), then re-run `uv run python scripts/gate.py G00`.
                      MUST be done before T01's S2/S3 spikes. S1 FAIL → open question "use droplet profile?" (master §24).
                  S2: – · S3: – · S4/S5/S6: –
data_facts:       source: data/derived/ingest_manifest.json (config_sha cfe402a6…a8a9, brl_per_eur 6.00 ASSUMPTION D-fx)
                  cases with text: 42,157 · contact dates 2016-10-06 … 2018-08-31
                  by split: train 17,496 · replay 17,985 · live 6,454 · excluded 222
                  by band (edges 0/1500/4000/7500, D-band): V1 17,010 (.4035) · V2 18,270 (.4334) · V3 4,434 (.1052) · V4 2,443 (.0580)
                  split × band: train 7271/7329/1898/998 · replay 7049/8027/1858/1051 · live 2601/2835/642/376
                  delivered share: 0.9673
                  case types, hh agreement: – (T01 intake, T02 team)
runs:             –
open_questions:   0 (Q1 D-band resolved: V4 edge 7,500 cents)
resume_point:     T00 complete. Next: T01 in a fresh session (needs a real endpoint for its llm tests and S2/S3).
module_table:     | module | gate | status | commit |
                  | T00 | G00 | green (S1 pending, mock LLM) | ca580c2 (feat) |
