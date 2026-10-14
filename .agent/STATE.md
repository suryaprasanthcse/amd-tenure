# STATE
current_module:   T01   status: green LIVE (G01 re-run on the real endpoint, 5e20f29)
last_green_gate:  G01 (gates/G01.json, commit 5e20f29; 178 tests, 0 failures, 0 skipped, 504 s, live Token Factory
                  endpoint behind the 30 RPM limiter; ratelimit 100%, policy 98.7%, schema 97.3%, intake 97.1%)
                  G00 tests also pass live (45/45, .agent/evidence/T01/g00-live.txt); gates/G00.json keeps its original
                  record because gate.py G00 now trips on coverage of T01 modules, which G00 markers never exercise.
platform_facts:   S1: PASS (2026-10-15) → host=developer.amd.com.cn, model=Gemma-4-31B-IT, models_status=200,
                      listed=True, gemma=True, completions=3/3, rate_limit_headers: none reported
                      (`uv run tenure smoke --n 3`; .env holds the live TENURE_LLM_* values; D-llm-mock retired)
                  Rate limit: shared 30 RPM + daily spend cap (human, 2026-10-15) → D-rpm, client-side limiter.
                  S2: FAIL on throughput, cache proven (var/spikes/S2.json; reduced sweep under D-rpm: n=40, c=1 and 4)
                      c=1: 14.0 cases/min, p50/p95 2570/10864 ms · c=4: 19.5 cases/min, p50/p95 8133/32708 ms,
                      attempts_max 4 (429 retries on the shared limit) · error rate 0 · valid 1.0 at both levels
                      cached_ratio 0.9997 (usage cached_tokens) · cold/warm probe 3665/2487 ms · ~2.9k prompt tokens/case
                      Pass needs ≥ 750 cases/min; at ~20/min a 15k-case replay takes ~13 h → Q4. tenure.yaml concurrency 4.
                  S3: PASS (var/spikes/S3.json, 100 replay cases per trial) → structured_mode=vllm_structured, system_mode=fold
                      json_schema/system 0/100 and json_schema/fold 0/100 (HTTP_400 on every request: field rejected)
                      vllm_structured/system 98/100 (2 SCHEMA_ERROR) · vllm_structured/fold 100/100
                      Caveat: completions arrive wrapped in ```json fences, which a grammar-constrained decoder could not
                      emit, so the endpoint probably accepts but ignores `structured_outputs`; validity comes from
                      parse_decision's fence stripping. D-mode proposed accordingly.
                      Latencies in S3.json include limiter queueing (100 tasks gathered at 2 s spacing); use S2's.
                  decide_sample: 30/30 valid (tests/fixtures/llm/decide_sample.jsonl, vllm_structured, Gemma-4-31B-IT)
                  S4/S5/S6: –
                  prompt_sha (decider static block, policy v1 + 5 examples): 29fd791f3a0adbb12558a886052718ccc5dccac77698fee5fbedba8eda4f54f0
data_facts:       source: data/derived/ingest_manifest.json (config_sha cfe402a6…a8a9, brl_per_eur 6.00 ASSUMPTION D-fx)
                  cases with text: 42,157 · contact dates 2016-10-06 … 2018-08-31
                  by split: train 17,496 · replay 17,985 · live 6,454 · excluded 222
                  by band (edges 0/1500/4000/7500, D-band): V1 17,010 (.4035) · V2 18,270 (.4334) · V3 4,434 (.1052) · V4 2,443 (.0580)
                  split × band: train 7271/7329/1898/998 · replay 7049/8027/1858/1051 · live 2601/2835/642/376
                  delivered share: 0.9673
                  intake: NOT STARTED, by instruction: the human ruled out sending all 42,157 messages live (30 RPM,
                      spend cap). The live G01 intake test classified 50 replay cases (≥ 48 by Gemma) without freezing. → Q5
                  case types, gemma–keyword agreement: – (after intake) · hh agreement: – (T02)
runs:             –
open_questions:   2 (Q3 resolved by D-q3: the two config assertions accept the S3-measured mode)
                  Q4 (S2 fail → master §24 fallback): ~20 cases/min under 30 RPM. Options: (a) live replay of a
                     3k-case slice (~2.5 h) and pre-run the rest elsewhere; (b) move replay to the vllm_droplet profile
                     (MI300X); (c) both: droplet for bulk, Token Factory slice as the "live AMD endpoint" demo. Decide before T03.
                  Q5 (intake strategy, master §11): labels are frozen forever and decide the cells. Options:
                     (a) Gemma on a bounded sample (e.g. the 3k replay slice of Q4 + gold set), keywords for the rest,
                         source column records which; (b) keywords for all now (frozen; Gemma never labels these);
                     (c) defer intake until the droplet profile exists, then Gemma for all. T02 needs case types.
resume_point:     T01 complete and green live. Before T02 on real data: answer Q5 and run intake as decided
                  (never `intake --all` live on Token Factory). Answer Q4 before T03. D-mode still awaits approval.
module_table:     | module | gate | status | commit |
                  | T00 | G00 | green (live re-run: 45/45 pass) | ca580c2 (feat) |
                  | T01 | G01 | green live (5e20f29); intake pending Q5 | 3d09b72, 5e124f3 (feat) |
