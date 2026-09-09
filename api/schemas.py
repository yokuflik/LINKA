"""Shared API primitives (ADR 0030).

Domain request/response models now live in each feature package's own
``schemas.py`` (``modules/<feature>/schemas.py``). This module keeps only
cross-cutting types every feature depends on. It must not import from
``modules/`` - that would create an import cycle with the feature schemas,
which import ``IdStr`` from here.
"""

from typing import Annotated

from pydantic import BeforeValidator

# Every id in this codebase is a 64-bit Snowflake. JavaScript's JSON.parse
# (and fetch().json(), and JSON.parse on a WebSocket message) decodes JSON
# numbers as IEEE-754 doubles, which only represent integers exactly up to
# 2^53-1 - about 9 quadrillion. Our ids are ~3.5 * 10^17, so roughly 95% of
# them get silently corrupted the instant a browser parses one (confirmed:
# this is exactly what broke chat creation - a corrupted other_user_id
# pointed at a nonexistent user, and the participant insert failed silently).
# Emitting ids as JSON strings instead means the client never runs them
# through Number at all - request bodies still parse a numeric string back
# to an exact int with no special handling needed (Pydantic does that natively).
IdStr = Annotated[str, BeforeValidator(str)]

__all__ = ["IdStr"]
