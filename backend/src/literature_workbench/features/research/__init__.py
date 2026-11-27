"""Research-assistant (single-document RAG) feature.

Chunks the current parse output into an FTS5 index, retrieves relevant
fragments for a question, and answers through the audited DeepSeek egress
channel.  See ``v2-mvp-plan.md`` in the repository root.
"""
