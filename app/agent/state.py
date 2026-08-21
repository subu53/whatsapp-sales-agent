"""Lead-stage state machine. Deliberately just a set of allowed values plus
a couple of helpers — the LLM decides transitions (via the log_lead_info
tool, guided by the system prompt); this module just keeps the vocabulary
consistent so downstream reporting/analytics can rely on it."""

STAGES = [
    "new",
    "discovering",
    "presenting",
    "handling_objection",
    "ready_to_buy",
    "handed_off",
    "nurture",
    "closed_won",
    "closed_lost",
]


def is_valid_stage(stage: str) -> bool:
    return stage in STAGES


def default_stage() -> str:
    return "new"
