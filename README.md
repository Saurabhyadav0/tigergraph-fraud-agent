# Agentic Fraud Investigation Agent — HHGOA_IEEE / TigerGraph

An AI agent that investigates fraud alerts against the real **HHGOA_IEEE** dataset,
builds and writes cases into TigerGraph as case memory, decides when it needs more
evidence, and recommends next-best-actions under the bank's fraud policy — producing
the exact answer format the benchmark requires for all 20 cases in `case_pack.csv`.

## Architecture

```
case_pack.csv row (trigger: risk_score | customer_report | analyst_request)
        v
  agent/investigate.py    <- orchestrates one case end to end
        |
        +-- agent/evidence.py        gather evidence from the real data:
        |                            - agent/transaction_store.py: a pandas-backed
        |                              index over the full 590K transactions.csv +
        |                              144K identity.csv (card velocity, shared
        |                              device/region detection, customer history)
        |                            - closed-case memory: a LIVE GSQL traversal
        |                              against TigerGraph (graph/queries/
        |                              similar_closed_cases.gsql), exposed both as
        |                              a direct pyTigerGraph call (agent/graph_client.py,
        |                              the low-latency path for a 20-case run) and
        |                              as an MCP tool (agent/mcp_server.py, for any
        |                              MCP-compatible agent/client)
        |
        +-- agent/assess.py           rule-based fraud assessment (or
        |   agent/llm_assess.py       agent/llm_assess.py -> Claude, when
        |                             ANTHROPIC_API_KEY is set) -- verdict,
        |                             fraud_probability, pattern, R1-R10 citations
        |
        +-- agent/policy.py           the exact action catalog, auto/L1/L2
        |                             approval routing, and SAR trigger rules
        |                             from the dataset's own Fraud Policy
        |
        +-- agent/case_builder.py     assembles the exact required answer JSON
        |                             (case / sar / next_best_actions / ...)
        |
        +-- agent/graph_client.py     writes the InvestigationCase + its edges
                                      into TigerGraph -- the case memory the next
                                      investigation retrieves via evidence.py
```

## Why TigerGraph holds curated case memory, not a raw data mirror

`transactions.csv` is 590,742 rows / 397 columns (~708MB). Bulk-loading all of it
into a shared Savanna evaluation workspace isn't practical, and isn't actually what
the investigation needs: what matters is fast lookups and pattern scans over the raw
ledger, and a persistent, queryable record of what each investigation *found*.

So: `agent/transaction_store.py` is a fast local pandas index over the real CSVs
(loads in ~7s) used for velocity windows, shared-device/region detection, and
customer history. **TigerGraph holds**: every `Customer`/`InvCard` observed in the
data (13,553 customers, 13,568 cards), the full `closed_cases_history.csv` (5,565
cases) as case memory, and an `InvestigationCase` vertex + edges for every case the
agent actually investigates — written as it happens, exactly matching the brief's
"write it into the graph... the next investigation should be able to find it."

## TigerGraph MCP

`agent/mcp_server.py` exposes two of the graph operations above as real MCP tools
(built on the official `mcp` SDK, tested against the live instance):
`similar_closed_cases(card_id)` (the GSQL traversal) and
`write_investigation_case(answer_json)`. Run it standalone with
`python -m agent.mcp_server`, or point any MCP client (Claude Desktop, `mcp dev`,
another agent) at it to give it the same graph capabilities this pipeline uses.

`agent/investigate.py`'s own loop calls `agent/graph_client.py` directly (a Python
import, not a network hop, for a tight local loop over 20 cases) rather than
round-tripping through its own MCP server — the MCP server is this project's
integration surface for *other* agents/tools, exercising the same underlying
`GraphClient` methods, not a second implementation of them.

## The pipeline in one run

```
trigger (case_pack.csv row)
  -> gather evidence: card velocity window, shared-device ring detection,
     shared-region detection (corroborated by fraud history, not raw traffic --
     see agent/assess.py's _ring_signal docstring for why plain region overlap
     is too noisy in this dataset), customer purchase-history baseline,
     case-memory retrieval (closed cases on this card / device / region)
  -> assess: verdict, fraud_probability, pattern, citing R1-R10
  -> if R1 applies (single weak signal, probability < 0.70): simulate a
     VERIFY_WITH_CUSTOMER / STEP_UP_AUTH response (policy explicitly allows
     this -- no real customer channel exists for the benchmark; the
     assumption is documented, not a coin flip -- see agent/investigate.py)
  -> reassess with the simulated evidence
  -> recommend next-best-actions (auto/L1/L2 routed), decide if a SAR is
     required (policy 3a), and why
  -> write the case + evidence trail into TigerGraph
  -> emit cases/<case_id>.json in the exact required answer format
```

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# .env: TG_HOST, TG_SECRET, TG_GRAPH_NAME already set up against a live
# Savanna instance (see graph/schema.gsql for what's deployed there).
# ANTHROPIC_API_KEY is optional -- unset, the rule-based assessor in
# agent/assess.py runs instead of agent/llm_assess.py.

python scripts/build_card_index.py          # once: derive card_id labels
python scripts/load_graph_foundation.py     # once: load customers/cards/closed cases

python scripts/run_case_pack.py             # all 20 cases -> cases/*.json
python scripts/run_case_pack.py HHG-003     # a single case, for iterating

streamlit run ui/app.py                     # analyst dashboard
```

## Calibration: a real finding, not a footnote

The dataset README explicitly warns "half the cases are legitimate... an agent
that blocks everything scores badly." An early full run scored 18/20 fraud --
clearly miscalibrated. Rather than hand-tune against the unlabeled case pack
(which would just be overfitting to guesses), the fix was to measure: run the
assessor against 40 *random, non-flagged* transactions and check how often it
falsely called fraud on ordinary activity. That surfaced two real bugs:

1. `is_new_device` (id_15=='New') fires on **50% of random transactions** --
   a coin flip, not a signal -- yet two detectors both keyed off it
   independently and got summed, double-counting the same weak evidence.
2. The device-fingerprint "shared device" ring signal is coarse (device
   model + OS + browser + screen, no true unique device ID): **49% of
   fingerprints are shared by >1 card**, some by 1000+ (popular phone
   models colliding across unrelated customers, not rings).

Fixing both dropped the background false-positive rate from **42.5% to
2.5%** (see git history / agent/assess.py and agent/transaction_store.py
docstrings for the measurements and the fix). The case-pack distribution
after the fix is still fraud-weighted (17/20) rather than ~50/50 --
plausible, since real alerts aren't random noise and should correlate with
real signals more than the general population, but not fully resolved
without ground truth to validate against further.

## Repo layout

```
graph/schema.gsql        the deployed TigerGraph schema (Customer, InvCard, Txn,
                          DeviceProfile, EmailDomain, BillingRegion, ClosedCase,
                          InvestigationCase + edges) -- see its header comment for
                          two TigerGraph-version-specific gotchas hit while
                          deploying this (PRIMARY_ID_AS_ATTRIBUTE, reserved words)
agent/
  transaction_store.py    fast local index over the real CSVs
  evidence.py              gathers structured evidence for one case
  assess.py                 rule-based fraud assessment (R1-R10)
  llm_assess.py             Claude-based assessment (same interface, used when
                             ANTHROPIC_API_KEY is set)
  policy.py                 action catalog, approval routing, SAR rules
  case_builder.py            assembles the required answer JSON
  graph_client.py             writes investigations into TigerGraph
  investigate.py               orchestrates one case end to end
  config.py                     .env-driven configuration
scripts/
  build_card_index.py       derives card_id labels (not a raw column -- see
                             the script's docstring for why this needed care)
  load_graph_foundation.py  loads customers/cards/closed-case history
  run_case_pack.py           runs the 20 real cases, writes cases/*.json
ui/app.py                 Streamlit analyst dashboard
cases/                    the 20 required answer files
data/HHGOA_IEEE/          the real dataset (DATASET_README.md is the original
                           task spec -- policy, patterns, answer format, cases)
```

## What's real vs. simulated

- Real: the TigerGraph schema and graph writes, the transaction/identity data
  analysis, card-testing/out-of-region/new-device/shared-origin pattern detection,
  closed-case memory retrieval, the policy's approval routing and SAR rules,
  the produced answer JSON.
- Simulated, per the policy's own section 5 (customer and analyst replies aren't
  provided for this benchmark): when the agent requests `VERIFY_WITH_CUSTOMER` or
  `STEP_UP_AUTH`, the response is simulated and the assumption is recorded in
  `evidence_requests[].assumed_response` on every case that needed one — the
  assumption tracks the case's own pre-verification fraud_probability rather than
  being arbitrary (documented in `agent/investigate.py`).
