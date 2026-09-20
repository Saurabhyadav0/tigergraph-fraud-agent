"""
Rule-based fraud assessment: turns CaseEvidence + the trigger into a
verdict, calibrated fraud_probability, pattern classification, and
policy-cited next-best-actions (R1-R10). This is the reasoning engine
used when no ANTHROPIC_API_KEY is configured; agent/llm_assess.py
wraps the same evidence for the Claude path when a key is present.

Deliberately transparent and documented rather than a black box: every
probability adjustment below is traceable to a specific piece of
evidence, which is what the policy's section 7 ("Explaining") and the
answer format's `evidence` list require anyway.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from agent import policy
from agent.evidence import CaseEvidence


@dataclass
class Assessment:
    verdict: str                 # fraud | legitimate | uncertain
    fraud_probability: float
    pattern: str
    pattern_description: str
    signals: list[str] = field(default_factory=list)
    needs_more_evidence: bool = False
    evidence_request_type: str | None = None  # customer_validation | step_up_auth | analyst_info


def _card_testing_signal(ev: CaseEvidence) -> tuple[bool, float, str]:
    if ev.channel != "online" or len(ev.card_window_1h) < 3:
        return False, 0.0, ""
    small = ev.card_window_1h[ev.card_window_1h["TransactionAmt"] < 5]
    if len(small) < 3:
        return False, 0.0, ""
    larger = ev.card_window_1h[ev.card_window_1h["TransactionAmt"] > 100]
    weight = 0.55 + (0.15 if len(larger) > 0 else 0.0)
    desc = (f"{len(small)} online authorizations under $5 within an hour"
            + (f", followed by a ${larger['TransactionAmt'].max():.2f} purchase" if len(larger) > 0 else ""))
    return True, weight, desc


def _out_of_region_signal(ev: CaseEvidence) -> tuple[bool, float, str]:
    region = ev.flagged_txn.get("addr1")
    if ev.channel != "in_person" or region is None or pd.isna(region) or region in ev.customer_known_regions:
        return False, 0.0, ""
    # home activity continuing elsewhere in the same window suggests a clone, not a trip
    home_activity = ev.customer_history[
        (ev.customer_history["ts"] >= ev.flagged_txn["ts"] - pd.Timedelta(days=2))
        & (ev.customer_history["ts"] <= ev.flagged_txn["ts"] + pd.Timedelta(days=2))
        & (ev.customer_history["addr1"].isin(ev.customer_known_regions))
    ]
    weight = 0.45 + (0.15 if len(home_activity) > 0 else 0.0)
    desc = f"card-present purchase in region {region}, no prior history there for this customer"
    if len(home_activity) > 0:
        desc += f"; {len(home_activity)} transaction(s) continued in the customer's home region(s) in the same window"
    return True, weight, desc


def _cnp_fraud_signal(ev: CaseEvidence) -> tuple[bool, float, str]:
    """Covers both card_not_present_fraud and its new-device variant.

    is_new_device (id_15=='New') is NOT used as an independent trigger:
    measured against 40 random online transactions in this dataset, it
    fires 50% of the time -- a coin flip, not a signal (buying a new
    phone is common, as the dataset README itself notes). It only counts
    here as a modifier on top of an already-corroborated CNP signal,
    consistent with the pattern doc: 'Stronger than pattern 2, still not
    proof.'"""
    if ev.channel != "online":
        return False, 0.0, ""
    novel_product = ev.flagged_txn.get("ProductCD") not in ev.customer_known_products
    outlier_amount = ev.amount_zscore is not None and abs(ev.amount_zscore) > 2.5
    burst = len(ev.card_window_48h) >= 3
    if not (novel_product or outlier_amount):
        return False, 0.0, ""
    weight = 0.20 + (0.1 if outlier_amount else 0) + (0.1 if novel_product else 0) + (0.05 if burst else 0)
    parts = []
    if novel_product:
        parts.append(f"product {ev.flagged_txn.get('ProductCD')} never used before by this customer")
    if outlier_amount:
        parts.append(f"amount is a {ev.amount_zscore:.1f}-sigma outlier vs. this customer's history")
    if burst:
        parts.append(f"{len(ev.card_window_48h)} transactions on this card within 48 hours")
    if ev.is_new_device:
        weight += 0.15
        parts.append("from a device profile marked New for this account")
    return True, weight, "; ".join(parts)


def _account_takeover_signal(ev: CaseEvidence) -> tuple[bool, float, str]:
    """New device alone (50% background rate) and 'has ever used both
    channels' (true for most customers over 6 months) are both too common
    to trigger this on their own. Requires: new device, a genuine CNP
    anomaly (reuses the same bar as _cnp_fraud_signal), AND the OTHER
    channel being rare in this customer's own history (<15% of their
    transactions) -- i.e. this really is a channel switch, not their
    normal mixed usage."""
    if not ev.is_new_device or ev.channel != "online":
        return False, 0.0, ""
    novel_product = ev.flagged_txn.get("ProductCD") not in ev.customer_known_products
    outlier_amount = ev.amount_zscore is not None and abs(ev.amount_zscore) > 2.5
    if not (novel_product or outlier_amount):
        return False, 0.0, ""
    hist = ev.customer_history
    if len(hist) < 5:
        return False, 0.0, ""
    other_channel_share = (hist["channel"] == "in_person").mean()
    if other_channel_share < 0.05 or other_channel_share > 0.5:
        # too rare to have a baseline, or too common to call this a "switch"
        return False, 0.0, ""
    weight = 0.40
    desc = (f"new device profile plus a spending anomaly, on a customer whose history is "
            f"{other_channel_share:.0%} in-person -- inconsistent with the usual pattern")
    return True, weight, desc


def _ring_signal(ev: CaseEvidence) -> tuple[bool, float, str]:
    """R6: 'several cards show FRAUD from the same device profile, billing
    region, or recipient email in one window'.

    This dataset's base fraud rate is unusually high (4,665/5,565 closed
    cases are confirmed fraud), so "shares a billing region with some
    historically-fraudulent card" matches dozens of unrelated cards even in
    a tight +/-6h window -- not actually discriminating without a proper
    statistical baseline for that region's expected traffic, which is out
    of scope here. Region overlap is kept as informational evidence only
    (see evidence.py); only a shared device *fingerprint* -- specific and
    rare -- drives the R6 probability boost and MONITOR_CONNECTED_CARDS."""
    device_cards = set(ev.ring_device_cards) - {ev.card_id}
    if not device_cards:
        return False, 0.0, ""
    return True, 0.30, f"{len(device_cards)} other card(s) share this transaction's device profile"


PATTERN_DETECTORS = (
    ("card_testing", _card_testing_signal),
    ("out_of_region_use", _out_of_region_signal),
    ("card_not_present_fraud", _cnp_fraud_signal),  # renamed to the _new_device variant below if ev.is_new_device
    ("account_takeover", _account_takeover_signal),
)


def assess(ev: CaseEvidence, trigger_type: str, trigger_text: str, bank_risk_score: float | None,
           stage: str, customer_confirms: bool | None = None) -> Assessment:
    signals: list[str] = []

    if trigger_type == "customer_report":
        base = 0.55
        signals.append(f"customer proactively reported the transaction: \"{trigger_text}\"")
    elif trigger_type == "analyst_request":
        base = 0.35
        signals.append(f"analyst request: \"{trigger_text}\"")
    else:
        base = min(bank_risk_score or 0.0, 1.0) * 0.5
        signals.append(f"bank risk model scored this transaction {bank_risk_score:.2f} (an input, discounted per "
                        f"policy since high scores are often legitimate)")

    if customer_confirms is True:
        return Assessment(
            verdict="legitimate", fraud_probability=0.05, pattern="none", pattern_description="",
            signals=signals + ["customer confirmed making the transaction"],
        )
    if customer_confirms is False:
        base = max(base, 0.72)
        signals.append("customer denied making the transaction")

    detected = []
    for pattern_name, fn in PATTERN_DETECTORS:
        hit, weight, desc = fn(ev)
        if hit:
            if pattern_name == "card_not_present_fraud" and ev.is_new_device:
                pattern_name = "card_not_present_new_device"
            detected.append((pattern_name, weight, desc))
            signals.append(f"[{pattern_name}] {desc}")

    detected.sort(key=lambda t: t[1], reverse=True)
    pattern_weight_sum = sum(w for _, w, _ in detected[:2])  # top 2, avoid over-counting overlapping signals

    ring_hit, ring_weight, ring_desc = _ring_signal(ev)
    if ring_hit:
        signals.append(f"[shared_origin] {ring_desc}")

    # Only THIS card's own closed-case history is a safe probability signal.
    # The broader similar_closed_cases blend (ev.similar_closed_cases) includes
    # region-based matches, which -- given this dataset's unusually high base
    # fraud rate (4,665/5,565 closed cases are confirmed_fraud) -- would trigger
    # on almost every case and bias everything toward "fraud". Kept for
    # citation/case-memory display, not for scoring.
    closed_case_adj = 0.0
    for c in ev.same_card_closed_cases:
        if c["case_id"] and c.get("outcome") == "confirmed_fraud":
            closed_case_adj = max(closed_case_adj, 0.20)
        elif c.get("outcome") == "cleared":
            closed_case_adj = min(closed_case_adj, -0.15)
    if closed_case_adj:
        signals.append(f"case-memory prior adjustment: {closed_case_adj:+.2f} from this card's own closed-case history")

    probability = base + pattern_weight_sum + (ring_weight if ring_hit else 0.0) + closed_case_adj
    probability = max(0.02, min(0.98, probability))

    same_card_patterns = [
        c["pattern"] for c in ev.same_card_closed_cases
        if c["case_id"] and c.get("outcome") == "confirmed_fraud" and c.get("pattern") not in ("", "none")
    ]

    if detected:
        pattern, pattern_description = detected[0][0], ""
    elif probability >= 0.5 and same_card_patterns:
        # no live signal fired, but this card has a consistent documented
        # history -- more defensible than defaulting to undocumented
        pattern = max(set(same_card_patterns), key=same_card_patterns.count)
        pattern_description = ""
        signals.append(f"no fresh pattern signal fired; classified from this card's historical pattern ({pattern})")
    elif probability >= 0.5:
        pattern, pattern_description = "undocumented", (
            "Activity was scored likely fraudulent but does not match any of the five documented patterns "
            f"(signals: {'; '.join(signals)})."
        )
    else:
        pattern, pattern_description = "none", ""

    n_independent_signals = len(detected) + (1 if ring_hit else 0) + (1 if closed_case_adj else 0)
    settled = customer_confirms is not None
    stop_now = settled or (probability >= 0.85 and n_independent_signals >= 2) or (probability <= 0.15 and n_independent_signals >= 2)

    needs_more_evidence = False
    evidence_request_type = None
    if not stop_now and stage == "before_additional_evidence":
        # R1: single weak signal (including risk score alone) below 0.70 -> verify first
        if probability < 0.70 and n_independent_signals <= 1:
            needs_more_evidence = True
            if trigger_type == "analyst_request":
                evidence_request_type = "analyst_info"
            elif trigger_type == "customer_report":
                evidence_request_type = "step_up_auth"
            else:
                evidence_request_type = "customer_validation"

    if probability >= 0.70:
        verdict = "fraud"
    elif probability <= 0.20:
        verdict = "legitimate"
    else:
        verdict = "uncertain"

    return Assessment(
        verdict=verdict, fraud_probability=round(probability, 2), pattern=pattern,
        pattern_description=pattern_description, signals=signals,
        needs_more_evidence=needs_more_evidence, evidence_request_type=evidence_request_type,
    )


def recommend_actions(a: Assessment, exposure_usd: float, ring_cards: list[str]) -> list[dict]:
    """Maps an Assessment to policy actions with routes and rule citations."""
    actions: list[dict] = []

    def add(action, rule_ref):
        actions.append({"action": action, "route": policy.approval_route(action, exposure_usd).value, "reason": rule_ref})

    if a.needs_more_evidence:
        if a.evidence_request_type == "step_up_auth":
            add("STEP_UP_AUTH", f"R1: probability {a.fraud_probability:.2f} on a single signal, confirm before blocking")
        else:
            add("VERIFY_WITH_CUSTOMER", f"R1: probability {a.fraud_probability:.2f} on a single signal, confirm before blocking")
        return actions

    if a.verdict == "legitimate":
        add("CLOSE_NO_FRAUD", "R3: no evidence of fraud" if "customer confirmed" not in " ".join(a.signals) else "R3: customer confirmed the transaction")
        return actions

    if a.verdict == "uncertain":
        if exposure_usd > 500 or any("conflict" in s for s in a.signals):
            add("ESCALATE_TO_ANALYST", "R8: uncertain verdict with exposure over $500")
        add("MONITOR_CARD", "monitoring while uncertainty remains")
        return actions

    # verdict == fraud
    if a.pattern == "card_testing":
        add("DECLINE_TRANSACTION", "R5: card-testing sequence observed")
        add("STEP_UP_AUTH", "R5")
    else:
        add("BLOCK_CARD", "R2: confirmed/strongly suspected fraud")

    add("CREATE_CASE", "3a: fraud probability at/above 0.30")

    file_report, sar_reason = policy.sar_required(a.pattern, exposure_usd, bool(ring_cards))
    if file_report:
        add("FILE_REPORT", f"3a/R6: {sar_reason}")

    if ring_cards:
        add("MONITOR_CONNECTED_CARDS", f"R6: shared origin with {len(ring_cards)} other card(s)")

    if a.pattern == "undocumented":
        add("ESCALATE_TO_ANALYST", "R9: undocumented but coordinated/repeated pattern")

    return actions
