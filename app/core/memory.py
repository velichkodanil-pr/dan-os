"""Memory Service boundary.

Round 1 implements only candidate creation (see orchestrator) with provenance:
every MemoryItem stores source_event_id, domain, status, confidence.
Round 2 adds candidate review (evening check-in), confirm/forget commands.
Round 3 adds pgvector retrieval, conflicts and the coverage map.
"""
