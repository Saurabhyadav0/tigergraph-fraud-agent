"""
Analyst dashboard: pick a case (from the real 20-case case_pack, or
any transaction id in the dataset) and watch the investigation run --
evidence, the before/after decision checkpoints, the SAR when policy
calls for one, and case-memory matches -- against the real HHGOA_IEEE
data and the live TigerGraph instance.

Run: streamlit run ui/app.py
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import CONFIG
from agent.graph_client import get_graph_client
from agent.investigate import investigate_case
from agent.transaction_store import get_store

st.set_page_config(page_title="Fraud Investigation Agent", layout="wide")


@st.cache_resource
def _init():
    store = get_store()
    graph = get_graph_client()
    return store, graph


with st.spinner("Loading transaction store and connecting to TigerGraph (first run only)..."):
    store, graph = _init()

case_pack = pd.read_csv(f"{CONFIG.real_data_dir}/case_pack.csv")

st.title("Agentic Fraud Investigation — Analyst Dashboard")
st.caption(f"HHGOA_IEEE dataset · {len(store.txns):,} transactions · {len(store.closed_cases):,} closed cases "
           f"· graph: `{CONFIG.tg_graph_name}` on TigerGraph Savanna")

with st.sidebar:
    st.header("Investigate a case")
    case_id = st.selectbox("Case (from case_pack.csv)", case_pack["case_id"].tolist())
    row = case_pack[case_pack["case_id"] == case_id].iloc[0]
    st.write(f"**Trigger:** {row['trigger_type']}")
    st.write(f"**Card:** {row['card_id']}  \n**Customer:** {row['customer_id']}")
    st.write(f"**Flagged txn:** {row['flagged_txn_id']}")
    if pd.notna(row.get("risk_score")):
        st.write(f"**Bank risk score:** {row['risk_score']}")
    st.caption(row["trigger_text"])

    run = st.button("Investigate", type="primary")

if run:
    with st.spinner("Investigating..."):
        answer = investigate_case(store, graph, row.to_dict())
    st.session_state["answer"] = answer

if "answer" not in st.session_state:
    st.info("Select a case and click **Investigate** to start.")
    st.stop()

answer = st.session_state["answer"]
case = answer["case"]

risk_colors = {"fraud": "red", "uncertain": "orange", "legitimate": "green"}
col1, col2, col3, col4 = st.columns(4)
col1.metric("Case", answer["case_id"])
col2.metric("Verdict", case["verdict"].upper())
col3.metric("Fraud probability", f"{case['fraud_probability']:.0%}")
col4.metric("Exposure", f"${case['exposure_usd']:,.2f}")

st.markdown(f":{risk_colors.get(case['verdict'], 'gray')}[**Status: {case['status']}**  ·  Pattern: "
            f"{case['pattern']}]" + ("  ·  🚨 SAR filed" if answer["sar"]["file"] else ""))

tab_summary, tab_evidence, tab_actions, tab_memory, tab_sar, tab_raw = st.tabs(
    ["Summary", "Evidence", "Next-best-actions", "Case memory", "SAR", "Raw JSON"]
)

with tab_summary:
    st.write(case["summary"])
    st.write(f"**Stop reason:** {answer['stop_reason']}")
    if answer["evidence_requests"]:
        st.subheader("Evidence requested")
        for er in answer["evidence_requests"]:
            st.write(f"- **{er['type']}**: {er['assumed_response']}")
    st.caption(f"tool_calls={answer['tool_calls']}  tokens={answer['tokens']}  latency={answer['latency_s']}s")

with tab_evidence:
    for e in case["evidence"]:
        with st.expander(f"[{e['source']}] {e['ref']}"):
            st.write(e["claim"])
            if e["entity_ids"]:
                st.caption(", ".join(str(x) for x in e["entity_ids"][:20]))

with tab_actions:
    nba = answer["next_best_actions"]
    st.subheader("Initial (before additional evidence)")
    st.dataframe(pd.DataFrame(nba["initial"]), use_container_width=True, hide_index=True)
    st.subheader("Final (after additional evidence)")
    st.dataframe(pd.DataFrame(nba["final"]), use_container_width=True, hide_index=True)
    st.write(f"**What changed:** {nba['what_changed']}")

with tab_memory:
    if case["similar_prior_cases"]:
        st.write(f"{len(case['similar_prior_cases'])} similar closed case(s) retrieved as memory:")
        st.write(", ".join(case["similar_prior_cases"]))
    else:
        st.write("No similar closed cases found.")
    st.write(f"**Written to graph:** {case['written_to_graph']}  ·  **graph_case_id:** {case['graph_case_id']}")
    if case["connected_card_ids"]:
        st.write(f"**Connected cards (shared device):** {', '.join(case['connected_card_ids'])}")

with tab_sar:
    sar = answer["sar"]
    if sar["file"]:
        st.write(f"**Reason:** {sar['reason']}")
        st.write(sar["narrative"])
        st.caption(f"Subjects: {', '.join(sar['subjects'])}")
    else:
        st.write(f"No SAR filed. {sar['reason']}")

with tab_raw:
    st.json(answer)
