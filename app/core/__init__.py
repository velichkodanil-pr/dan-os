"""DAN.OS core: business logic lives here, adapters call into it.

Module boundaries (implemented starting round 1):
- orchestrator: intent -> role -> context -> model -> proposal/answer
- memory: raw/indexed/candidate/confirmed facts with provenance and domains
- policy: deterministic action levels L0-L5, approvals
- audit: append-only action log
- scheduler: briefs, reminders, digests
"""
