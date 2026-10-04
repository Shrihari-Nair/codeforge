"""Memory write gate: deterministic rules between "the agent wants to remember X" and storage.

The agent proposes; this code decides. There is no "add everything" path.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field

MIN_CONFIDENCE = 0.7
MAX_FIELD_CHARS = 400
MAX_FACTS_PER_TASK = 5

_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
]
_INSTRUCTION_PATTERNS = [
    re.compile(r"(?i)ignore (all )?(previous|prior|above) (instructions|prompts)"),
    re.compile(r"(?i)\b(you must|always|never)\b.{0,40}\b(run|execute|call|send|curl|wget)\b"),
    re.compile(r"(?i)(curl|wget)\s+https?://"),
    re.compile(r"(?i)<\s*/?\s*(system|assistant|tool_result)\b"),
    re.compile(r"(?i)\bdisregard\b.{0,30}\b(rules|policy|instructions)\b"),
]


class FactProposal(BaseModel):
    subject: str
    predicate: str
    value: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, description="tool-call / observation ids")
    origin: Literal["trusted", "untrusted_only"] = "trusted"
    confirmed_by_verifier: bool = False


class Decision(BaseModel):
    accept: bool
    rule: str  # which rule decided
    reason: str


def contains_secret(text: str, secret_values: list[str] | None = None) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS) or any(
        v and v in text for v in (secret_values or [])
    )


def looks_like_instruction(text: str) -> bool:
    return any(p.search(text) for p in _INSTRUCTION_PATTERNS)


def _no(rule: str, reason: str) -> Decision:
    return Decision(accept=False, rule=rule, reason=reason)


def gate_fact(
    fact: FactProposal,
    *,
    already_known: bool = False,
    accepted_so_far: int = 0,
    secret_values: list[str] | None = None,
) -> Decision:
    text = " ".join([fact.subject, fact.predicate, fact.value])
    if not (fact.subject.strip() and fact.predicate.strip() and fact.value.strip()):
        return _no("non_empty", "subject, predicate and value are required")
    if max(len(fact.subject), len(fact.predicate), len(fact.value)) > MAX_FIELD_CHARS:
        return _no("length", f"fields are capped at {MAX_FIELD_CHARS} chars")
    if contains_secret(text, secret_values):
        return _no("no_secrets", "contains something that looks like a secret")
    if looks_like_instruction(text):
        return _no("no_instructions", "reads like an instruction, not a fact")
    if fact.confidence < MIN_CONFIDENCE:
        return _no("confidence", f"confidence {fact.confidence:.2f} < {MIN_CONFIDENCE}")
    if fact.origin == "untrusted_only" and not fact.confirmed_by_verifier:
        return _no("untrusted_origin", "only from untrusted content, not confirmed by the verifier")
    if not fact.evidence and not fact.confirmed_by_verifier:
        return _no("evidence", "no supporting evidence referenced")
    if accepted_so_far >= MAX_FACTS_PER_TASK:
        return _no("rate_limit", f"at most {MAX_FACTS_PER_TASK} facts per task")
    if already_known:
        return _no("duplicate", "an identical active fact exists")
    return Decision(accept=True, rule="accepted", reason="passed all write rules")


def gate_episode_text(text: str, secret_values: list[str] | None = None) -> Decision:
    """Free text that will be stored in an episode (lessons): same hygiene rules."""
    if contains_secret(text, secret_values):
        return _no("no_secrets", "looks like a secret")
    if looks_like_instruction(text):
        return _no("no_instructions", "reads like an instruction")
    if len(text) > 2 * MAX_FIELD_CHARS:
        return _no("length", "too long")
    return Decision(accept=True, rule="accepted", reason="ok")
