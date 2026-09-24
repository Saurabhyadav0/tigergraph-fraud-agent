"""
GraphRAG: retrieval over unstructured text, complementing the
structured graph traversal in agent/graph_client.py and the
policy/pattern grounding in agent/policy.py.

agent/graph_client.py's similar_closed_cases_graph finds prior cases
connected to THIS card by an explicit edge (same card, or named as
connected). This module finds prior cases that describe a similar
*modus operandi* in free text -- catching a case with no direct
card/device/region overlap at all, purely because the analyst's
narrative describes the same kind of activity. Both get synthesized
into the context passed to the LLM/assessor rather than raw rows,
per the brief: "pass the relevant context to the LLM rather than
simply passing raw data."

TF-IDF rather than a hosted embedding model -- zero extra
infrastructure/API cost for a 5,565-document corpus that's rebuilt
once per process and queried in milliseconds; swapping in a real
embedding model behind the same retrieve() signature only helps once
the corpus is large enough that lexical overlap stops being a good
enough proxy for semantic similarity.
"""
from __future__ import annotations

from functools import lru_cache

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from agent.config import CONFIG


class NarrativeRetriever:
    def __init__(self, closed_cases_df):
        self.df = closed_cases_df.reset_index(drop=True)
        notes = self.df["analyst_notes"].fillna("").tolist()
        self._vectorizer = TfidfVectorizer(stop_words="english", max_features=5000)
        self._matrix = self._vectorizer.fit_transform(notes)

    def retrieve(self, query_text: str, top_k: int = 5, exclude_case_ids: set[str] = None) -> list[dict]:
        if not query_text.strip():
            return []
        qv = self._vectorizer.transform([query_text])
        sims = cosine_similarity(qv, self._matrix)[0]
        exclude_case_ids = exclude_case_ids or set()

        ranked = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)
        results = []
        for i in ranked:
            if sims[i] <= 0.05:
                break
            row = self.df.iloc[i]
            if row["case_id"] in exclude_case_ids:
                continue
            results.append({
                "case_id": row["case_id"], "outcome": row["outcome"], "pattern": row["pattern"],
                "exposure_usd": row["exposure_usd"], "analyst_notes": row["analyst_notes"],
                "similarity": round(float(sims[i]), 3),
            })
            if len(results) >= top_k:
                break
        return results


@lru_cache(maxsize=1)
def get_retriever() -> NarrativeRetriever:
    import pandas as pd
    closed = pd.read_csv(f"{CONFIG.real_data_dir}/closed_cases_history.csv")
    return NarrativeRetriever(closed)
