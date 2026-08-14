"""Durable element addressing: build anchors from snapshot nodes, resolve
them against a live tree later.

This is the heart of the project. The whole thesis (deterministic replay
with zero model calls) lives or dies on whether an anchor recorded last
week still finds the same element today. ARCHITECTURE.md section 5 defines
the contract; the short version:

- Resolution is a scoring function, not a cascade of fallbacks. A cascade
  that ends in "search the window for the role" will happily bind the wrong
  element and report success, which is worse than failing.
- An anchor resolves only when the best candidate clears an accept
  threshold AND beats the runner-up by a clear margin. Two close candidates
  raise AnchorAmbiguous. No viable candidate raises AnchorLost. Both are
  the healer's cue. Guessing is banned.
- Identifiers outrank labels. Labels are localized and state-dependent (a
  Play button relabels itself Pause); AXIdentifier is set by developers and
  survives both. Labels still matter, so an anchor remembers every label it
  has seen the element wear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterator

from .ax import Element
from .snapshot import ChainLink, Node


class AnchorLost(RuntimeError):
    """No candidate scored high enough to accept."""


class AnchorAmbiguous(RuntimeError):
    """Two or more candidates scored too close to call. Guessing is banned."""


# Scoring weights. Tuned by the durability harness, not by taste; if you
# change one, rerun `vent harness` and put the new numbers in the commit.
W_IDENTIFIER = 6.0
W_ROLE = 3.0
W_LABEL = 2.0
W_CHAIN = 3.0
W_ORDINAL = 1.0
W_INDEX = 0.5

ACCEPT_THRESHOLD = 6.0
AMBIGUITY_MARGIN = 1.5

MAX_CANDIDATE_DEPTH = 30


@dataclass
class Anchor:
    """A durable address for one element."""

    role: str
    identifier: str
    labels: list[str]  # every label this element has been observed under
    window_title: str
    chain: list[ChainLink]

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "identifier": self.identifier,
            "labels": self.labels,
            "window_title": self.window_title,
            "chain": [vars(link) for link in self.chain],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Anchor":
        return cls(
            role=data["role"],
            identifier=data.get("identifier", ""),
            labels=list(data.get("labels", [])),
            window_title=data.get("window_title", ""),
            chain=[ChainLink(**link) for link in data.get("chain", [])],
        )


def build(node: Node) -> Anchor:
    """Create an anchor from a surfaced snapshot node."""
    return Anchor(
        role=node.role,
        identifier=node.identifier,
        labels=[node.label] if node.label else [],
        window_title=node.window_title,
        chain=list(node.chain),
    )


@dataclass
class _Candidate:
    element: Element
    chain: tuple[ChainLink, ...]
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)


def _walk_with_chains(root: Element, max_depth: int) -> Iterator[tuple[Element, tuple[ChainLink, ...]]]:
    """Yield every element with its ancestor chain, same shape as snapshot."""

    def visit(element: Element, chain: tuple[ChainLink, ...], depth: int):
        yield element, chain
        if depth >= max_depth:
            return
        role_counts: dict[str, int] = {}
        for index, child in enumerate(element.children()):
            role = child.role
            ordinal = role_counts.get(role, 0)
            role_counts[role] = ordinal + 1
            link = ChainLink(
                role=role,
                label=child.label,
                identifier=str(child.attribute("AXIdentifier") or ""),
                ordinal=ordinal,
                index=index,
            )
            yield from visit(child, chain + (link,), depth + 1)

    root_link = ChainLink(
        role=root.role,
        label=root.label,
        identifier=str(root.attribute("AXIdentifier") or ""),
        ordinal=0,
        index=0,
    )
    yield from visit(root, (root_link,), 0)


def _chain_similarity(recorded: list[ChainLink], candidate: tuple[ChainLink, ...]) -> float:
    """How alike two ancestor chains are, from 0 to 1.

    Compared as sequences of (role, label-or-identifier) so that an app
    update inserting or removing one wrapper AXGroup degrades the score a
    little instead of zeroing it.
    """

    def key(link: ChainLink) -> str:
        return f"{link.role}:{link.identifier or link.label}"

    return SequenceMatcher(
        None,
        [key(link) for link in recorded],
        [key(link) for link in candidate],
    ).ratio()


def _score(anchor: Anchor, element: Element, chain: tuple[ChainLink, ...]) -> tuple[float, dict]:
    role = element.role
    if role != anchor.role:
        return -1.0, {"disqualified": "role mismatch"}

    breakdown: dict[str, float] = {"role": W_ROLE}
    score = W_ROLE

    identifier = str(element.attribute("AXIdentifier") or "")
    if anchor.identifier:
        if identifier == anchor.identifier:
            score += W_IDENTIFIER
            breakdown["identifier"] = W_IDENTIFIER
        else:
            score -= W_IDENTIFIER / 2
            breakdown["identifier"] = -W_IDENTIFIER / 2

    label = element.label
    if anchor.labels:
        if label in anchor.labels:
            score += W_LABEL
            breakdown["label"] = W_LABEL
        elif label:
            score -= W_LABEL / 2
            breakdown["label"] = -W_LABEL / 2

    similarity = _chain_similarity(anchor.chain, chain)
    score += W_CHAIN * similarity
    breakdown["chain"] = round(W_CHAIN * similarity, 2)

    if anchor.chain and chain:
        recorded_leaf = anchor.chain[-1]
        candidate_leaf = chain[-1]
        ordinal_gap = abs(recorded_leaf.ordinal - candidate_leaf.ordinal)
        index_gap = abs(recorded_leaf.index - candidate_leaf.index)
        score += max(0.0, W_ORDINAL - 0.5 * ordinal_gap)
        score += max(0.0, W_INDEX - 0.1 * index_gap)
        breakdown["position"] = round(
            max(0.0, W_ORDINAL - 0.5 * ordinal_gap) + max(0.0, W_INDEX - 0.1 * index_gap), 2
        )

    return score, breakdown


def resolve(root: Element, anchor: Anchor, low_confidence: bool = False) -> Element:
    """Find the element an anchor describes, or refuse.

    low_confidence raises the accept threshold. Pack loading sets it when
    the app version or locale no longer matches what the pack recorded,
    so a stale pack fails toward the healer instead of toward a plausible
    wrong element.
    """
    threshold = ACCEPT_THRESHOLD + (2.0 if low_confidence else 0.0)

    candidates: list[_Candidate] = []
    for element, chain in _walk_with_chains(root, MAX_CANDIDATE_DEPTH):
        score, breakdown = _score(anchor, element, chain)
        if score >= 0:
            candidates.append(_Candidate(element=element, chain=chain, score=score, breakdown=breakdown))

    if not candidates:
        raise AnchorLost(f"No element with role {anchor.role} found at all")

    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]

    if best.score < threshold:
        raise AnchorLost(
            f"Best candidate for {anchor.role} {anchor.labels or anchor.identifier} "
            f"scored {best.score:.1f}, below threshold {threshold:.1f} ({best.breakdown})"
        )

    if len(candidates) > 1 and best.score - candidates[1].score < AMBIGUITY_MARGIN:
        raise AnchorAmbiguous(
            f"Two candidates for {anchor.role} scored {best.score:.1f} and "
            f"{candidates[1].score:.1f}, within the ambiguity margin. Refusing to guess."
        )

    return best.element
