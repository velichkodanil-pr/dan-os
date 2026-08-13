"""Deterministic action policy (levels L0-L5). No LLM involvement — code only.

The model may PROPOSE actions; this module decides what is allowed and what
confirmation it requires. Unknown actions are denied by default.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    level: str
    confirmation_required: bool
    reason: str
    policy_version: str = "1.0"


# action -> (level, allowed, confirmation_required)
_RULES: dict[str, tuple[str, bool, bool]] = {
    # L0 — read/analyze: automatic
    "note.read": ("L0", True, False),
    "today.read": ("L0", True, False),
    "task.read": ("L0", True, False),
    "drive.read": ("L0", True, False),
    "gmail.read": ("L0", True, False),
    # L1 — internal writes: automatic (undo-able)
    "raw_event.create": ("L1", True, False),
    "memory.candidate_create": ("L1", True, False),
    "proposal.create": ("L1", True, False),
    # L2 — reversible personal writes: explicit user intent (button/command)
    "task.create_via_approval": ("L2", True, True),
    "task.cancel": ("L2", True, True),
    "task.complete": ("L2", True, True),
    "proposal.reject": ("L2", True, True),
    "proposal.edit": ("L2", True, True),
    "memory.confirm": ("L2", True, True),
    "memory.reject": ("L2", True, True),
    "memory.supersede": ("L2", True, True),
    "google.connect": ("L2", True, True),  # OAuth consent IS the confirmation
    "reminder.schedule": ("L2", True, False),  # follows an approved task
    "reminder.cancel": ("L2", True, False),
    # L3/L4 — external writes/communication: NOT SUPPORTED in round 1
    "calendar.write": ("L3", False, True),
    "email.draft": ("L3", True, True),  # draft-only (preview+confirm); SENDING stays denied
    "email.send": ("L4", False, True),
    "crm.write": ("L4", False, True),
    "post.publish": ("L4", False, True),
    # L5 — never automated
    "payment.execute": ("L5", False, True),
    "trading.execute": ("L5", False, True),
    "data.hard_delete": ("L5", False, True),
}


def evaluate(action: str) -> PolicyDecision:
    rule = _RULES.get(action)
    if rule is None:
        return PolicyDecision(False, "L?", True, f"UNKNOWN_ACTION_DENIED:{action}")
    level, allowed, confirm = rule
    if not allowed:
        return PolicyDecision(False, level, confirm, f"UNSUPPORTED_IN_ROUND:{action}")
    return PolicyDecision(True, level, confirm, "OK")


class PolicyDenied(Exception):
    def __init__(self, action: str, decision: PolicyDecision):
        self.action = action
        self.decision = decision
        super().__init__(f"denied {action}: {decision.reason}")
