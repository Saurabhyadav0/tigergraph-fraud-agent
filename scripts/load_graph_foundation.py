"""
Loads the foundational, always-needed parts of the graph:
  - every Customer / InvCard observed in transactions.csv (via card_index.csv)
  - the full closed_cases_history.csv as ClosedCase vertices + edges

Deliberately does NOT bulk-load all 590K Txn rows -- at this dataset's
scale that's better served by a local pandas-backed TransactionStore
(agent/transaction_store.py) for broad pattern scans, with only the
transactions/devices/regions an investigation actually touches written
into the graph as it runs (see agent/graph_client.py write_investigation).
That keeps the graph as curated case-memory, not a raw data mirror, and
avoids moving 700MB over REST to a shared eval instance.

Run: python scripts/load_graph_foundation.py
"""
import os

import pandas as pd
import pyTigerGraph as tg
import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data/HHGOA_IEEE"


def get_conn():
    host = os.getenv("TG_HOST")
    secret = os.getenv("TG_SECRET")
    graph = os.getenv("TG_GRAPH_NAME", "FraudGraph")
    r = requests.post(f"{host}/gsql/v1/tokens", json={"secret": secret, "graph": graph})
    r.raise_for_status()
    token = r.json()["token"]
    return tg.TigerGraphConnection(host=host, apiToken=token, graphname=graph)


def load_customers_and_cards(conn):
    print("Building full card attribute table...")
    card_index = pd.read_csv(f"{DATA_DIR}/card_index.csv", dtype=str)

    txns = pd.read_csv(
        f"{DATA_DIR}/transactions.csv",
        usecols=["customer_id", "card1", "card2", "card3", "card4", "card5", "card6"],
        dtype=str,
    )
    first_seen = txns.drop_duplicates(subset=["customer_id", "card1"], keep="first")

    cards = card_index.merge(first_seen, on=["customer_id", "card1"], how="left")
    cards = cards.fillna("")

    customers = cards[["customer_id"]].drop_duplicates()
    print(f"Upserting {len(customers)} Customer vertices...")
    print(conn.upsertVertexDataFrame(customers, "Customer", v_id="customer_id", attributes={}))

    print(f"Upserting {len(cards)} InvCard vertices...")
    print(conn.upsertVertexDataFrame(
        cards, "InvCard", v_id="card_id",
        attributes={c: c for c in ["card1", "card2", "card3", "card4", "card5", "card6"]},
    ))

    print("Upserting OWNS edges (Customer -> InvCard)...")
    print(conn.upsertEdgeDataFrame(
        cards[["customer_id", "card_id"]], "Customer", "OWNS", "InvCard",
        from_id="customer_id", to_id="card_id", attributes={},
    ))


def load_closed_cases(conn):
    print("Loading closed_cases_history.csv...")
    closed = pd.read_csv(f"{DATA_DIR}/closed_cases_history.csv")
    closed = closed.fillna("")
    closed["report_filed"] = closed["report_filed"].astype(str)
    closed["first_fraud_txn_id"] = closed["first_fraud_txn_id"].astype(str)

    print(f"Upserting {len(closed)} ClosedCase vertices...")
    attrs = {
        c: c for c in [
            "customer_id", "card_id", "opened_at", "closed_at", "outcome", "pattern",
            "first_fraud_txn_id", "txn_ids", "n_txns", "exposure_usd",
            "connected_card_ids", "actions_taken", "report_filed", "analyst_notes",
        ]
    }
    print(conn.upsertVertexDataFrame(closed, "ClosedCase", v_id="case_id", attributes=attrs))

    print("Upserting CC_ON_CARD edges (ClosedCase -> InvCard)...")
    print(conn.upsertEdgeDataFrame(
        closed[["case_id", "card_id"]], "ClosedCase", "CC_ON_CARD", "InvCard",
        from_id="case_id", to_id="card_id", attributes={},
    ))

    connected = closed[closed["connected_card_ids"] != ""][["case_id", "connected_card_ids"]].copy()
    if not connected.empty:
        connected["connected_card_ids"] = connected["connected_card_ids"].str.split("|")
        connected = connected.explode("connected_card_ids").rename(columns={"connected_card_ids": "card_id"})
        connected = connected.drop_duplicates().reset_index(drop=True)
        print(f"Upserting {len(connected)} CC_CONNECTED_TO edges...")
        print(conn.upsertEdgeDataFrame(
            connected[["case_id", "card_id"]], "ClosedCase", "CC_CONNECTED_TO", "InvCard",
            from_id="case_id", to_id="card_id", attributes={},
        ))


def main():
    conn = get_conn()
    load_customers_and_cards(conn)
    load_closed_cases(conn)
    print("Done.")


if __name__ == "__main__":
    main()
