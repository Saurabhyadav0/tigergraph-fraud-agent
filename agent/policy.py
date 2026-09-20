"""
The HHGOA fraud policy (data/HHGOA_IEEE/DATASET_README.md, "Fraud
Policy" section), encoded as data the reasoner and workflow both
consult -- action names, approval routes and the R1-R10 rule text are
kept verbatim so a human reviewer can check every recommendation
against the source policy sentence by sentence.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    AUTO = "auto"
    L1 = "L1"
    L2 = "L2"


@dataclass
class ActionSpec:
    name: str
    description: str
    customer_impact: str


ACTION_CATALOG: dict[str, ActionSpec] = {
    "ALLOW_TRANSACTION": ActionSpec("ALLOW_TRANSACTION", "Let the flagged transaction stand", "None"),
    "DECLINE_TRANSACTION": ActionSpec("DECLINE_TRANSACTION", "Decline the flagged authorization only. Card stays active", "Low"),
    "MONITOR_CARD": ActionSpec("MONITOR_CARD", "Card stays active; raise monitoring sensitivity for 72 hours", "None"),
    "MONITOR_CONNECTED_CARDS": ActionSpec("MONITOR_CONNECTED_CARDS", "Put other cards linked to the same device profile, region cluster, or ring under monitoring", "None"),
    "WARN_CUSTOMER": ActionSpec("WARN_CUSTOMER", "Send an informational message", "None"),
    "VERIFY_WITH_CUSTOMER": ActionSpec("VERIFY_WITH_CUSTOMER", "Ask the cardholder whether they made the transaction. Card stays active pending reply", "Low"),
    "STEP_UP_AUTH": ActionSpec("STEP_UP_AUTH", "Require a one-time passcode or app confirmation before further activity", "Low"),
    "BLOCK_CARD": ActionSpec("BLOCK_CARD", "Block this card and reissue", "High"),
    "BLOCK_ALL_CARDS": ActionSpec("BLOCK_ALL_CARDS", "Block every card the customer holds", "Very high"),
    "GENERATE_REPORT": ActionSpec("GENERATE_REPORT", "Write up the investigation for the internal record, without opening a case", "None"),
    "CREATE_CASE": ActionSpec("CREATE_CASE", "Open an internal fraud case with the evidence attached, and write it to the graph", "None"),
    "FILE_REPORT": ActionSpec("FILE_REPORT", "File a suspicious activity report with the regulator", "None"),
    "ESCALATE_TO_ANALYST": ActionSpec("ESCALATE_TO_ANALYST", "Hand the case to a human analyst with the evidence", "None"),
    "CLOSE_NO_FRAUD": ActionSpec("CLOSE_NO_FRAUD", "Close the alert as legitimate", "None"),
}

_AUTO_ACTIONS = {
    "ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER",
    "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE",
    "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
}


def approval_route(action: str, exposure_usd: float = 0.0) -> Route:
    """Section 2, Approval routing. BLOCK_CARD's route depends on exposure;
    everything else is a fixed lookup."""
    if action in _AUTO_ACTIONS:
        return Route.AUTO
    if action == "DECLINE_TRANSACTION":
        return Route.L1
    if action == "BLOCK_CARD":
        return Route.L1 if exposure_usd <= 2500 else Route.L2
    if action in ("BLOCK_ALL_CARDS", "FILE_REPORT"):
        return Route.L2
    raise ValueError(f"Unknown action: {action}")


def sar_required(pattern: str, exposure_usd: float, shared_device_or_region: bool) -> tuple[bool, str]:
    """Section 3a: file a report when fraud is confirmed/strongly suspected
    AND at least one of: exposure > $1,000; connects to a shared
    device/region/ring; pattern is coordinated or undocumented (R9)."""
    reasons = []
    if exposure_usd > 1000:
        reasons.append(f"exposure ${exposure_usd:,.2f} exceeds $1,000")
    if shared_device_or_region:
        reasons.append("activity connects to a shared device profile / region cluster / ring")
    if pattern == "undocumented":
        reasons.append("undocumented but coordinated/repeated pattern (R9)")
    if reasons:
        return True, "; ".join(reasons)
    return False, "confirmed/suspected fraud but none of the SAR trigger conditions (3a) are met"


# ------------------------------------------------------------
# R1-R10, verbatim from the policy, for the reasoner prompt and as a
# citable reference when the rule-based path applies one.
# ------------------------------------------------------------
RULES = {
    "R1": "Verify before you block on a weak signal. If the case rests on a single signal (including a risk "
          "score alone) and assessed fraud probability is below 0.70, recommend VERIFY_WITH_CUSTOMER or "
          "STEP_UP_AUTH before any block.",
    "R2": "Customer denies the transaction. Recommend BLOCK_CARD and CREATE_CASE. Add FILE_REPORT if exposure "
          "exceeds $1,000 or the case connects to a shared device profile or another card's fraud.",
    "R3": "Customer confirms the transaction. Recommend CLOSE_NO_FRAUD. Note the confirmation in the case file.",
    "R4": "No reply within 24 hours. Recommend MONITOR_CARD and DECLINE_TRANSACTION for pending authorizations. "
          "Escalate if exposure exceeds $500.",
    "R5": "Card testing. Three or more small online authorizations on one card within an hour, followed by a "
          "larger purchase: recommend DECLINE_TRANSACTION and STEP_UP_AUTH. If a purchase over $100 has "
          "already cleared, recommend BLOCK_CARD.",
    "R6": "Shared origin. When several cards show fraud from the same device profile, billing region, or "
          "recipient email in one window, name the shared element, recommend CREATE_CASE and FILE_REPORT, "
          "and MONITOR_CONNECTED_CARDS for every card that shares it.",
    "R7": "Disputed but legitimate. When the customer disputes a charge matching their own recurring pattern "
          "(same merchant, same amount, monthly), recommend CREATE_CASE, VERIFY_WITH_CUSTOMER, WARN_CUSTOMER. "
          "Do not block.",
    "R8": "Escalate when uncertain and exposed. If verdict is uncertain and exposure exceeds $500, or evidence "
          "conflicts, recommend ESCALATE_TO_ANALYST.",
    "R9": "Undocumented patterns. When activity fits none of the known patterns but shows coordinated or "
          "repeated abuse across customers, recommend CREATE_CASE, FILE_REPORT, ESCALATE_TO_ANALYST, and "
          "describe the pattern in your own words.",
    "R10": "Never BLOCK_ALL_CARDS unless at least two of the customer's cards show confirmed fraud or the "
           "customer's credentials are confirmed compromised.",
}

PATTERNS = (
    "card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover", "undocumented", "none",
)
