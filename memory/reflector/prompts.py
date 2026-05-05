from __future__ import annotations

SUMMARY_HEADER = "# Reflector Summary"


def summary_prompt_template(session_id: str, turn_count: int) -> str:
    return (
        f"{SUMMARY_HEADER}\n"
        f"session={session_id}\n"
        f"turn_count={turn_count}\n"
        "Generate a concise reflective summary emphasizing decisions, preferences, and open questions."
    )
