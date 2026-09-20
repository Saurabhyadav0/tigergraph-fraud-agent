# [Draft] Agentic Fraud Investigation with TigerGraph

Fill in once the real dataset/TigerGraph instance are wired in and the
20 benchmark cases have been run. Sections per the hackathon submission
requirements:

## What we built
- One-paragraph summary of the agent and its investigate -> assess ->
  gather-more-evidence -> act -> explain -> remember loop.

## Architecture
- Diagram/description of graph/, agent/, ui/ (see README.md's ASCII
  diagram as a starting point).

## How TigerGraph is used
- Schema design rationale (why these entities/edges).
- Which evidence-gathering queries do the heavy lifting (shared-device
  ring, velocity, case-memory similarity) and why graph traversal beats
  a flat SQL join here.
- How case memory is stored and retrieved (SIMILAR_TO edges).

## Agentic capabilities implemented
- LangGraph loop with a real conditional edge (assess -> gather more
  evidence vs. take action), policy-gated action execution, the
  before/after-evidence decision checkpoints, GraphRAG grounding.

## What we learned
## What we'd improve with more time
- Real embeddings-based retrieval / TigerGraph vector index instead of
  TF-IDF, wiring the TigerGraph MCP server directly in, a richer
  case-similarity scoring model, human-approval UI for analyst-routed
  actions.
