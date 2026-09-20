"""
LLM reasoning path: same CaseEvidence input and Assessment output shape
as agent.assess.assess(), so agent/investigate.py can use either
transparently. Used only when ANTHROPIC_API_KEY is set (agent/assess.py's
rule-based scorer is the default otherwise) -- per the brief, the LLM
is for reasoning/synthesis/explanation over evidence the graph and
policy already ground, not a replacement for the graph analysis.
"""
from __future__ import annotations

import json

import pandas as pd

from agent import policy
from agent.assess import Assessment
from agent.config import CONFIG
from agent.evidence import CaseEvidence

SYSTEM_PROMPT = f"""You are a fraud investigation analyst agent for a bank, operating strictly under \
the fraud policy below. You are given structured evidence about one flagged transaction, gathered from \
the bank's transaction graph and its closed-case memory. Decide the verdict, exactly as an analyst would, \
citing the specific evidence and policy rule numbers that drove your decision.

## Policy rules (cite by number)
{chr(10).join(f"{k}: {v}" for k, v in policy.RULES.items())}

## Patterns
card_testing, card_not_present_fraud, card_not_present_new_device, out_of_region_use, account_takeover, \
undocumented (evidence shows coordinated/repeated abuse fitting none of the above -- describe it yourself), \
none (no fraud pattern).

## What "Above 0.7 risk score, often legitimate" means
The bank's risk_score is a noisy model input, not a verdict. Weight it accordingly.

Respond ONLY with a JSON object with exactly these keys:
verdict ("fraud"|"legitimate"|"uncertain"), fraud_probability (0.0-1.0), pattern (one of the values above), \
pattern_description (required, else ""), needs_more_evidence (bool -- true only if R1 applies: single weak \
signal, probability < 0.70), evidence_request_type ("customer_validation"|"step_up_auth"|null), \
signals (list of short strings, each citing specific evidence)."""


def _context(ev: CaseEvidence, trigger_type: str, trigger_text: str, bank_risk_score, stage: str) -> str:
    txn = ev.flagged_txn
    lines = [
        f"## Investigation stage: {stage}",
        f"Trigger: {trigger_type} -- {trigger_text}",
        f"Bank risk score: {bank_risk_score}",
        f"\n## Flagged transaction {txn.get('TransactionID')}",
        f"amount=${txn.get('TransactionAmt')}, channel={ev.channel}, product={txn.get('ProductCD')}, "
        f"billing_region={txn.get('addr1')}, ts={txn.get('ts')}",
        f"device_profile={ev.device_profile}, is_new_device={ev.is_new_device}",
        f"\n## Evidence gathered",
    ]
    for item in ev.items:
        lines.append(f"- [{item.source}] {item.claim} (ref: {item.ref})")
    lines.append(f"\ncard_window_1h: {len(ev.card_window_1h)} txns, card_window_48h: {len(ev.card_window_48h)} txns")
    lines.append(f"ring_device_cards: {ev.ring_device_cards}")
    lines.append(f"customer_known_regions: {sorted(ev.customer_known_regions)}")
    lines.append(f"amount_zscore: {ev.amount_zscore}")
    lines.append(f"\n## Similar closed cases (case memory)")
    for c in ev.similar_closed_cases:
        lines.append(f"- {c['case_id']}: {c['outcome']}/{c['pattern']}, ${c['exposure_usd']}: {c['analyst_notes'][:200]}")
    return "\n".join(lines)


def llm_assess(ev: CaseEvidence, trigger_type: str, trigger_text: str, bank_risk_score,
                stage: str, customer_confirms: bool | None = None) -> Assessment:
    import anthropic

    if customer_confirms is True:
        return Assessment(verdict="legitimate", fraud_probability=0.05, pattern="none", pattern_description="",
                           signals=["customer confirmed making the transaction"]), 0

    context = _context(ev, trigger_type, trigger_text, bank_risk_score, stage)
    if customer_confirms is False:
        context += "\n\nThe customer was asked and DENIED making this transaction."

    client = anthropic.Anthropic(api_key=CONFIG.anthropic_api_key)
    msg = client.messages.create(
        model="claude-sonnet-5", max_tokens=1024, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    text = msg.content[0].text
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])

    return Assessment(
        verdict=data["verdict"], fraud_probability=float(data["fraud_probability"]),
        pattern=data["pattern"], pattern_description=data.get("pattern_description", ""),
        signals=data.get("signals", []),
        needs_more_evidence=bool(data.get("needs_more_evidence", False)) and stage == "before_additional_evidence",
        evidence_request_type=data.get("evidence_request_type"),
    ), msg.usage.input_tokens + msg.usage.output_tokens
