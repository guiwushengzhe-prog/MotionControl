"""Text folding used wherever two spoken phrases have to be compared.

``compact_text`` existed four times over -- voice_backend, hybrid_voice_backend,
sherpa_kws_backend and sherpa_wake_backend each carried a byte-identical copy.
Four copies of a comparison rule is three chances for them to drift, and the
cloud needs a fifth: it rejects a duplicate command word on upload, and it has
to call a phrase a duplicate under exactly the rule the desktop will apply when
the config comes back down.  So the rule lives here once.

The folding is deliberately aggressive about punctuation: a speech recogniser
may or may not insert a comma, so "向左，走" and "向左走" are the same command
as far as configuration is concerned, and allowing both to be bound separately
would make one utterance ambiguous.
"""

from __future__ import annotations

import re

# Whitespace (including the ideographic space) plus the CJK and ASCII sentence
# punctuation a recogniser might emit.  Anything in here is dropped, not
# replaced, so "向左 走" folds to "向左走" rather than keeping a gap.
_DROPPED = re.compile(r"[\s\u3000，。！？、,.!?;；:：]+")


def compact_text(value: str) -> str:
    """Fold *value* to the form used for comparing spoken phrases."""
    return _DROPPED.sub("", str(value or "").strip().lower())
