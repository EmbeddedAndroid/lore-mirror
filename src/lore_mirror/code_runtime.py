"""Shared exact code-ref syntax, independent of Git and filesystem state."""
from __future__ import annotations

import re

_REF = re.compile(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def code_ref_valid(value):
    """One syntax policy for exact head/tag names, never revision expressions."""
    return (isinstance(value, str) and len(value) <= 512 and bool(_REF.fullmatch(value))
        and ".." not in value and "//" not in value and not value.endswith(("/", "."))
        and all(not part.startswith(".") and not part.endswith(".lock") for part in value.split("/")))
