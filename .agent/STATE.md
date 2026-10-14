# STATE
current_module:   T01   status: green (G01, feat 3d09b72)
last_green_gate:  G01 (gates/G01.json; 171 tests, 0 skipped; llm tests ran against the D-llm-mock endpoint)
platform_facts:   S1: PASS (2026-10-15) → host=developer.amd.com.cn, model=Gemma-4-31B-IT, models_status=200,
                      listed=True, gemma=True, completions=3/3, rate_limit_headers: none reported
                      (`uv run tenure smoke --n 3`; .env now holds the live TENURE_LLM_* values, D-llm-mock retired)
                      Live re-runs of G00/G01 not done yet: the session's permission classifier blocked further
                      traffic to this host pending the human's confirmation.
                  S2: PENDING (needs S1). Run `uv run tenure spike s2` → var/spikes/S2.json, writes concurrency to tenure.yaml.
                  S3: PENDING (needs S1). Run `uv run tenure spike s3` → var/spikes/S3.json, writes structured_mode and
                      system_mode to tenure.yaml. Until then tenure.yaml keeps json_schema/system (D-mode proposed).
                  Code paths of both spikes were dry-run against the in-process mock: S3 picked json_schema/system,
                  S2 reported FAIL (no cache evidence), which is the correct mock outcome. No mock numbers are recorded.
                  S4/S5/S6: –
                  prompt_sha (decider static block, policy v1 + 5 examples): 29fd791f3a0adbb12558a886052718ccc5dccac77698fee5fbedba8eda4f54f0
data_facts:       source: data/derived/ingest_manifest.json (config_sha cfe402a6…a8a9, brl_per_eur 6.00 ASSUMPTION D-fx)
                  cases with text: 42,157 · contact dates 2016-10-06 … 2018-08-31
                  by split: train 17,496 · replay 17,985 · live 6,454 · excluded 222
                  by band (edges 0/1500/4000/7500, D-band): V1 17,010 (.4035) · V2 18,270 (.4334) · V3 4,434 (.1052) · V4 2,443 (.0580)
                  split × band: train 7271/7329/1898/998 · replay 7049/8027/1858/1051 · live 2601/2835/642/376
                  delivered share: 0.9673
                  intake: NOT STARTED. `uv run tenure intake --all` refuses the mock key (exit 2) on purpose: intake
                      labels are frozen forever, so a keyword-only run now would block Gemma labels later.
                      Start it in the background once S1 passes; it resumes on restart.
                  case types, gemma–keyword agreement: – (after intake) · hh agreement: – (T02)
runs:             –
open_questions:   0 (Q2 resolved by D-eol: .gitattributes forces LF under policy/; this checkout was already LF,
                  a fresh clone without it was CRLF)
resume_point:     T01 complete under the mock. When the Token Factory key is whitelisted, in order:
                  1. real TENURE_LLM_* in .env → `uv run tenure smoke --n 3` (S1) → re-run gates G00 and G01 live;
                  2. `uv run tenure spike s3` then `uv run tenure spike s2`; copy the printed lines here; approve D-mode;
                  3. `uv run tenure decide-sample --n 30` → tests/fixtures/llm/decide_sample.jsonl (commit it);
                  4. `uv run tenure intake --all` in the background; record type distribution + agreement here.
                  T02 can start meanwhile, but it needs case types, so it is blocked on step 4 for anything on real data.
module_table:     | module | gate | status | commit |
                  | T00 | G00 | green (S1 pending, mock LLM) | ca580c2 (feat) |
                  | T01 | G01 | green (S1/S2/S3 + intake pending, mock LLM) | 3d09b72 (feat) |
