# STATE
current_module:   T04   status: COMPLETE (G04 green against the mock, D-offline-gates; LIVE VERIFICATION PENDING) → next T05
last_green_gate:  G04 (gates/G04.json, commit dfb94eb; 268 tests, 0 failures, 0 skipped, 75 s, cumulative G00–G04,
                  llm tests against the in-process MOCK endpoint under D-offline-gates, no outbound HTTP:
                  no live verification. Coverage stats 100%, cost 100%, baselines 100%, gold 99.3%.
                  LIVE VERIFICATION PENDING for pre-flight, as for G03)
                  G03: (gates/G03.json, commit 93ee9e4; 219 tests, 0 failures, 0 skipped, 88 s, cumulative G00–G03,
                  llm tests against the in-process MOCK endpoint under D-offline-gates: no live verification.
                  LIVE VERIFICATION PENDING for pre-flight: re-run G03 (and later gates) live on a stable link,
                  including test_replay_500_live with its < 1% error bound; its var/runs/T03-live500-* dirs from
                  mock runs hold mock numbers and must not be quoted)
                  G02: (gates/G02.json, commit 981aa19; 200 tests, 0 failures, 0 skipped, 651 s, cumulative G00–G02,
                  llm tests live on Token Factory behind the 30 RPM limiter; humans 100%, gold 99.2% coverage)
                  G01: gates/G01.json, commit b85cf6a, 184 tests live after the D-intake run.
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
                      parse_decision's fence stripping. D-mode approved (decided 2026-10-15).
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
                  intake: COMPLETE (2026-10-15, D-intake) → data/derived/intake.parquet, merged into cases.parquet
                      42,157 labelled, 0 empty · source gemma 4,496 · keywords 37,661 (37,657 keyword fill + 4 Gemma
                      calls that fell back) · Gemma subsets: gold-pool 1,500 (live; 1,499 gemma) + slice 3,000 (replay)
                      live run: gold-pool 01:21–02:38, slice 02:38–05:18 (IST), 4,500 calls at 30 RPM, no endpoint errors
                      case types (excl. 222 excluded): POSITIVE 24,755 · VAGUE_COMPLAINT 4,178 · NOT_RECEIVED 3,213 ·
                      QUALITY 2,602 · OTHER 2,162 · DELAY 1,139 · MISSING_ITEM 831 · BILLING 690 · DEFECTIVE 572 ·
                      DAMAGED 561 · WRONG_ITEM 518 · REMORSE 478 · WRONG_VARIANT 169 · SELLER_SERVICE 67
                      gemma–keyword agreement on the Gemma-labelled cases: 3130/4496 = 0.696 overall; per Gemma type:
                      POSITIVE 2405/2783 · NOT_RECEIVED 272/429 · DELAY 96/137 · VAGUE_COMPLAINT 37/65 · DAMAGED 45/82 ·
                      DEFECTIVE 45/91 · BILLING 24/59 · QUALITY 60/181 · WRONG_ITEM 44/150 · MISSING_ITEM 77/294 ·
                      WRONG_VARIANT 15/90 · OTHER 6/68 · REMORSE 4/33 · SELLER_SERVICE 0/34
                      CAVEAT (sanity metric, not gated): the keyword rules miss much of what Gemma finds in the minority
                      types, so keyword-labelled cells (all of train, most of replay) will hold fewer of those types than
                      the Gemma-labelled slice. T04 should report results by intake source.
                  team (T02, `tenure humans`, seed 20261018, Policy v1): humans.parquet 41,935 cases (all splits except
                      excluded), double-coded 0.1498; humans_drift.parquet 6,454 live cases, v2 from 2018-08-01 (D-POLICY)
                  calibration on train+replay (35,481 cases, 5,328 pairs = 0.1502):
                      human–human agreement 0.9261 (band 0.85–0.96 ✓) · oracle vs primary 0.9641 (≥ hh ✓)
                      by type (pairs): SELLER_SERVICE 0.818 (11) · WRONG_VARIANT 0.824 (17) · QUALITY 0.870 (331) ·
                      DAMAGED 0.914 (70) · DELAY 0.916 (166) · OTHER 0.923 (284) · POSITIVE 0.928 (3,074) · BILLING 0.931 (87) ·
                      NOT_RECEIVED 0.935 (416) · VAGUE_COMPLAINT 0.935 (556) · MISSING_ITEM 0.944 (107) · REMORSE 0.951 (61) ·
                      DEFECTIVE 0.958 (71) · WRONG_ITEM 0.961 (77) · spread over types with ≥ 100 pairs 0.074 (≥ 0.05 ✓)
                      by intake source: gemma 0.918 (487 pairs) · keywords 0.927 (4,841 pairs)
                      (master §14.5 synthetic mix: 0.890 hh, 0.938 oracle; the Olist mix is 70% POSITIVE, hence higher)
                  gold (T02, `tenure gold sample`): data/gold/gold_cases.parquet, 300 cases from the D-intake gold pool
                      BILLING 16 · DAMAGED 22 · DEFECTIVE 22 · DELAY 15 · MISSING_ITEM 23 · NOT_RECEIVED 24 · OTHER 21 ·
                      POSITIVE 51 · QUALITY 22 · REMORSE 12 · SELLER_SERVICE 5 · VAGUE_COMPLAINT 22 · WRONG_ITEM 23 ·
                      WRONG_VARIANT 22 (pool shortfalls in SELLER_SERVICE/REMORSE/DELAY redistributed by size, §15)
                      labels: none yet → HUMAN TASK 17 Oct 15:30–18:00: `uv run tenure gold label --labeler <name>`
                      (English translations come from Gemma once per case, cached, through the 30 RPM limiter)
runs:             R-20261015-01 (live, Token Factory, Gemma-4-31B-IT, run_key 3e10bf3e14f30d7e9409bb57) · K2 applied
                      (D-k2): FROZEN at the first 500 cases of the D-replay slice in case order, contact dates
                      2018-01-01 … 2018-02-03 (decisions.jsonl, 500 records; manifest has no "finished": run killed)
                  figures (events.jsonl progress at committed=500, attempt 2 started 2026-10-15T04:22:34Z; 10 of the
                      500 came from the outcome cache, 490 were endpoint calls):
                      wall 3,260.5 s (54.3 min) · 9.20 cases/min · compression 821× (33 days in 54.3 min)
                      cached_ratio 0.9312 · prompt 431 tok/s · completion 11.2 tok/s · p50/p95 74,698 / 220,812 ms
                      valid 483/500 = 0.966 · overall agreement 466/500 = 0.932
                      endpoint errors 17/500 = 3.4%: TRANSPORT 10 · HTTP_429 5 · TIMEOUT 1 · HTTP_500 1 (all after
                      7 attempts; counted invalid and as disagreements, I5; not cached, I7)
                      attempts of the 483 stored completions: 1:294 · 2:101 · 3:34 · 4:26 · 5:12 · 6:11 · 7:5
                      var/outcomes.db holds 602 completions for this run_key (102 beyond case 500, uncommitted)
evaluation:       R-20261015-01 (T04, `uv run tenure evaluate --run R-20261015-01` → var/runs/R-20261015-01/evaluation.json,
                      decisions_sha256 e72d3564…c89b, complete=false: 500 of 3,000 planned, stopped under D-k2)
                      overall 466/500 = 0.932, Wilson-cc 95% [0.9053, 0.9518], valid 483/500 (17 invalid = endpoint
                      errors, counted as disagreements, I5) · by intake source: gemma 464/498, keywords 2/2
                      hh baseline pairs (train+replay): 5,328 pairs, 4,934 agree, 55 cells; n_min 60, pooled per §4.2
                      49 cells, only 2 with n ≥ 30 (POSITIVE|V1 139, POSITIVE|V2 130); 47 cells blocked by N_MIN at L1
                      CELLS PASSING THE NI THRESHOLD AND n_min, ignoring dwell, monitoring power and cost:
                        L1: POSITIVE|V1 (L_cc 0.8771 ≥ 0.9220 − 0.10) and POSITIVE|V2 (L_cc 0.8689 ≥ 0.9299 − 0.10)
                        L2: none (POSITIVE|V1 clears the NI threshold 0.8720 but n 139 < 150; POSITIVE|V2 fails NI)
                        L3: none
                      With the valid-rate gate, NO cell passes any level: POSITIVE|V1 valid 135/139 = 0.971 and
                      POSITIVE|V2 125/130 = 0.962 are < 0.98. Every one of their 9 invalids is an endpoint error over the
                      mobile link (TRANSPORT/HTTP_429/TIMEOUT/HTTP_500), not a model output that failed to parse, so on a
                      stable link both cells would probably clear L1 (not measured). Blocker counts L1: N_MIN 47,
                      NON_INFERIORITY 47, VALID_RATE 9 · L2: N_MIN 49, NON_INFERIORITY 48, COST 9, VALID_RATE 9, CEILING 7
                      · L3: N_MIN 49, NON_INFERIORITY 49, COST 9, VALID_RATE 9, CEILING 7. Cost (B 2,000, seed per cell,
                      run_id, n): POSITIVE|V1 mean/UCB 11.5/23.0 ¢ vs hh 0.0 ¢ (threshold 50 ¢); POSITIVE|V2 15.4/30.8 ¢
open_questions:   0 (Q8 → D-offline-gates · Q7 → D-rpm2 · Q3 → D-q3 · Q4 → D-replay · Q5 → D-intake · Q6 approved with D-intake)
                  Q7 (T03, 2026-10-15): the Token Factory now returns rate-limit headers that contradict D-rpm:
                      x-ratelimit-limit-user-rpm 20 (limiter sends 30) and x-ratelimit-limit-user-daily-usd 1
                      (used 0.029; a mostly-cached decider call costs ~1.2e-5 USD, so 3,000 cases ≈ 0.04 USD).
                      Decider latency measured now: 26.3 / 32.3 / 100.1 s for three slice cases (S2: p50 2.6 s),
                      so timeout_s 60 times out; the 3-case CLI smoke (var/runs/T03-cli-smoke) needed 6–7 attempts
                      per call. Proposed: rpm 20, timeout_s 180, token_factory concurrency 16 (≈ 20 RPM × 45 s / 60).
                      RESOLVED → D-rpm2 (rpm 20, timeout_s 180, concurrency 16; two assertions follow the yaml).
                  Q8 (T03, 2026-10-15): gate G03 includes tests/integration/test_replay_500_live.py (llm), which runs
                      500 fresh live calls and asserts endpoint_calls == 500 and error_rate < 1%. The frozen run has
                      3.4% endpoint errors, and the human asked that no further live calls be made. Freezing the
                      500 as that test's evidence needs a test change (C4) and still fails its < 1% bound. Not run,
                      not edited; gate(G03) not committed. RESOLVED → D-offline-gates (mock gates until a stable link; amended
                      with a 5 ms mock round trip in tests/conftest.py, no assertion changed).
resume_point:     T05 (ladder + CUSUM) in a fresh session. Gates run under D-offline-gates while off a stable link:
                    TENURE_LLM_API_KEY=mock_key TENURE_LLM_BASE_URL=http://127.0.0.1:9/v1 uv run python scripts/gate.py G05
                  T04 provides tenure.stats (R.7), tenure.cost (R.2), tenure.baselines (pair_table, cell_baselines) and
                  cli.evaluate_run; evaluation.json blockers use the Appendix A codes (CEILING, N_MIN, VALID_RATE,
                  NON_INFERIORITY, COST); DWELL and MONITOR_POWER are left to T05.
                  Note for T05/T07: on the 500 frozen cases no cell passes any level (see evaluation), so a promotion
                  demo needs more replay evidence (the rest of the slice on a stable link or the vLLM droplet).
                  Pre-flight (stable link, before G08 / 18 Oct freeze): re-run the gates live with .env values.
module_table:     | module | gate | status | commit |
                  | T00 | G00 | green (live re-run: 45/45 pass) | ca580c2 (feat) |
                  | T01 | G01 | green live after intake (b85cf6a) | 3d09b72, 5e124f3, e7c5396, b85cf6a (feat) |
                  | T02 | G02 | green live (981aa19) | 981aa19 (feat) |
                  | T03 | G03 | green MOCK (D-offline-gates), live pending | 7baaa54 (feat), 0b03a96 (D-rpm2), 93ee9e4 |
                  | T04 | G04 | green MOCK (D-offline-gates), live pending | 5474446 (test), dfb94eb (feat) |
