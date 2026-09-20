"""
Fast local index over the real HHGOA_IEEE dataset (590K transactions,
144K identity records). Loading all 590K rows into TigerGraph itself
isn't practical on a shared eval instance (700MB), so this store is
the agent's window onto the raw data for broad pattern scans -- shared
device/region detection across the whole dataset, velocity windows,
etc. -- while TigerGraph holds the curated case-memory graph (closed
cases plus whatever each investigation actually touches).

Loads a reduced column set (drops the 339 unnamed V-columns, which
have no documented meaning and would roughly double load time/memory
for little interpretable signal) -- C1-14, D1-15, M1-9 are kept since
the dataset README calls them out as usable, if unnamed, signals.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache

import pandas as pd

from agent.config import CONFIG

_ESSENTIAL_COLS = (
    ["TransactionID", "TransactionDT", "TransactionAmt", "ProductCD",
     "card1", "card2", "card3", "card4", "card5", "card6",
     "addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain"]
    + [f"C{i}" for i in range(1, 15)]
    + [f"D{i}" for i in range(1, 16)]
    + [f"M{i}" for i in range(1, 10)]
    + ["customer_id", "ts", "channel", "risk_score"]
)

_IDENTITY_COLS = [
    "TransactionID", "id_15", "id_23", "id_30", "id_31", "id_33", "DeviceType", "DeviceInfo",
] + [f"id_{i:02d}" for i in range(1, 12)]  # id_01-id_11: encoded ratings


def device_profile_id(device_info: str, os_: str, browser: str, screen: str) -> str:
    key = f"{device_info}|{os_}|{browser}|{screen}"
    return "dev_" + hashlib.sha1(key.encode()).hexdigest()[:16]


class TransactionStore:
    def __init__(self, data_dir: str = None):
        self.data_dir = data_dir or CONFIG.real_data_dir

        print(f"[TransactionStore] loading transactions from {self.data_dir} ...")
        txns = pd.read_csv(f"{self.data_dir}/transactions.csv", usecols=_ESSENTIAL_COLS)
        txns["TransactionID"] = txns["TransactionID"].astype(str)
        txns["card1"] = txns["card1"].astype(str)
        txns["ts"] = pd.to_datetime(txns["ts"])

        card_index = pd.read_csv(f"{self.data_dir}/card_index.csv", dtype=str)
        txns = txns.merge(card_index[["customer_id", "card1", "card_id"]], on=["customer_id", "card1"], how="left")

        identity = pd.read_csv(f"{self.data_dir}/identity.csv", usecols=_IDENTITY_COLS)
        identity["TransactionID"] = identity["TransactionID"].astype(str)
        identity["device_profile_id"] = identity.apply(
            lambda r: device_profile_id(
                str(r.get("DeviceInfo", "")), str(r.get("id_30", "")),
                str(r.get("id_31", "")), str(r.get("id_33", "")),
            ),
            axis=1,
        )

        self.txns = txns.merge(identity, on="TransactionID", how="left").set_index("TransactionID", drop=False)
        print(f"[TransactionStore] loaded {len(self.txns)} transactions, {len(identity)} identity records.")

        self.closed_cases = self._load_closed_cases_with_fingerprints()
        print(f"[TransactionStore] loaded {len(self.closed_cases)} closed cases with resolved fingerprints.")

        # device_profile_id is only (DeviceInfo, OS, browser, screen) -- a
        # device MODEL+CONFIG fingerprint, not a unique physical device.
        # 49% of fingerprints in this dataset are shared by >1 card, some by
        # 1000+ (popular phone models). Precompute global popularity so
        # "shared device" signals can filter out common models as noise.
        self.device_card_counts = self.txns.dropna(subset=["device_profile_id"]).groupby(
            "device_profile_id"
        )["card_id"].nunique()

    def _load_closed_cases_with_fingerprints(self) -> pd.DataFrame:
        closed = pd.read_csv(f"{self.data_dir}/closed_cases_history.csv")
        closed["first_fraud_txn_id"] = closed["first_fraud_txn_id"].apply(
            lambda v: str(int(v)) if pd.notna(v) else None
        )

        def lookup(txn_id):
            if txn_id is None or txn_id not in self.txns.index:
                return pd.Series({"device_profile_id": None, "addr1": None})
            row = self.txns.loc[txn_id]
            return pd.Series({"device_profile_id": row.get("device_profile_id"), "addr1": row.get("addr1")})

        fingerprints = closed["first_fraud_txn_id"].apply(lookup)
        return pd.concat([closed, fingerprints], axis=1)

    # -- lookups -----------------------------------------------------

    def get_transaction(self, txn_id: str) -> dict:
        txn_id = str(txn_id)
        if txn_id not in self.txns.index:
            return {}
        row = self.txns.loc[txn_id]
        return row.to_dict()

    def get_card_history(self, card_id: str) -> pd.DataFrame:
        return self.txns[self.txns["card_id"] == card_id].sort_values("ts")

    def is_rare_device(self, device_profile_id_: str, max_cards: int = 5) -> bool:
        """True if this device fingerprint is used by few enough distinct
        cards globally to plausibly be one actual device/actor, not just a
        popular phone model coincidentally shared by unrelated customers."""
        if not device_profile_id_:
            return False
        return self.device_card_counts.get(device_profile_id_, 0) <= max_cards

    def card_window(self, card_id: str, around_ts, hours: float = 2.0) -> pd.DataFrame:
        hist = self.get_card_history(card_id)
        lo, hi = around_ts - pd.Timedelta(hours=hours), around_ts + pd.Timedelta(hours=hours)
        return hist[(hist["ts"] >= lo) & (hist["ts"] <= hi)]

    def device_neighbors(self, device_profile_id_: str, exclude_card_id: str = None, days: float = 30) -> pd.DataFrame:
        if not device_profile_id_ or pd.isna(device_profile_id_):
            return self.txns.iloc[0:0]
        sub = self.txns[self.txns["device_profile_id"] == device_profile_id_]
        if exclude_card_id:
            sub = sub[sub["card_id"] != exclude_card_id]
        return sub.sort_values("ts")

    def region_neighbors(self, region_code, exclude_card_id: str = None, window_start=None, window_end=None) -> pd.DataFrame:
        sub = self.txns[self.txns["addr1"] == region_code]
        if exclude_card_id:
            sub = sub[sub["card_id"] != exclude_card_id]
        if window_start is not None:
            sub = sub[sub["ts"] >= window_start]
        if window_end is not None:
            sub = sub[sub["ts"] <= window_end]
        return sub.sort_values("ts")

    def customer_billing_history(self, customer_id: str, exclude_txn_id: str = None) -> pd.DataFrame:
        sub = self.txns[self.txns["customer_id"] == customer_id]
        if exclude_txn_id:
            sub = sub[sub["TransactionID"] != str(exclude_txn_id)]
        return sub.sort_values("ts")

    def closed_cases_by_card(self, card_id: str) -> pd.DataFrame:
        direct = self.closed_cases[self.closed_cases["card_id"] == card_id]
        connected_mask = self.closed_cases["connected_card_ids"].fillna("").apply(
            lambda s: card_id in s.split("|") if s else False
        )
        return pd.concat([direct, self.closed_cases[connected_mask]]).drop_duplicates(subset=["case_id"])

    def closed_cases_by_device(self, device_profile_id_: str) -> pd.DataFrame:
        if not device_profile_id_:
            return self.closed_cases.iloc[0:0]
        return self.closed_cases[self.closed_cases["device_profile_id"] == device_profile_id_]

    def closed_cases_by_region(self, region_code, exclude_card_id: str = None,
                                around_ts=None, days: float = 60) -> pd.DataFrame:
        if region_code is None or pd.isna(region_code):
            return self.closed_cases.iloc[0:0]
        sub = self.closed_cases[self.closed_cases["addr1"] == region_code]
        if exclude_card_id:
            sub = sub[sub["card_id"] != exclude_card_id]
        if around_ts is not None:
            opened = pd.to_datetime(sub["opened_at"])
            sub = sub[(opened >= around_ts - pd.Timedelta(days=days)) & (opened <= around_ts + pd.Timedelta(days=days))]
        return sub


@lru_cache(maxsize=1)
def get_store() -> TransactionStore:
    return TransactionStore()
