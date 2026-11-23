"""Text normalization shared by deduplication and lexical search.

``normalize_text`` turns titles and abstracts into lowercase ASCII words:
HTML tags and entities removed, accents stripped, Greek letters spelled out
("TNF-α" -> "tnf alpha"), and everything else that isn't a letter or digit
treated as a word break.
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import List, Optional

_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "ζ": "zeta", "η": "eta",
    "θ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu", "ξ": "xi",
    "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "χ": "chi", "ψ": "psi", "ω": "omega",
}
_TAG = re.compile(r"<[^>]+>")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

STOPWORDS = frozenset(
    "a an and are as at be by for from in into is of on or the to with via its their this that these "
    "using use based between during after before among within without".split()
)


def normalize_text(text: Optional[str]) -> str:
    """Lowercase ASCII words: tags and accents stripped, Greek letters spelled out."""
    if not text:
        return ""
    t = html.unescape(_TAG.sub(" ", text))
    t = "".join(_GREEK.get(ch, _GREEK.get(ch.lower(), ch)) for ch in t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return " ".join(_NON_ALNUM.sub(" ", t).split())


def stem(token: str) -> str:
    """A deliberately light plural stemmer: 'variants' -> 'variant', 'viruses' -> 'virus', 'studies' -> 'study'.

    Light on purpose: aggressive stemming conflates scientific terms
    ("organism"/"organization"), and BM25 recall is backed up by semantic search.
    """
    if len(token) <= 4 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 5:
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is", "ys")):
        return token[:-1]
    return token


def search_tokens(text: Optional[str]) -> List[str]:
    """Tokens for lexical search: normalized, stopwords removed, lightly stemmed (order and repeats kept)."""
    return [stem(t) for t in normalize_text(text).split() if t not in STOPWORDS]
