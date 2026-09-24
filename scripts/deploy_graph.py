"""
One-shot deployment: creates the FraudGraph schema and installs the
GSQL queries against whatever TigerGraph instance .env points at.
Safe to re-run -- CREATE statements on already-existing types/graphs
just report "already exists" without touching data.

Run once, after filling in .env (TG_HOST, TG_SECRET, TG_GRAPH_NAME):
    python scripts/deploy_graph.py

Then load the foundational data:
    python scripts/build_card_index.py
    python scripts/load_graph_foundation.py
"""
import os
import time

import pyTigerGraph as tg
import requests
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("TG_HOST")
SECRET = os.getenv("TG_SECRET")
GRAPH = os.getenv("TG_GRAPH_NAME", "FraudGraph")


def get_token(graph: str = None) -> str:
    payload = {"secret": SECRET}
    if graph:
        payload["graph"] = graph
    r = requests.post(f"{HOST}/gsql/v1/tokens", json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["token"]


def main():
    if not HOST or not SECRET:
        raise SystemExit("Set TG_HOST and TG_SECRET in .env first (see .env.example).")

    print(f"Deploying schema to graph '{GRAPH}' at {HOST} ...")
    global_conn = tg.TigerGraphConnection(host=HOST, apiToken=get_token())

    schema = open("graph/schema.gsql").read().replace("FraudGraph", GRAPH)
    print(global_conn.gsql(schema))

    print("\nInstalling queries ...")
    graph_conn = tg.TigerGraphConnection(host=HOST, apiToken=get_token(GRAPH), graphname=GRAPH)
    for fname in ["graph/queries/similar_closed_cases.gsql"]:
        q = open(fname).read().replace("FraudGraph", GRAPH)
        print(graph_conn.gsql(q))

    # INSTALL QUERY needs a global (non-graph-scoped) token
    print(global_conn.gsql(f"USE GRAPH {GRAPH}\nINSTALL QUERY similar_closed_cases_graph"))

    print("\nDone. Next: python scripts/build_card_index.py && python scripts/load_graph_foundation.py")


if __name__ == "__main__":
    main()
