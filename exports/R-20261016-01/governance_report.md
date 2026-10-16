# Governance report

Verification: OK · 1038 entries · head seq 1038 · e90a05f787cf7ab62960a2f74f90aec5afb3db62089838d8ad9681481c0c9084

## Runs

| seq | kind | run_id | mode | agent | model | run_key | cases / committed |
|---|---|---|---|---|---|---|---|
| 4 | RUN_STARTED | R-20261015-01 | replay | Gemma-4-31B-IT | Gemma-4-31B-IT | 3e10bf3e14f30d7e9409bb57 | 3000 |
| 505 | RUN_STARTED | L-20261015-01 | live | mock-oracle-v1 | – | – | 6454 |
| 531 | RUN_FINISHED | L-20261015-01 | live | mock-oracle-v1 | – | – | 6454 |
| 537 | RUN_STARTED | R-20261016-01 | replay | Gemma-4-31B-IT | Gemma-4-31B-IT | 3e10bf3e14f30d7e9409bb57 | 500 |
| 1038 | RUN_FINISHED | R-20261016-01 | – | – | – | – | 500 |

## System entries

| seq | kind | actor | detail |
|---|---|---|---|
| 1 | CONFIG | system | {"config_sha": "45d31140465e769144327980e5e34848fbe733aec8a6174c1f66aa9955ea97f7"} |
| 2 | POLICY_VERSION | system | {"changed_clauses": [], "policy_id": "refund_rules_v1", "policy_sha": "1e4498d26f1bad077f8fe2d4851ad63e3f80726982a8c68b02fc2a4a45820e81"} |
| 3 | MODEL_PROFILE | system | {"endpoint_kind": "token_factory", "gpu": null, "image_digest": null, "model": "Gemma-4-31B-IT", "profile": "token_factory", "rocm_version": null, "vllm_version": null} |
| 519 | DRIFT_INJECTED | system | {"at_date": "2018-08-01", "detail": {"changed_clauses": ["P1"], "policy_file": "policy/refund_rules_v2.md"}, "scenario": "D-POLICY"} |
| 532 | EXPORT | system | {"head_hash": "bf5186505d6cdcbc7ac016c6445273fbd94c4bf91123e3615761f5e8504bbf40", "head_seq": 531, "path_sha": "484f7738cb217d4ea212ef32ccb9e0922f406f07649f661cefb89946e32807c3"} |
| 533 | EXPORT | system | {"head_hash": "9202839f346a9a7011b10ef1364e5e38dcb9464562e3ca9424a6ca5b04c4abda", "head_seq": 532, "path_sha": "c91e1023f3d6d89b253de49ae5a01c2b43479cedd7a6e48246f6372e06b0426f"} |
| 534 | CONFIG | system | {"config_sha": "45d31140465e769144327980e5e34848fbe733aec8a6174c1f66aa9955ea97f7"} |
| 535 | POLICY_VERSION | system | {"changed_clauses": [], "policy_id": "refund_rules_v1", "policy_sha": "1e4498d26f1bad077f8fe2d4851ad63e3f80726982a8c68b02fc2a4a45820e81"} |
| 536 | MODEL_PROFILE | system | {"endpoint_kind": "token_factory", "gpu": null, "image_digest": null, "model": "Gemma-4-31B-IT", "profile": "token_factory", "rocm_version": null, "vllm_version": null} |

DECISION entries: 1000 · GOLD_LABEL entries: 0

## Cells

### BILLING\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### BILLING\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### BILLING\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### DAMAGED\|V1

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 525 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=814.6 · arl1=11.0 · baseline=0.914286 · baseline_scope=case_type · cusum_h=2.9233 · cusum_p0=0.010 · cusum_p1=0.186 · delta=0.100000 · lower=0.866554 · n=32 · newcombe_diff_lower=-0.056928 · threshold=0.814286 · x=32 | [0.866554, 1.000000] | 0.814286 | 0.914286 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 6 · blockers N_MIN, NON_INFERIORITY

### DAMAGED\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 528 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=525.8 · arl1=17.3 · baseline=0.914286 · baseline_scope=case_type · cusum_h=3.4708 · cusum_p0=0.031 · cusum_p1=0.186 · delta=0.100000 · lower=0.820019 · n=32 · newcombe_diff_lower=-0.102571 · threshold=0.814286 · x=31 | [0.820019, 0.998367] | 0.814286 | 0.914286 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 5 · blockers N_MIN, NON_INFERIORITY

### DAMAGED\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 3 · blockers N_MIN, NON_INFERIORITY

### DAMAGED\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### DEFECTIVE\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 3 · blockers N_MIN, NON_INFERIORITY

### DEFECTIVE\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 5 · blockers N_MIN, NON_INFERIORITY

### DEFECTIVE\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### DEFECTIVE\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### DELAY\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 7 · blockers N_MIN, NON_INFERIORITY

### DELAY\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 11 · blockers N_MIN, NON_INFERIORITY

### DELAY\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 7 · blockers N_MIN, NON_INFERIORITY

### MISSING_ITEM\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 6 · blockers N_MIN, NON_INFERIORITY

### MISSING_ITEM\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 524 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=510.8 · arl1=30.3 · baseline=0.943925 · baseline_scope=case_type · cusum_h=3.0530 · cusum_p0=0.046 · cusum_p1=0.156 · delta=0.100000 · lower=0.862413 · n=65 · newcombe_diff_lower=-0.087311 · threshold=0.843925 · x=62 | [0.862413, 0.988004] | 0.843925 | 0.943925 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 18 · blockers N_MIN, NON_INFERIORITY

### MISSING_ITEM\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 8 · blockers N_MIN, NON_INFERIORITY

### MISSING_ITEM\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 3 · blockers N_MIN, NON_INFERIORITY

### NOT_RECEIVED\|V1

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 521 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=525.6 · arl1=38.5 · baseline=0.938776 · baseline_scope=cell · cusum_h=2.9032 · cusum_p0=0.060 · cusum_p1=0.161 · delta=0.100000 · lower=0.846529 · n=67 · newcombe_diff_lower=-0.097255 · threshold=0.838776 · x=63 | [0.846529, 0.980702] | 0.838776 | 0.938776 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 16 · blockers N_MIN, NON_INFERIORITY

### NOT_RECEIVED\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 511 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=521.2 · arl1=24.3 · baseline=0.925134 · baseline_scope=cell · cusum_h=3.2581 · cusum_p0=0.044 · cusum_p1=0.175 · delta=0.100000 · lower=0.836353 · n=45 · newcombe_diff_lower=-0.092943 · threshold=0.825134 · x=43 | [0.836353, 0.992264] | 0.825134 | 0.925134 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 15 · blockers N_MIN, NON_INFERIORITY

### NOT_RECEIVED\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 4 · blockers N_MIN, NON_INFERIORITY

### NOT_RECEIVED\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 5 · blockers N_MIN, NON_INFERIORITY

### OTHER\|V1

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 520 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=506.8 · arl1=22.9 · baseline=0.891892 · baseline_scope=cell · cusum_h=3.3515 · cusum_p0=0.057 · cusum_p1=0.208 · delta=0.100000 · lower=0.794772 · n=35 · newcombe_diff_lower=-0.104871 · threshold=0.791892 · x=33 | [0.794772, 0.990040] | 0.791892 | 0.891892 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### OTHER\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 522 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=505.8 · arl1=43.4 · baseline=0.933824 · baseline_scope=cell · cusum_h=2.7855 · cusum_p0=0.070 · cusum_p1=0.166 · delta=0.100000 · lower=0.836516 · n=71 · newcombe_diff_lower=-0.103166 · threshold=0.833824 · x=66 | [0.836516, 0.973784] | 0.833824 | 0.933824 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### OTHER\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### OTHER\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### POSITIVE\|V1

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 506 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=509.8 · arl1=32.8 · baseline=0.922029 · baseline_scope=cell · cusum_h=3.0681 · cusum_p0=0.062 · cusum_p1=0.178 · delta=0.100000 · lower=0.900369 · n=273 · newcombe_diff_lower=-0.024062 · threshold=0.822029 · x=256 | [0.900369, 0.962187] | 0.822029 | 0.922029 |
| 508 | L-20261015-01 | L1 → L2 | PROMOTE | arl0=501.6 · arl1=67.2 · baseline=0.922029 · baseline_scope=cell · cost_mean_cents=12.50 · cost_threshold_cents=50.00 · cost_ucb_cents=20.45 · cusum_h=2.3570 · cusum_p0=0.065 · cusum_p1=0.128 · delta=0.050000 · detect_days=29.4 · lower=0.902209 · n=352 · newcombe_diff_lower=-0.022559 · threshold=0.872029 · x=329 | [0.902209, 0.957220] | 0.872029 | 0.922029 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L1 · n 139 · blockers N_MIN, DWELL

### POSITIVE\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 507 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=523.1 · arl1=36.7 · baseline=0.929878 · baseline_scope=cell · cusum_h=3.0081 · cusum_p0=0.063 · cusum_p1=0.170 · delta=0.100000 · lower=0.901240 · n=300 · newcombe_diff_lower=-0.030929 · threshold=0.829878 · x=281 | [0.901240, 0.960401] | 0.829878 | 0.929878 |
| 510 | L-20261015-01 | L1 → L2 | PROMOTE | arl0=505.1 · arl1=66.7 · baseline=0.929878 · baseline_scope=cell · cost_mean_cents=11.76 · cost_threshold_cents=50.00 · cost_ucb_cents=18.63 · cusum_h=2.4067 · cusum_p0=0.059 · cusum_p1=0.120 · delta=0.050000 · detect_days=27.1 · lower=0.912538 · n=408 · newcombe_diff_lower=-0.020129 · threshold=0.879878 · x=384 | [0.912538, 0.961152] | 0.879878 | 0.929878 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L1 · n 130 · blockers N_MIN, DWELL

### POSITIVE\|V3

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 512 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=500.6 · arl1=56.0 · baseline=0.948630 · baseline_scope=cell · cusum_h=2.6431 · cusum_p0=0.073 · cusum_p1=0.151 · delta=0.100000 · lower=0.850556 · n=96 · newcombe_diff_lower=-0.100983 · threshold=0.848630 · x=89 | [0.850556, 0.967672] | 0.848630 | 0.948630 |
| 530 | L-20261015-01 | L1 → L0 | CUSUM | window_reset=True | – | – | – |

CUSUM signals:

| seq | run_id | level | S | h | outcomes monitored | change point |
|---|---|---|---|---|---|---|
| 529 | L-20261015-01 | L1 | 3.1946 | 2.6431 | 289 | 279 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 13 · blockers N_MIN, NON_INFERIORITY

### POSITIVE\|V4

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 509 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=505.7 · arl1=18.2 · baseline=0.932886 · baseline_scope=cell · cusum_h=3.4462 · cusum_p0=0.027 · cusum_p1=0.167 · delta=0.100000 · lower=0.841905 · n=37 · newcombe_diff_lower=-0.094985 · threshold=0.832886 · x=36 | [0.841905, 0.998588] | 0.832886 | 0.932886 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 13 · blockers N_MIN, NON_INFERIORITY

### QUALITY\|V1

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 517 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=505.9 · arl1=18.4 · baseline=0.907895 · baseline_scope=cell · cusum_h=3.2932 · cusum_p0=0.037 · cusum_p1=0.192 · delta=0.100000 · lower=0.861601 · n=54 · newcombe_diff_lower=-0.053520 · threshold=0.807895 · x=52 | [0.861601, 0.993559] | 0.807895 | 0.907895 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 6 · blockers N_MIN, NON_INFERIORITY

### QUALITY\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 518 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=502.5 · arl1=40.5 · baseline=0.893333 · baseline_scope=cell · cusum_h=2.9498 · cusum_p0=0.093 · cusum_p1=0.207 · delta=0.100000 · lower=0.811482 · n=75 · newcombe_diff_lower=-0.090924 · threshold=0.793333 · x=68 | [0.811482, 0.958481] | 0.793333 | 0.893333 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 17 · blockers N_MIN, NON_INFERIORITY

### QUALITY\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### QUALITY\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 3 · blockers N_MIN, NON_INFERIORITY

### REMORSE\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### REMORSE\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### SELLER_SERVICE\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### SELLER_SERVICE\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### SELLER_SERVICE\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### SELLER_SERVICE\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### VAGUE_COMPLAINT\|V1

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 513 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=578.9 · arl1=17.3 · baseline=0.915789 · baseline_scope=cell · cusum_h=3.5194 · cusum_p0=0.029 · cusum_p1=0.184 · delta=0.100000 · lower=0.829462 · n=34 · newcombe_diff_lower=-0.090308 · threshold=0.815789 · x=33 | [0.829462, 0.998463] | 0.815789 | 0.915789 |
| 527 | L-20261015-01 | L1 → L0 | CUSUM | window_reset=True | – | – | – |

CUSUM signals:

| seq | run_id | level | S | h | outcomes monitored | change point |
|---|---|---|---|---|---|---|
| 526 | L-20261015-01 | L1 | 3.6953 | 3.5194 | 82 | 80 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### VAGUE_COMPLAINT\|V2

Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human baseline − δ):

| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | human baseline |
|---|---|---|---|---|---|---|---|
| 514 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=514.9 · arl1=25.1 · baseline=0.953125 · baseline_scope=cell · cusum_h=2.9879 · cusum_p0=0.033 · cusum_p1=0.147 · delta=0.100000 · lower=0.876433 · n=61 · newcombe_diff_lower=-0.079156 · threshold=0.853125 · x=59 | [0.876433, 0.994301] | 0.853125 | 0.953125 |
| 516 | L-20261015-01 | L1 → L0 | CUSUM | window_reset=True | – | – | – |
| 523 | L-20261015-01 | L0 → L1 | PROMOTE | arl0=561.4 · arl1=19.5 · baseline=0.953125 · baseline_scope=cell · cusum_h=3.3918 · cusum_p0=0.022 · cusum_p1=0.147 · delta=0.100000 · lower=0.870302 · n=46 · newcombe_diff_lower=-0.084903 · threshold=0.853125 · x=45 | [0.870302, 0.998864] | 0.853125 | 0.953125 |

CUSUM signals:

| seq | run_id | level | S | h | outcomes monitored | change point |
|---|---|---|---|---|---|---|
| 515 | L-20261015-01 | L1 | 3.8546 | 2.9879 | 9 | 1 |

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### VAGUE_COMPLAINT\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### WRONG_ITEM\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 11 · blockers N_MIN, NON_INFERIORITY

### WRONG_ITEM\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 5 · blockers N_MIN, NON_INFERIORITY

### WRONG_ITEM\|V3

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 2 · blockers N_MIN, NON_INFERIORITY

### WRONG_ITEM\|V4

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 1 · blockers N_MIN, NON_INFERIORITY

### WRONG_VARIANT\|V1

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 5 · blockers N_MIN, NON_INFERIORITY

### WRONG_VARIANT\|V2

Final (ladder rebuilt from run R-20261016-01's DECISION entries): L0 · n 5 · blockers N_MIN, NON_INFERIORITY
