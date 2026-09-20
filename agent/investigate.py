"""
The investigation loop for one case_pack.csv row: gather evidence,
assess, decide whether to simulate a response (per policy section 5:
"Customer and analyst replies are not provided ... simulate the
response ... and state the assumption you made"), reassess, decide
final actions, and assemble the answer JSON (agent.case_builder).

Simulation assumption (documented here since it's a real modeling
choice, not a given fact): for a customer-facing verification
(VERIFY_WITH_CUSTOMER / STEP_UP_AUTH), the simulated response tracks
the *initial* assessed fraud_probability -- >= 0.5 simulates a denial,
< 0.5 simulates a confirmation. For an analyst_request case, there's no
customer to ask at all -- the simulated follow-up instead asks the
analyst to confirm whether the shared-device ring they flagged shows
its own fraud history, and the verdict moves only if they do. Both are
deliberately consistent with evidence already gathered rather than a
coin flip, while staying honest that no real reply exists for this
benchmark.
"""
from __future__ import annotations

import time

from agent import assess, case_builder, policy
from agent.assess import Assessment
from agent.config import CONFIG
from agent.evidence import gather
from agent.graph_client import GraphClient
from agent.transaction_store import TransactionStore


def _assess(ev, trigger_type, trigger_text, bank_risk_score, stage, customer_confirms=None):
    """Routes to the LLM reasoner when ANTHROPIC_API_KEY is set, else the
    rule-based one; both share the same Assessment shape (agent.assess.Assessment)."""
    if CONFIG.anthropic_api_key:
        from agent.llm_assess import llm_assess
        return llm_assess(ev, trigger_type, trigger_text, bank_risk_score, stage, customer_confirms)
    return assess.assess(ev, trigger_type, trigger_text, bank_risk_score, stage, customer_confirms), 0


def _finalize(graph, case_id, ev, initial_actions, final_assessment, what_changed,
              evidence_requests, tool_calls, tokens_used, t0) -> dict:
    final_exposure = case_builder._exposure(ev, case_builder._affected_txn_ids(ev, final_assessment))
    final_actions = assess.recommend_actions(final_assessment, final_exposure, ev.ring_device_cards)

    if final_assessment.verdict == "fraud":
        sar_file, sar_reason = policy.sar_required(final_assessment.pattern, final_exposure, bool(ev.ring_device_cards))
    else:
        sar_file, sar_reason = False, "verdict is not fraud"

    p = final_assessment.fraud_probability
    if evidence_requests:
        stop_reason = "Simulated verification response settled the verdict; no further evidence would change the action."
    elif final_assessment.verdict == "fraud" and p >= 0.85:
        stop_reason = f"Fraud probability {p:.2f} >= 0.85 with corroborating evidence."
    elif final_assessment.verdict == "legitimate" and p <= 0.15:
        stop_reason = f"Fraud probability {p:.2f} <= 0.15 with corroborating evidence."
    elif final_assessment.verdict in ("fraud", "legitimate"):
        stop_reason = (
            f"Verdict ({final_assessment.verdict}) reached at probability {p:.2f} on "
            f"{len(final_assessment.signals)} corroborating signal(s); further evidence-gathering is unlikely "
            f"to change a decisive action here."
        )
    else:
        stop_reason = f"Verdict remains uncertain at probability {p:.2f}; escalating per R8 rather than acting on unresolved evidence."

    answer = case_builder.build_answer(
        case_id=case_id, ev=ev, final_assessment=final_assessment,
        initial_actions=initial_actions, final_actions=final_actions, what_changed=what_changed,
        evidence_requests=evidence_requests, stop_reason=stop_reason,
        tool_calls=tool_calls, tokens=tokens_used, latency_s=time.time() - t0,
        written_to_graph=True, graph_case_id=case_id,
        sar_file=sar_file, sar_reason=sar_reason,
    )

    try:
        graph.write_investigation(answer)
        tool_calls += 1
    except Exception as e:
        answer["case"]["written_to_graph"] = False
        answer["case"]["graph_case_id"] = ""
        print(f"[investigate_case] WARNING: failed to write {case_id} to graph: {e}")

    answer["tool_calls"] = tool_calls
    answer["latency_s"] = round(time.time() - t0, 2)
    return answer


def investigate_case(store: TransactionStore, graph: GraphClient, case_row: dict) -> dict:
    t0 = time.time()
    tool_calls = 0
    tokens_used = 0

    case_id = case_row["case_id"]
    trigger_type = case_row["trigger_type"]
    trigger_text = case_row["trigger_text"]
    flagged_txn_id = str(case_row["flagged_txn_id"])
    card_id = case_row["card_id"]
    customer_id = case_row["customer_id"]
    bank_risk_score = case_row.get("risk_score")
    bank_risk_score = float(bank_risk_score) if bank_risk_score not in (None, "") and bank_risk_score == bank_risk_score else None

    ev = gather(store, flagged_txn_id, card_id, customer_id)
    tool_calls += 5  # get_transaction, card_window, device_neighbors, region_neighbors, similar_closed_cases

    initial_assessment, t = _assess(ev, trigger_type, trigger_text, bank_risk_score, "before_additional_evidence")
    tokens_used += t
    initial_exposure = case_builder._exposure(ev, case_builder._affected_txn_ids(ev, initial_assessment))
    initial_actions = assess.recommend_actions(initial_assessment, initial_exposure, ev.ring_device_cards)

    if not initial_assessment.needs_more_evidence:
        return _finalize(graph, case_id, ev, initial_actions, initial_assessment, "nothing",
                          [], tool_calls, tokens_used, t0)

    req_type = initial_assessment.evidence_request_type

    if req_type == "analyst_info":
        # Not a fraud/no-fraud question -- an analyst_request case is
        # already an internal tip (e.g. "several cards share a device
        # profile"). The simulated follow-up asks the analyst to confirm
        # whether the ring they flagged shows its own fraud history; the
        # customer_confirms binary below (built for a direct "did you make
        # this purchase" question) doesn't apply here.
        analyst_corroborates = bool(ev.ring_device_cards)
        assumed = (
            f"Analyst confirms {len(ev.ring_device_cards)} connected card(s) on this device profile show "
            f"their own confirmed-fraud history." if analyst_corroborates else
            "Analyst has no further corroborating detail beyond the original tip."
        )
        evidence_requests = [{
            "type": req_type, "asked_after_step": 1,
            "assumed_response": assumed + " (Simulated: no real analyst channel exists for this benchmark.)",
        }]
        tool_calls += 1

        if analyst_corroborates:
            final_assessment = Assessment(
                verdict="fraud", fraud_probability=max(0.75, initial_assessment.fraud_probability),
                pattern=initial_assessment.pattern or "undocumented",
                pattern_description=initial_assessment.pattern_description,
                signals=initial_assessment.signals + ["analyst corroborated the shared-device ring"],
            )
            what_changed = "Analyst corroboration of the shared-device ring moved the verdict to fraud."
        else:
            final_assessment = initial_assessment
            what_changed = "nothing -- analyst had no further detail beyond the original tip"

        return _finalize(graph, case_id, ev, initial_actions, final_assessment, what_changed,
                          evidence_requests, tool_calls, tokens_used, t0)

    customer_confirms = initial_assessment.fraud_probability < 0.5
    assumed = (
        "Customer states they did not make this transaction and still has the card in their possession."
        if not customer_confirms else
        "Customer confirms they made this transaction themselves."
    )
    evidence_requests = [{
        "type": req_type,
        "asked_after_step": 1,
        "assumed_response": assumed
        + " (Simulated: no real customer channel exists for this benchmark; the assumption tracks "
          "the initial assessed fraud probability -- see agent/investigate.py docstring.)",
    }]
    tool_calls += 1

    final_assessment, t = _assess(
        ev, trigger_type, trigger_text, bank_risk_score, "after_additional_evidence", customer_confirms
    )
    tokens_used += t
    what_changed = (
        f"Assumed customer {'denial' if not customer_confirms else 'confirmation'} moved fraud_probability "
        f"from {initial_assessment.fraud_probability:.2f} to {final_assessment.fraud_probability:.2f}, "
        f"changing verdict from {initial_assessment.verdict} to {final_assessment.verdict}."
    )

    return _finalize(graph, case_id, ev, initial_actions, final_assessment, what_changed,
                      evidence_requests, tool_calls, tokens_used, t0)
