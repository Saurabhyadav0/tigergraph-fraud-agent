"""
Computes the structured fraud-investigation signals for one case,
against the real HHGOA_IEEE data (via TransactionStore) and the
graph's case memory (a live GSQL traversal via
GraphClient.similar_closed_cases_graph). This is the "gather evidence"
step of the investigate loop -- the output feeds both the rule-based
assessor and, when configured, the LLM reasoner.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from agent.transaction_store import TransactionStore


@dataclass
class EvidenceItem:
    claim: str
    source: str  # graph | document | customer | external
    ref: str
    entity_ids: list[str] = field(default_factory=list)


@dataclass
class CaseEvidence:
    flagged_txn: dict
    card_id: str
    customer_id: str
    channel: str
    device_profile: str | None
    is_new_device: bool
    card_window_1h: pd.DataFrame       # same-card txns within 1h of flagged txn
    card_window_48h: pd.DataFrame      # same-card txns within 48h
    customer_history: pd.DataFrame     # this customer's other txns, excluding flagged
    device_neighbors: pd.DataFrame     # OTHER cards sharing this device profile (30d window)
    region_neighbors: pd.DataFrame     # OTHER cards billed in this region (7d window around flagged)
    customer_known_regions: set
    customer_known_products: set
    amount_zscore: float | None
    similar_closed_cases: list[dict]      # broad: for citation/case-memory display only
    same_card_closed_cases: list[dict]    # narrow: this exact card's own history -- safe for probability adjustment
    ring_device_cards: list[str] = field(default_factory=list)   # other cards on the exact same device fingerprint
    ring_region_cards: list[str] = field(default_factory=list)   # other cards in-region WITH their own fraud history
    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, claim: str, source: str, ref: str, entity_ids: list[str] = None):
        self.items.append(EvidenceItem(claim, source, ref, entity_ids or []))


def _similar_closed_cases(store: TransactionStore, card_id: str, device_profile_id: str | None,
                           region_code, around_ts=None, top_k: int = 8) -> list[dict]:
    by_card = store.closed_cases_by_card(card_id)
    by_device = store.closed_cases_by_device(device_profile_id) if device_profile_id else store.closed_cases.iloc[0:0]
    by_region = (
        store.closed_cases_by_region(region_code, exclude_card_id=card_id, around_ts=around_ts)
        if region_code is not None and not pd.isna(region_code) else store.closed_cases.iloc[0:0]
    )

    # rank by specificity: same card first, then same device, then same region/time window
    ranked = pd.concat([by_card, by_device, by_region]).drop_duplicates(subset=["case_id"], keep="first").head(top_k)
    return ranked[["case_id", "outcome", "pattern", "exposure_usd", "analyst_notes"]].to_dict("records") if len(ranked) else []


def gather(store: TransactionStore, graph, flagged_txn_id: str, card_id: str, customer_id: str) -> CaseEvidence:
    txn = store.get_transaction(flagged_txn_id)
    channel = txn.get("channel", "")
    dev_id = txn.get("device_profile_id")
    dev_id = dev_id if isinstance(dev_id, str) and dev_id else None
    is_new_device = str(txn.get("id_15", "")).strip().lower() == "new"

    ts = txn.get("ts")
    card_window_1h = store.card_window(card_id, ts, hours=1) if ts is not None else pd.DataFrame()
    card_window_48h = store.card_window(card_id, ts, hours=48) if ts is not None else pd.DataFrame()
    customer_history = store.customer_billing_history(customer_id, exclude_txn_id=flagged_txn_id)

    device_neighbors = store.device_neighbors(dev_id, exclude_card_id=card_id) if dev_id else pd.DataFrame()

    # a tight window: this is meant to catch a coordinated ring operating in
    # the same place at the same time, not "anyone who ever billed here" --
    # this dataset's base fraud rate is unusually high (4,665/5,565 closed
    # cases are confirmed fraud), so a loose region+day window trivially
    # matches hundreds of unrelated cards and stops being informative.
    region = txn.get("addr1")
    if ts is not None and region is not None and not pd.isna(region):
        region_neighbors = store.region_neighbors(
            region, exclude_card_id=card_id,
            window_start=ts - pd.Timedelta(hours=6), window_end=ts + pd.Timedelta(hours=6),
        )
    else:
        region_neighbors = pd.DataFrame()

    known_regions = set(customer_history["addr1"].dropna().unique().tolist())
    known_products = set(customer_history["ProductCD"].dropna().unique().tolist())

    amount_zscore = None
    amts = customer_history["TransactionAmt"].dropna()
    if len(amts) >= 3 and amts.std() > 0:
        amount_zscore = (txn.get("TransactionAmt", 0) - amts.mean()) / amts.std()

    similar = _similar_closed_cases(store, card_id, dev_id, region, around_ts=ts)

    # Same-card case memory: a real GSQL traversal against TigerGraph
    # (InvCard -CC_ON_CARD/CC_CONNECTED_TO- ClosedCase), not a pandas
    # lookup. Falls back to the local mirror only if the graph call fails
    # (e.g. workspace mid-resume), so a transient connection hiccup
    # doesn't take down the whole investigation.
    same_card_source = "graph"
    try:
        same_card = graph.similar_closed_cases_graph(card_id)
    except Exception as e:
        print(f"[evidence.gather] similar_closed_cases_graph failed ({e}); falling back to local mirror")
        same_card_source = "local mirror (graph call failed)"
        same_card = store.closed_cases_by_card(card_id)[
            ["case_id", "outcome", "pattern", "exposure_usd", "analyst_notes"]
        ].to_dict("records")

    # A device fingerprint alone isn't enough (49% of fingerprints in this
    # dataset are shared by >1 card, some by 1000+ -- popular phone models,
    # not rings). Genuinely rare fingerprints (<=5 cards) count on their
    # own; anything short of the most extreme popularity (<=100 cards)
    # still counts IF at least one connected card has its own confirmed-fraud
    # history -- same corroboration bar used for region matching below.
    ring_device_cards = []
    if len(device_neighbors) > 0 and dev_id:
        candidates = set(device_neighbors["card_id"].dropna().tolist())
        if store.is_rare_device(dev_id, max_cards=5):
            ring_device_cards = sorted(candidates)
        elif store.is_rare_device(dev_id, max_cards=100):
            ring_device_cards = sorted(
                c for c in candidates
                if len(store.closed_cases_by_card(c).query("outcome == 'confirmed_fraud'")) > 0
            )

    ring_region_cards = []
    if len(region_neighbors) > 0:
        candidates = set(region_neighbors["card_id"].dropna().tolist())
        for c in candidates:
            if len(store.closed_cases_by_card(c).query("outcome == 'confirmed_fraud'")) > 0:
                ring_region_cards.append(c)
    ring_region_cards = sorted(set(ring_region_cards))

    ev = CaseEvidence(
        flagged_txn=txn, card_id=card_id, customer_id=customer_id, channel=channel,
        device_profile=dev_id, is_new_device=is_new_device,
        card_window_1h=card_window_1h, card_window_48h=card_window_48h,
        customer_history=customer_history, device_neighbors=device_neighbors,
        region_neighbors=region_neighbors, customer_known_regions=known_regions,
        customer_known_products=known_products, amount_zscore=amount_zscore,
        similar_closed_cases=similar, same_card_closed_cases=same_card,
        ring_device_cards=ring_device_cards, ring_region_cards=ring_region_cards,
    )

    ev.add(
        f"Flagged transaction {flagged_txn_id}: ${txn.get('TransactionAmt', 0):.2f}, "
        f"{channel}, product {txn.get('ProductCD')}, billing region {region}, "
        f"bank risk score {txn.get('risk_score')}",
        "graph", f"lookup:transaction({flagged_txn_id})", [flagged_txn_id],
    )

    if len(card_window_1h) > 1:
        small = card_window_1h[card_window_1h["TransactionAmt"] < 5]
        ev.add(
            f"{len(card_window_1h)} transaction(s) on card {card_id} within 1 hour of the flagged one; "
            f"{len(small)} under $5",
            "graph", f"query:card_window(card_id={card_id}, hours=1)",
            card_window_1h["TransactionID"].tolist(),
        )

    if dev_id and len(device_neighbors) > 0:
        other_cards = sorted(set(device_neighbors["card_id"].dropna().tolist()))
        rare = store.is_rare_device(dev_id)
        qualifier = "" if rare else " (a common device model/config -- not treated as a ring signal on its own)"
        ev.add(
            f"Device profile {dev_id} also used by {len(other_cards)} other card(s) in the last 30 days"
            f"{qualifier}: {other_cards[:5]}",
            "graph", f"query:device_neighbors(device_id={dev_id})", other_cards,
        )

    if region is not None and not pd.isna(region) and region not in known_regions and channel == "in_person":
        other_cards = sorted(set(region_neighbors["card_id"].dropna().tolist()))
        ev.add(
            f"Billing region {region} has no history for customer {customer_id} (known regions: "
            f"{sorted(known_regions)}); {len(other_cards)} other card(s) billed there in a +/-3 day window",
            "graph", f"query:region_neighbors(region={region})", other_cards,
        )

    if is_new_device:
        ev.add(f"Device profile {dev_id} marked New (id_15) for this account", "graph",
               f"lookup:transaction({flagged_txn_id})", [flagged_txn_id])

    if amount_zscore is not None:
        ev.add(
            f"Amount ${txn.get('TransactionAmt', 0):.2f} is {amount_zscore:.1f} standard deviations from "
            f"customer {customer_id}'s historical mean (n={len(amts)})",
            "graph", f"query:customer_history({customer_id})", [],
        )

    if same_card:
        ev.add(
            f"GSQL traversal found {len(same_card)} closed case(s) directly on or connected to this card "
            f"(source: {same_card_source}): "
            + ", ".join(f"{c['case_id']} ({c['outcome']}/{c['pattern']})" for c in same_card[:5]),
            "graph", f"query:similar_closed_cases_graph(card_id={card_id})", [c["case_id"] for c in same_card],
        )

    if similar:
        ev.add(
            f"Found {len(similar)} related closed case(s) in case memory (card/device/region blend): "
            + ", ".join(f"{c['case_id']} ({c['outcome']}/{c['pattern']})" for c in similar[:5]),
            "graph", "query:similar_closed_cases", [c["case_id"] for c in similar],
        )

    return ev
