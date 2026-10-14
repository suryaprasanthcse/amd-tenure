# DEVLOG

`<utc start> | <utc end> | <module> | model | gate <G> <status> | <measured numbers>`

2026-10-14T15:25Z | 2026-10-14T15:45Z | T00 | claude-opus-5-5 | gate G00 green | 45 tests, 0 skipped; coverage canonical 97.5%, config 100%, domain 100%, llm 90.3%, olist 99.0%, store 97.4%; ingest 42,157 cases (train 17,496 / replay 17,985 / live 6,454 / excluded 222), band shares .4035/.4334/.1052/.0580 after D-band (V4 at 7,500 cents), delivered .9673; S1 PENDING (D-llm-mock: llm tests ran against the in-process mock)
2026-10-14T16:05Z | 2026-10-14T16:40Z | T01 | claude-opus-5-5 | gate G01 green | 171 tests, 0 skipped; coverage policy 98.7%, schema 97.3%, prompt 100%, intake 97.1%; prompt_sha 29fd791f…4f0; S1/S2/S3 PENDING and intake not started (D-llm-mock: llm tests ran against the in-process mock)
