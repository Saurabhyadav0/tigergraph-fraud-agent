"""
Runs the agent over all 20 real cases in data/HHGOA_IEEE/case_pack.csv
and writes one answer JSON per case to cases/<case_id>.json, per the
submission format.

Usage:
    python scripts/run_case_pack.py [case_id ...]

With no arguments, runs all 20 cases. Pass one or more case ids
(e.g. HHG-003) to run a subset while iterating.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import CONFIG
from agent.graph_client import get_graph_client
from agent.investigate import investigate_case
from agent.transaction_store import get_store

OUT_DIR = "cases"


def main():
    only = set(sys.argv[1:]) or None

    case_pack = pd.read_csv(f"{CONFIG.real_data_dir}/case_pack.csv")
    if only:
        case_pack = case_pack[case_pack["case_id"].isin(only)]

    os.makedirs(OUT_DIR, exist_ok=True)

    store = get_store()
    graph = get_graph_client()

    results = []
    for _, row in case_pack.iterrows():
        case = row.to_dict()
        print(f"Investigating {case['case_id']} ({case['trigger_type']}, txn {case['flagged_txn_id']})...")
        try:
            answer = investigate_case(store, graph, case)
        except Exception as e:
            print(f"  FAILED: {e}")
            raise

        path = os.path.join(OUT_DIR, f"{case['case_id']}.json")
        with open(path, "w") as f:
            json.dump(answer, f, indent=2, default=str)

        c = answer["case"]
        print(f"  -> verdict={c['verdict']} prob={c['fraud_probability']} pattern={c['pattern']} "
              f"sar={answer['sar']['file']} actions_final={[a['action'] for a in answer['next_best_actions']['final']]}")
        results.append((case["case_id"], c["verdict"], c["fraud_probability"], c["pattern"]))

    print(f"\n{len(results)} case(s) written to {OUT_DIR}/")


if __name__ == "__main__":
    main()
