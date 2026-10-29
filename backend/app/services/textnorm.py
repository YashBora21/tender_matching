"""Text normalisation shared by the reader, the embedder and the verifier.

A quote is checked against the page it came from, and the page is stored as a
chunk. The two only compare if both are normalised the same way. The rules
live here so the reader that writes a chunk and the verifier that reads a
quote cannot drift apart.

Normalisation is deliberately lossless about meaning: it removes control
characters a PDF's text layer carries, applies Unicode compatibility folding
(so a ligature and the two letters it stands for are the same text), and
collapses every run of whitespace to one space. It does not rewrite words,
numbers or punctuation.
"""

from __future__ import annotations

import re
import unicodedata

# Control characters a PDF's text layer can carry and PostgreSQL will not
# store: a NUL byte in a text column is an error, not a character. A vertical
# tab or form feed separates text rather than decorating it, so it becomes a
# space before the rest are removed: deleting it would join two words.
_CONTROL_SPACE = re.compile(r"[\x0b\x0c]")
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def strip_control(text: str) -> str:
    """Remove the control characters a text layer can carry."""
    spaced = _CONTROL_SPACE.sub(" ", text or "")
    return _CONTROL.sub("", spaced)


def normalise_text(text: str) -> str:
    """The form a quote and a page are both compared in.

    The reader stores chunks with their control characters removed and their
    lines joined. A verifier that only collapsed spaces would still miss a
    quote whose page carried a form-feed or a compatibility ligature.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", strip_control(text))
    return _WHITESPACE.sub(" ", folded).strip()
