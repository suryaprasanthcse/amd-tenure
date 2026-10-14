# DECISIONS

Format: `D-<id> | <date> | <module> | decision | reason | source | approved-by: <you> | status`

D-fx | 2026-10-14 | T00 | brl_per_eur = 6.00, kept labelled ASSUMPTION in tenure.yaml, README and UI | no cited reference rate chosen; the human accepted the runbook default | RUNBOOK §4.2 default; human instruction in chat, 2026-10-14 | approved-by: Surya Prasanth R | decided

D-llm-mock | 2026-10-14 | T00 | Until AMD Token Factory access is granted, `.env` sets TENURE_LLM_API_KEY=mock_key and the `llm`-marked G00 tests run against an in-process OpenAI-compatible mock (httpx MockTransport in tests/conftest.py). The tests never skip. With a real key they hit the live endpoint unchanged | the Token Factory whitelist is pending; the human asked for builds and tests to pass independently of the live API | human instruction in chat, 2026-10-14 | approved-by: Surya Prasanth R | decided. CONSEQUENCE: G00 green under mock does NOT verify spike S1. S1 stays PENDING in STATE.md, and G00 must be re-run with real TENURE_LLM_* values before T01's live spikes (S2/S3)

D-band | – | T00 | Value-band edges | raised only if a band holds < 5% of cases | – | – | not raised (see STATE data_facts)
