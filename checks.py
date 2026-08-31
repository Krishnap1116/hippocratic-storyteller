import re


WORD_COUNT_MIN = 300
WORD_COUNT_MAX = 2000

UNSAFE_WORDS = [
    "kill", "murder", "blood", "gun", "knife", "suicide", "drugs", "alcohol",
    "sex", "naked", "damn", "hell", "die", "death",
    "weapon", "torture", "abuse",
]


def scan_unsafe_words(text: str) -> list[str]:
    lowered = text.lower()
    return [w for w in UNSAFE_WORDS if re.search(rf"\b{re.escape(w)}\b", lowered)]


def run_pre_check(text: str) -> dict:
    """Local, no-LLM gate. Returns a report dict; 'passed' is False only on
    obvious/egregious misses -- this is a cheap net, not the real safety check."""
    word_count = len(re.findall(r"\S+", text))
    unsafe_hits = scan_unsafe_words(text)

    length_ok = WORD_COUNT_MIN <= word_count <= WORD_COUNT_MAX
    notes = []
    if not length_ok:
        notes.append(f"word count {word_count} outside sane range [{WORD_COUNT_MIN}, {WORD_COUNT_MAX}]")
    if unsafe_hits:
        notes.append(f"unsafe word-list hit(s): {unsafe_hits}")

    return {
        "word_count": word_count,
        "unsafe_hits": unsafe_hits,
        "passed": length_ok and not unsafe_hits,
        "notes": notes,
    }
