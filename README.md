# TENURE

Earned autonomy for customer-resolution agents on AMD Instinct and Gemma.

TENURE lets an AI refund agent earn autonomy one cell (case type × order-value band) at a time. A cell is promoted only when statistics show the agent is as consistent as the human team. The agent demotes itself when it drifts, and every decision is recorded in a hash-chained ledger. The design is in [`TENURE_master_blueprint.md`](TENURE_master_blueprint.md) and the build plan is in [`RUNBOOK_TENURE.md`](RUNBOOK_TENURE.md).

> Status: under construction for the AMD Developer Hackathon: ACT III (lablab.ai). Later sections are filled in from measured results as the build progresses.

## AI & Human Collaboration Disclosure

This project is built by a human developer working with an AI coding assistant. We disclose this in line with the hackathon's transparency rules.

| Item | Detail |
|---|---|
| Human author | Surya Prasanth R: sole author and committer of this repository |
| AI assistant | Claude Code, running Anthropic Claude models (Claude Opus) |
| What the AI did | Helped draft the master blueprint and the build runbook; generates code, tests and documentation module by module (T00–T08) under a test-first contract |
| What the human did | Chose the problem and product direction; owns the refund policy (`policy/refund_rules_v1.md`); makes every recorded decision in `.agent/DECISIONS.md`; reviews, runs and accepts each module's gate; labels the 300-case gold set; records the video and submits |
| Verification | Every module must pass an automated gate (`scripts/gate.py`) before it is accepted. Numbers in the README, slides and video come from run manifests and the ledger, never typed by hand |
| Session log | `.agent/DEVLOG.md` records each AI-assisted session: module, model, times and gate result |

The application uses Google's Gemma models, served on AMD Instinct GPUs, as its decision agent at runtime. That use is a feature of the product and is separate from the development assistance described above.
