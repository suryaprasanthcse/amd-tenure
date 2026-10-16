---
marp: true
title: TENURE
paginate: true
---

# TENURE
Earned autonomy for customer-resolution agents
Gemma-4-31B-IT on the AMD Token Factory · AMD Developer Hackathon: ACT III · Track 3: Reinvent Commerce

---

# The problem
- A refund agent that is trusted too little saves nothing: a person re-reads every case.
- One trusted too much turns a policy change into silent losses.
- Static guardrails are fixed rules. They do not learn and they do not expire.

---

# The idea: autonomy is earned, per cell
- Cell = case type × order-value band (14 types × 4 bands).
- L0 human decides, agent shadows · L1 suggests · L2 acts with audits · L3 acts, light audit.
- A cell is promoted only when its agreement with the decision is statistically non-inferior to the human team's own.

---

# How a cell is promoted
- Wilson continuity-corrected lower bound on agreement ≥ human baseline − δ.
- Minimum sample, valid-output rate, and cost-weighted error bound per level.
- The model never grants itself trust: the gate is `src/tenure/ladder.py` and `src/tenure/stats.py`.

---

# How a cell loses trust
- Bernoulli CUSUM on each promoted cell's labelled outcomes (`src/tenure/cusum.py`).
- S climbs on disagreements; when S ≥ h the cell demotes itself, no human in the loop.
- Example: POSITIVE|V3, S 3.19 ≥ h 2.64 after 289 outcomes.

---

# Verifiable by anyone
- Hash-chained SHA-256 ledger of every decision, promotion and demotion.
- `python -m tenure.dashboard verify`: 1,038 entries, head seq 1038, re-checked offline.
- Export and anchor: `exports/R-20261016-01/{ledger.jsonl,head.json}`.

---

# What is real, what is simulated

| Layer | Figures |
|---|---|
| Real: Gemma-4-31B-IT on the AMD Token Factory | 482/500 agree (96.4%), 95% CI [0.943, 0.978], 500/500 valid after retrying 17 endpoint failures (first pass 466/500). POSITIVE|V1 and POSITIVE|V2 promoted to L1 |
| Simulated: 6,454 cases, 60 days, mock agent | Promotions, 3 CUSUM demotions: ladder mechanics, not model accuracy |
| Assumed: priced with labelled assumptions | €7,494 human-only · €693 net saving · 45.1% offload per month |

---

# Business impact (assumptions, adjustable)
- Handling time 6.3 min, wage €22/h, error cost €25, GPU €2/h.
- Net €693 per month; 45.1% of cases need no person after audits; mean resolution 49.4% faster.
- Not a claim about real merchant economics.

---

# AMD Token Factory telemetry (measured)
- Endpoint `developer.amd.com.cn`, Gemma-4-31B-IT, shared 20 requests/min limit.
- Replay: 552 cases/h, 431 prompt tok/s, 11.2 completion tok/s, 93% prompt-cache hit, p50 74.7 s (queueing on the rate limit, mobile link).
- S2 spike, concurrency 1: p50 2.6 s, p95 10.9 s. No GPU counters on this profile.

---

# Architecture and next steps
- Olist cases → Gemma decider (AMD Token Factory) → ladder + CUSUM → hash-chained ledger → recorded dashboard.
- Offline gates run on a mock endpoint (D-offline-gates); live re-run pending.
- Next: finish the replay on a stable link; serve the decider on our own vLLM endpoint on AMD Developer Cloud.
