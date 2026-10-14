# Refund Policy v1 · clause digest

Clause tables of `policy/refund_rules_v1.md` only, for the TX1 fine-tuning lane. The full policy file is authoritative.

## 1. Escalations: action `ESCALATE` (always checked first)

| ID | Applies when |
|---|---|
| E1 | `fraud_flag` is true |
| E2 | `legal_threat` is true (consumer agency, complaint site, lawyer, lawsuit) |
| E3 | `order_total_eur` is above 500.00, unless the category is POSITIVE |
| E4 | Category is BILLING (double charge, invoice, chargeback) |
| E5 | Category is NOT_RECEIVED, but the order is delivered (carrier dispute) |
| E6 | Category is OTHER |

## 2. Categories without compensation

| ID | Category | Action |
|---|---|---|
| N1 | POSITIVE | `NO_ACTION`: thank the customer |
| N2 | SELLER_SERVICE | `NO_ACTION`: apologise and forward to the seller |
| I2 | VAGUE_COMPLAINT | `REQUEST_INFO`: ask what went wrong |

## 3. Orders not yet delivered

| ID | Applies when | Action |
|---|---|---|
| F3 | DELAY or NOT_RECEIVED, and `days_past_estimate` is more than 5 | `REFUND_FULL` |
| N3 | DELAY or NOT_RECEIVED, and `days_past_estimate` is 5 or less | `NO_ACTION`: share tracking, no compensation yet |
| I3 | Any other category | `REQUEST_INFO`: the claim concerns goods not yet delivered |

## 4. Delivered orders

**4.1 Late delivery** (category DELAY)

| ID | Applies when | Action |
|---|---|---|
| F1 | `days_late` is more than 5 | `REFUND_FULL`; no return needed |
| P1 | `days_late` is 1 to 5 (minor delay) | `REFUND_PARTIAL` of `freight_total_eur`. If freight is 0.00, refund 5% of `items_total_eur` instead |
| D2 | `days_late` is 0 or less (on time) | `DENY` |

**4.2 Wrong, missing or damaged goods**

| ID | Applies when | Action |
|---|---|---|
| F2 | DAMAGED and `photo_evidence` is true | `REFUND_FULL`; no return needed |
| I1 | DAMAGED and `photo_evidence` is false | `REQUEST_INFO`: ask for a photo |
| R1 | WRONG_ITEM (a different product arrived) | `RESHIP` the correct item; `requires_return` true |
| R2 | MISSING_ITEM | `RESHIP` the missing item |
| P2 | WRONG_VARIANT (the right product, wrong colour or size) | `REFUND_PARTIAL` of 20% of `items_total_eur`; the customer keeps the item |

**4.3 Defects, quality and change of mind** (30-day window)

| ID | Applies when | Action |
|---|---|---|
| F4 | DEFECTIVE, within 30 days | `REFUND_FULL`; `requires_return` true |
| P3 | QUALITY, within 30 days | `REFUND_PARTIAL` of 10% of `items_total_eur` |
| F5 | REMORSE, within 30 days | `REFUND_FULL`; `requires_return` true |
| D3 | DEFECTIVE or QUALITY, after 30 days | `DENY`: refer to the manufacturer's warranty |
| D1 | REMORSE, after 30 days | `DENY` |
