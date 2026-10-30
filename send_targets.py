"""send_message target syntax (kept out of the adapter module)."""

import re

# wxid_xxx | group:<roomid> | group:<roomid>|wxid_a,wxid_b
_TARGET_RE = re.compile(r"^\s*((?:group:[^\s|]+(?:\|[^\s|]+)?)|(?:wxid_[-A-Za-z0-9_]+))\s*$")


def parse_target_ref(ref: str):
    """Explicit target -> (chat_id, thread_id=None); anything else -> None (generic rules apply)."""
    m = _TARGET_RE.fullmatch(ref or "")
    return (m.group(1), None) if m else None


SEND_ENTRY_KWARGS = {"target_ref_syntax_fn": parse_target_ref}
