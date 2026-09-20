"""
Builds a canonical (customer_id, card1) -> card_id mapping.

transactions.csv has no card_id column -- only customer_id + card1..card6.
closed_cases_history.csv and case_pack.csv DO give card_id strings like
"C08623-K2", but the "-K" suffix isn't a simple per-customer sequence: a
customer can carry a "K2" label their whole history even with only one
card1 fingerprint ever observed (verified against C08623). So the K-number
is an external label we can't re-derive from first principles.

Strategy: card1 is the stable fingerprint. For each (customer_id, card1)
pair actually observed in transactions.csv:
  1. If closed_cases_history.csv or case_pack.csv resolve to this exact
     pair (via a referenced transaction id), use THEIR card_id verbatim
     -- it's authoritative.
  2. Otherwise assign the next unused "-K<n>" label for that customer.

Output: data/HHGOA_IEEE/card_index.csv with columns
customer_id, card1, card_id -- the join key the loading pipeline and the
agent's card resolution both use.

Run: python scripts/build_card_index.py
"""
import re

import pandas as pd

DATA_DIR = "data/HHGOA_IEEE"


def main():
    print("Loading transactions (customer_id, card1, TransactionID)...")
    txns = pd.read_csv(f"{DATA_DIR}/transactions.csv", usecols=["TransactionID", "customer_id", "card1"])
    txns["card1"] = txns["card1"].astype(str)

    # canonical set of (customer_id, card1) pairs, in first-appearance order
    pairs = txns.drop_duplicates(subset=["customer_id", "card1"], keep="first")
    pair_to_pos = {(row.customer_id, row.card1): i for i, row in enumerate(pairs.itertuples())}

    txn_to_pair = txns.set_index("TransactionID")[["customer_id", "card1"]]
    txn_to_pair_dict = {
        tid: (cid, c1) for tid, cid, c1 in zip(txn_to_pair.index, txn_to_pair["customer_id"], txn_to_pair["card1"])
    }

    given_label = {}  # (customer_id, card1) -> authoritative card_id string

    def resolve(card_id: str, customer_id: str, txn_ids: list[int]):
        if not isinstance(card_id, str) or not card_id.strip():
            return
        for tid in txn_ids:
            pair = txn_to_pair_dict.get(tid)
            if pair and pair[0] == customer_id:
                given_label[pair] = card_id
                return

    print("Resolving labels from case_pack.csv...")
    case_pack = pd.read_csv(f"{DATA_DIR}/case_pack.csv")
    for row in case_pack.itertuples():
        resolve(row.card_id, row.customer_id, [row.flagged_txn_id])

    print("Resolving labels from closed_cases_history.csv...")
    closed = pd.read_csv(f"{DATA_DIR}/closed_cases_history.csv")
    for row in closed.itertuples():
        txn_ids = []
        if isinstance(row.first_fraud_txn_id, (int, float)) and not pd.isna(row.first_fraud_txn_id):
            txn_ids.append(int(row.first_fraud_txn_id))
        if isinstance(row.txn_ids, str):
            txn_ids += [int(t) for t in row.txn_ids.split("|") if t.strip().isdigit()]
        resolve(row.card_id, row.customer_id, txn_ids)

    print(f"Resolved {len(given_label)} authoritative card labels out of {len(pairs)} distinct (customer, card1) pairs.")

    next_k = {}  # customer_id -> next unused K number

    def used_k_numbers(customer_id):
        used = set()
        for (cid, _c1), label in given_label.items():
            if cid == customer_id:
                m = re.search(r"-K(\d+)$", label)
                if m:
                    used.add(int(m.group(1)))
        return used

    rows = []
    for (customer_id, card1) in pair_to_pos:
        if (customer_id, card1) in given_label:
            card_id = given_label[(customer_id, card1)]
        else:
            if customer_id not in next_k:
                used = used_k_numbers(customer_id)
                next_k[customer_id] = max(used, default=0) + 1
            while next_k[customer_id] in used_k_numbers(customer_id):
                next_k[customer_id] += 1
            card_id = f"{customer_id}-K{next_k[customer_id]}"
            next_k[customer_id] += 1
        rows.append({"customer_id": customer_id, "card1": card1, "card_id": card_id})

    out = pd.DataFrame(rows)
    out.to_csv(f"{DATA_DIR}/card_index.csv", index=False)
    print(f"Wrote {len(out)} rows to {DATA_DIR}/card_index.csv")

    dupes = out[out.duplicated("card_id", keep=False)]
    if not dupes.empty:
        print(f"WARNING: {len(dupes)} duplicate card_id labels -- inspect manually:")
        print(dupes.sort_values("card_id"))


if __name__ == "__main__":
    main()
