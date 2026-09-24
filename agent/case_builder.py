"""
Assembles the exact answer JSON the submission format requires
(data/HHGOA_IEEE/DATASET_README.md, "Answer Format") from an
investigation's evidence, assessments, and actions.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from agent.assess import Assessment
from agent.evidence import CaseEvidence


def _affected_txn_ids(ev: CaseEvidence, a: Assessment) -> list[str]:
    if a.verdict != "fraud":
        return []
    ids = {ev.flagged_txn["TransactionID"]}
    if a.pattern == "card_testing" and len(ev.card_window_1h) > 0:
        small = ev.card_window_1h[ev.card_window_1h["TransactionAmt"] < 5]
        larger = ev.card_window_1h[ev.card_window_1h["TransactionAmt"] > 100]
        ids |= set(small["TransactionID"].tolist()) | set(larger["TransactionID"].tolist())
    return sorted(ids)


def _first_suspicious_txn_id(ev: CaseEvidence, affected: list[str]) -> str:
    if not affected:
        return ""
    if len(affected) == 1:
        return affected[0]
    sub = ev.card_window_1h[ev.card_window_1h["TransactionID"].isin(affected)]
    if len(sub) > 0:
        return sub.sort_values("ts").iloc[0]["TransactionID"]
    return ev.flagged_txn["TransactionID"]


def _exposure(ev: CaseEvidence, affected: list[str]) -> float:
    if not affected:
        return 0.0
    sub = ev.card_window_1h[ev.card_window_1h["TransactionID"].isin(affected)]
    covered = set(sub["TransactionID"].tolist())
    total = sub["TransactionAmt"].abs().sum()
    missing = set(affected) - covered
    if ev.flagged_txn["TransactionID"] in missing:
        total += abs(ev.flagged_txn.get("TransactionAmt", 0))
    return round(float(total), 2)


def _clean_signal(s: str) -> str:
    # signals carry bracketed tags like "[card_testing] ..." for internal
    # tracing; strip them and trim for prose use in the summary/narrative.
    if s.startswith("["):
        s = s.split("]", 1)[-1].strip()
    return s[:1].upper() + s[1:] if s else s


def _summary(ev: CaseEvidence, a: Assessment, affected: list[str], exposure: float) -> str:
    lead = {
        "fraud": f"Assessed as {a.pattern.replace('_', ' ')} at {a.fraud_probability:.0%} confidence.",
        "legitimate": f"Assessed as legitimate at {1 - a.fraud_probability:.0%} confidence.",
        "uncertain": f"Verdict remains uncertain at {a.fraud_probability:.0%} confidence.",
    }[a.verdict]
    details = [_clean_signal(s) for s in a.signals[:3]]
    detail = ". ".join(d.rstrip(".") for d in details if d) + "." if details else ""
    tail = f" Exposure ${exposure:,.2f} across {len(affected)} transaction(s)." if affected else ""
    return f"{lead} {detail}{tail}".strip()


def _sar_narrative(ev: CaseEvidence, a: Assessment, affected: list[str], exposure: float,
                    activity_dates: list[str], reason: str) -> str:
    txn = ev.flagged_txn
    who = f"Customer {ev.customer_id}, card {ev.card_id}"
    what = (f"{len(affected)} transaction(s) totaling ${exposure:,.2f}, pattern: "
            f"{a.pattern.replace('_', ' ') if a.pattern != 'undocumented' else 'an undocumented but coordinated pattern'}.")
    when = f"Activity dated {activity_dates[0]} to {activity_dates[1]}." if activity_dates else ""
    where = f"Channel: {ev.channel}; billing region {txn.get('addr1')}."
    how = " ".join(a.signals[:4])
    why = f"Filed per policy: {reason}."
    return f"{who}. {what} {when} {where} How: {how} {why}".strip()


def build_answer(
    case_id: str,
    ev: CaseEvidence,
    final_assessment: Assessment,
    initial_actions: list[dict],
    final_actions: list[dict],
    what_changed: str,
    evidence_requests: list[dict],
    stop_reason: str,
    tool_calls: int,
    tokens: int,
    latency_s: float,
    written_to_graph: bool,
    graph_case_id: str,
    sar_file: bool,
    sar_reason: str,
) -> dict:
    a = final_assessment
    affected = _affected_txn_ids(ev, a)
    exposure = _exposure(ev, affected)
    first_txn = _first_suspicious_txn_id(ev, affected)

    connected_card_ids = sorted(set(ev.ring_device_cards) - {ev.card_id})
    connected_device_profiles = [ev.device_profile] if (ev.device_profile and connected_card_ids) else []

    evidence_list = [
        {"claim": it.claim, "source": it.source, "ref": it.ref, "entity_ids": it.entity_ids}
        for it in ev.items
    ]
    similar_prior_cases = sorted(set(
        [c["case_id"] for c in ev.similar_closed_cases]
        + [c["case_id"] for c in ev.narrative_similar_cases]
    ))

    status = {
        "fraud": "closed_fraud", "legitimate": "closed_legitimate", "uncertain": "escalated",
    }[a.verdict]

    case_part = {
        "status": status,
        "verdict": a.verdict,
        "fraud_probability": a.fraud_probability,
        "pattern": a.pattern,
        "pattern_description": a.pattern_description,
        "affected_txn_ids": affected,
        "first_suspicious_txn_id": first_txn,
        "connected_card_ids": connected_card_ids,
        "connected_device_profiles": connected_device_profiles,
        "exposure_usd": exposure,
        "evidence": evidence_list,
        "similar_prior_cases": similar_prior_cases,
        "summary": _summary(ev, a, affected, exposure),
        "written_to_graph": written_to_graph,
        "graph_case_id": graph_case_id if written_to_graph else "",
    }

    if sar_file:
        ts = ev.flagged_txn.get("ts")
        dates = [ts.strftime("%Y-%m-%d"), ts.strftime("%Y-%m-%d")] if isinstance(ts, pd.Timestamp) else []
        sar_part = {
            "file": True,
            "reason": sar_reason,
            "narrative": _sar_narrative(ev, a, affected, exposure, dates, sar_reason),
            "subjects": sorted({ev.customer_id, ev.card_id, *connected_card_ids}),
            "total_amount_usd": exposure,
            "activity_dates": dates,
        }
    else:
        sar_part = {"file": False, "reason": sar_reason, "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []}

    return {
        "case_id": case_id,
        "case": case_part,
        "evidence_requests": evidence_requests,
        "next_best_actions": {"initial": initial_actions, "final": final_actions, "what_changed": what_changed},
        "sar": sar_part,
        "stop_reason": stop_reason,
        "tool_calls": tool_calls,
        "tokens": tokens,
        "latency_s": round(latency_s, 2),
    }
