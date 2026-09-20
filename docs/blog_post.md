# Investigating Fraud, With a Graph as Memory

*Built for the TigerGraph × Hacker House Goa 2026 fraud investigation track.*

**Live version:** https://claude.ai/artifact/6eK2zbWi6iFH1ohsAs1Y3H

---

Building an agent that triages twenty real, unlabeled fraud alerts — and the two calibration bugs that nearly made it call almost everything fraud.

## What we built

Fraud teams work from a trigger — a risk score, a customer complaint, an analyst's hunch — and have to turn it into a defensible decision before the money is gone. We built an agent that does that end to end against a real dataset: 590,742 card transactions, 144,432 device/identity records, a history of 5,565 closed investigations, and twenty live alerts with no answer key.

For each alert the agent gathers evidence from the transaction ledger and from prior cases, decides whether it's actually looking at fraud, picks a pattern from five documented types (or names its own when nothing fits), recommends actions with the correct approval routing, decides whether a Suspicious Activity Report is required, and — when the evidence is too thin to act on — asks a question and revises its answer once it "hears back." Every case gets written into TigerGraph as a permanent record the next investigation can retrieve.

> The dataset ships with no fraud/not-fraud column by design. `closed_cases_history.csv` (July–October, 5,565 cases) is the only ground truth — that's the agent's case memory. `case_pack.csv` (twenty alerts, November–December) is deliberately unlabeled: deciding those, with evidence, *is* the task.

## Architecture

One decision shaped everything downstream: `transactions.csv` is 708MB across 397 columns. Loading all of it into a shared TigerGraph evaluation workspace isn't practical, and it isn't actually what an investigation needs — what it needs is fast lookups over the raw ledger and a durable, queryable record of what each investigation *found*. So the system splits in two:

- **A local transaction store** — a pandas index over the real CSVs, built once (~7 seconds), used for velocity windows, shared-device detection, and customer spending baselines.
- **TigerGraph as curated memory** — every customer and card observed in the data, the full closed-case history, and an `InvestigationCase` vertex with its evidence trail for every case the agent actually opens, written as it happens.

Schema: `Customer`, `InvCard`, `Txn`, `DeviceProfile`, `BillingRegion`, `EmailDomain`, `ClosedCase`, `InvestigationCase`.

The investigation loop itself is a straight pipeline:

```
trigger -> gather evidence -> assess -> (if uncertain) simulate a response -> reassess
    -> recommend actions, routed auto / L1 / L2 -> file a report if policy requires one
    -> write the case into TigerGraph -> emit the required answer JSON
```

## How TigerGraph is used

Deploying the schema surfaced three version-specific quirks worth naming: vertex and edge type names are **global** across the database, not scoped per graph, so `Card` collided with a pre-existing demo dataset in the same workspace; querying a vertex by its own primary ID requires `PRIMARY_ID_AS_ATTRIBUTE="true"` explicitly, or GSQL refuses to type-check `t.transaction_id`; and `proxy` is a reserved identifier, which broke a vertex attribute we didn't expect to be contentious.

Every closed case connects to its card and, where the data supports it, to other cards implicated in the same fraud ring. When an investigation opens, it queries that graph for cases already sitting on the same card, then cross-references the raw ledger for cases sharing this transaction's device fingerprint or billing region — and writes what it finds back as a new `InvestigationCase` node with edges to every transaction, card, device, and prior case it actually used. That's the case memory: not a static export, a graph that grows one real investigation at a time.

## The agentic capabilities

**Knowing when to ask instead of guess.** Rule R1 is explicit: a single weak signal below 70% confidence — including the bank's own risk score, taken alone — isn't enough to block a card. When that's all the agent has, it requests customer verification or step-up authentication instead of acting. No real customer channel exists for this benchmark, so the response is simulated and the assumption is written down verbatim in the answer file rather than hidden inside a probability. An `analyst_request` trigger gets different treatment: there's no customer to ask at all, so the simulated follow-up instead asks whether the ring the analyst flagged shows its own fraud history in the graph — and the verdict only moves if it does.

**Actions that change as evidence arrives.** Every case records its recommendation twice: `initial`, before any evidence request, and `final`, after. One case in the pack started at `VERIFY_WITH_CUSTOMER` on a 45% probability from a lone risk score; a simulated denial moved it to `BLOCK_CARD + CREATE_CASE` at 92%. Nothing about the policy required guessing at that gap — R1 and R2 specify it directly.

**Reasoning is swappable, not baked in.** A rule-based assessor runs by default — deterministic, and every probability adjustment traces to a specific piece of evidence. When `ANTHROPIC_API_KEY` is set, the same evidence and the same policy text route to Claude instead, through an identical interface.

## What we learned

The dataset README says it plainly: *"Half the cases are legitimate. An agent that blocks everything scores badly."* A first full run came back 18 of 20 cases as fraud. That's not a rounding error, that's a broken agent.

Rather than hand-tune against twenty cases with no answer key — which is just overfitting to guesses — we measured. We ran the assessor against forty *random, non-flagged* transactions and checked how often it falsely called fraud on ordinary activity: **42.5% false positive rate, before the fix. 2.5%, after.**

Two bugs accounted for nearly all of it:

| Signal | What we assumed | What the data showed |
|---|---|---|
| `is_new_device` | A meaningful fraud indicator | Fires on **50%** of random transactions — a coin flip, and it was feeding two separate detectors that got summed, double-counting the same weak signal |
| Shared device fingerprint | Specific and rare, a real ring signal | **49%** of fingerprints are shared by more than one card — some by over 1,000, from a fingerprint built on device model, OS, and browser rather than a true unique device ID |

The fix wasn't a threshold tweak. `is_new_device` now only counts as a modifier on top of an already-corroborated signal, matching what the dataset's own pattern documentation says almost verbatim — *"stronger than pattern 2, still not proof: people buy new phones."* Device-sharing only counts as a ring signal when the fingerprint is genuinely rare, or when it's common but the connected cards carry their own confirmed-fraud history.

> The most useful test wasn't the twenty cases we were graded on. It was the forty we weren't.

## What we'd improve with more time

- **Proper statistical baselining for regional signals.** Billing-region overlap is currently treated as informational only, because this dataset's base fraud rate (84% of closed cases are confirmed fraud) makes naive overlap counts meaningless without a real expected-traffic baseline per region.
- **A true device identifier.** The available fields (device model, OS, browser, screen) can't distinguish two different phones of the same model. A rarity-plus-corroboration filter is a reasonable stand-in, not a fix.
- **Validate the LLM path against the rule-based one.** Both are wired and share an interface; we haven't yet run the same twenty cases through both to see where a language model reasons past the rule-based assessor's blind spots.
- **GraphRAG over the actual regulatory documents.** The policy's SAR narrative rules are followed directly, but the FinCEN/FATF reference PDFs the brief points to were never indexed for retrieval — that's real GraphRAG surface area left on the table.
