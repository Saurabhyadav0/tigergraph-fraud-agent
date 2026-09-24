"""
TigerGraph access: two directions.

Read -- case-memory retrieval (prior fraud cases connected to a card)
runs as a real GSQL traversal against TigerGraph (graph/queries/
similar_closed_cases.gsql: InvCard -CC_ON_CARD/CC_CONNECTED_TO- ClosedCase),
not a local lookup. Broader raw-ledger analysis (velocity, shared
device/region across the full 590K-row transaction set) still runs
against agent.transaction_store's local index -- see that module's
docstring for why: TigerGraph holds curated case memory and the
customer/card/closed-case graph, not a mirror of the raw ledger.

Write -- each investigation's InvestigationCase vertex and its edges
(to the transactions/cards/devices/closed-cases it actually touched)
get written into the graph -- the case memory the brief asks for
("write it into the graph... the next investigation should be able to
find it").
"""
from __future__ import annotations

import requests
import pyTigerGraph as tg

from agent.config import CONFIG


class GraphClient:
    def __init__(self):
        self.conn = self._connect()

    def _fetch_token(self) -> str:
        # pyTigerGraph's built-in getToken() hits the legacy /requesttoken
        # endpoint, which this TigerGraph version 404s on; /gsql/v1/tokens
        # is the endpoint this instance actually serves.
        resp = requests.post(
            f"{CONFIG.tg_host}/gsql/v1/tokens",
            json={"secret": CONFIG.tg_secret, "graph": CONFIG.tg_graph_name},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    def _connect(self) -> tg.TigerGraphConnection:
        token = self._fetch_token()
        return tg.TigerGraphConnection(host=CONFIG.tg_host, apiToken=token, graphname=CONFIG.tg_graph_name)

    def similar_closed_cases_graph(self, card_id: str) -> list[dict]:
        """Real GSQL traversal (installed query similar_closed_cases_graph):
        closed cases sitting directly on this card, or naming it as a
        connected card in a ring -- via CC_ON_CARD_REV / CC_CONNECTED_TO_REV
        edges loaded for all 5,565 closed cases."""
        raw = self.conn.runInstalledQuery("similar_closed_cases_graph", params={"card_id": card_id})[0]
        cases = raw.get("Cases", [])
        return [
            {
                "case_id": c["attributes"].get("Cases.case_id", c["v_id"]),
                "outcome": c["attributes"].get("Cases.outcome"),
                "pattern": c["attributes"].get("Cases.pattern"),
                "exposure_usd": c["attributes"].get("Cases.exposure_usd", 0),
                "analyst_notes": c["attributes"].get("Cases.analyst_notes", ""),
            }
            for c in cases
        ]

    def write_investigation(self, answer: dict) -> None:
        case = answer["case"]
        case_id = answer["case_id"]

        self.conn.upsertVertex("InvestigationCase", case_id, {
            "status": case["status"],
            "verdict": case["verdict"],
            "fraud_probability": case["fraud_probability"],
            "pattern": case["pattern"],
            "pattern_description": case["pattern_description"],
            "exposure_usd": case["exposure_usd"],
            "summary": case["summary"],
            "stop_reason": answer["stop_reason"],
        })

        for txn_id in case["affected_txn_ids"]:
            self.conn.upsertEdge("InvestigationCase", case_id, "IC_INVOLVES_TXN", "Txn", txn_id, {})

        for card_id in {*case["connected_card_ids"]}:
            self.conn.upsertEdge("InvestigationCase", case_id, "IC_INVOLVES_CARD", "InvCard", card_id, {})

        for device_id in case["connected_device_profiles"]:
            self.conn.upsertEdge("InvestigationCase", case_id, "IC_USES_DEVICE", "DeviceProfile", device_id, {})

        for closed_case_id in case["similar_prior_cases"]:
            self.conn.upsertEdge("InvestigationCase", case_id, "IC_SIMILAR_TO_CC", "ClosedCase", closed_case_id, {})


_client: GraphClient | None = None


def get_graph_client() -> GraphClient:
    global _client
    if _client is None:
        _client = GraphClient()
    return _client
