"""Pinned, offline planning estimate; never a provider billing upper bound."""

import hashlib
from functools import lru_cache
from pathlib import Path

from ..schema import encode

TOKENIZER_PATH = Path(__file__).with_name("data") / "deepseek_v4_tokenizer.json"
JUDGE_INPUT_MEASUREMENT = {
    "method": "deepseek_v4_complete_wire_bpe_v1",
    "package": "tokenizers",
    "package_version": "0.22.2",
    "tokenizer_sha256": (
        "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
    ),
    "add_special_tokens": False,
    "overhead": 256,
}


@lru_cache(maxsize=1)
def _tokenizer():
    try:
        import tokenizers

        raw = TOKENIZER_PATH.read_bytes()
        if (
            tokenizers.__version__ != JUDGE_INPUT_MEASUREMENT["package_version"]
            or hashlib.sha256(raw).hexdigest()
            != JUDGE_INPUT_MEASUREMENT["tokenizer_sha256"]
        ):
            raise ValueError
        # Data-only parser: no remote model loader or downloaded Python execution.
        return tokenizers.Tokenizer.from_str(raw.decode("utf-8"))
    except ImportError, OSError, ValueError:
        raise ValueError("local_judge_tokenizer_unverifiable") from None


def estimate_judge_wire(payload):
    """Count the complete canonical wire, including tools and serialized evidence."""
    tokenizer = _tokenizer()
    try:
        count = len(
            tokenizer.encode(
                encode(payload).decode("utf-8"), add_special_tokens=False
            ).ids
        )
    except Exception:
        raise ValueError("local_judge_tokenizer_unverifiable") from None
    return count + JUDGE_INPUT_MEASUREMENT["overhead"]
