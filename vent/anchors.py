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
  threshold AND beats the runner-up by a margin. Two close candidates raise
  AnchorAmbiguous; zero viable candidates raise AnchorLost. Both are the
  healer's cue. Guessing is banned.
- Identifiers outrank labels, and a conflicting identifier disqualifies:
  when the anchor recorded an identifier and the candidate carries a
  different non-empty one, that candidate is a different element, not a
  degraded match. Labels are localized and state-dependent (a Play button
  relabels itself Pause), so an anchor remembers every label it has seen
  the element wear and treats them as hints.
- Window identity is scored explicitly. AppKit window identifiers are
  nib-derived and identical across documents, so the window title is the
  only thing separating "vent-demo.txt" from "taxes-2025.txt". A recorded
  window title that mismatches costs heavily.

Scoring reads only the chain links captured during the walk, never the
live element again; every attribute read is a cross-process call and the
walk already paid for them once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterator

from .ax import Element
from .snapshot import MAX_TREE_DEPTH, ChainLink, Node


class AnchorLost(RuntimeError):
    """No candidate scored high enough to accept."""


class AnchorAmbiguous(RuntimeError):
    """Two or more candidates scored too close to call. Guessing is banned."""


# Scoring weights. These will be tuned by the durability harness planned in
# ARCHITECTURE.md section 10; when that lands, changes to these numbers
# should come with harness results in the commit message.
W_IDENTIFIER = 6.0
W_ROLE = 3.0
W_LABEL = 2.0
W_CHAIN = 3.0
W_WINDOW = 2.0
W_ORDINAL = 1.0
W_INDEX = 0.5
W_SUBROLE = 2.0

ACCEPT_THRESHOLD = 6.0
AMBIGUITY_MARGIN = 1.5


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


def _walk_with_chains(
    root: Element, max_depth: int
) -> Iterator[tuple[Element, tuple[ChainLink, ...]]]:
    """Yield every element with its ancestor chain, same shape as snapshot."""

    seen: set[int] = set()

    def visit(element: Element, chain: tuple[ChainLink, ...], depth: int):
        key = element.ref_key()
        if key is not None:
            if key in seen:
                return
            seen.add(key)
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
                subrole=child.subrole,
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


def _ancestry_similarity(recorded: list[ChainLink], candidate: tuple[ChainLink, ...]) -> float:
    """How alike two ancestor chains are, from 0 to 1.

    The leaf is excluded: its role, identifier, and label already carry
    their own (heavier) weights, and counting them twice inflated
    wrong-element scores in review. Window links are keyed by role only,
    because window identity is scored separately against the recorded
    window title; keying windows by their document-name labels would break
    every anchor the moment a differently named document opened.
    """

    def key(link: ChainLink) -> str:
        if link.role == "AXWindow":
            return link.role
        return f"{link.role}:{link.identifier or link.label}"

    return SequenceMatcher(
        None,
        [key(link) for link in recorded[:-1]],
        [key(link) for link in candidate[:-1]],
    ).ratio()


def _candidate_window_title(chain: tuple[ChainLink, ...]) -> str:
    for link in chain:
        if link.role == "AXWindow":
            return link.label
    return ""


def _score(anchor: Anchor, chain: tuple[ChainLink, ...]) -> tuple[float, dict]:
    """Score one candidate from its recorded chain links alone."""
    leaf = chain[-1]

    if leaf.role != anchor.role:
        return -1.0, {"disqualified": "role mismatch"}

    if anchor.identifier and leaf.identifier and leaf.identifier != anchor.identifier:
        # A different non-empty identifier is a different element. This is
        # a disqualification, not a penalty: review showed a penalty could
        # be outscored by chain and position, binding the wrong element.
        return -1.0, {"disqualified": "identifier mismatch"}

    recorded_leaf = anchor.chain[-1] if anchor.chain else None
    if (
        recorded_leaf is not None
        and recorded_leaf.subrole
        and leaf.subrole
        and recorded_leaf.subrole != leaf.subrole
    ):
        # Subroles separate elements that look like twins by role alone:
        # a close button and a zoom button are both unlabeled AXButtons.
        return -1.0, {"disqualified": "subrole mismatch"}

    breakdown: dict[str, float] = {"role": W_ROLE}
    score = W_ROLE

    if anchor.identifier:
        if leaf.identifier == anchor.identifier:
            score += W_IDENTIFIER
            breakdown["identifier"] = W_IDENTIFIER
        else:
            # The candidate has no identifier at all. Possible after an app
            # update drops them wholesale, so degrade rather than disqualify.
            score -= W_IDENTIFIER / 2
            breakdown["identifier"] = -W_IDENTIFIER / 2

    if anchor.labels:
        if leaf.label in anchor.labels:
            score += W_LABEL
            breakdown["label"] = W_LABEL
        elif leaf.label:
            score -= W_LABEL / 2
            breakdown["label"] = -W_LABEL / 2

    if anchor.window_title:
        candidate_title = _candidate_window_title(chain)
        if candidate_title == anchor.window_title:
            score += W_WINDOW
            breakdown["window"] = W_WINDOW
        elif candidate_title:
            score -= W_WINDOW
            breakdown["window"] = -W_WINDOW

    similarity = _ancestry_similarity(anchor.chain, chain)
    score += W_CHAIN * similarity
    breakdown["chain"] = round(W_CHAIN * similarity, 2)

    if anchor.chain:
        recorded_leaf = anchor.chain[-1]
        ordinal_gap = abs(recorded_leaf.ordinal - leaf.ordinal)
        index_gap = abs(recorded_leaf.index - leaf.index)
        position = max(0.0, W_ORDINAL - 0.5 * ordinal_gap) + max(0.0, W_INDEX - 0.1 * index_gap)
        score += position
        breakdown["position"] = round(position, 2)

        if recorded_leaf.subrole and recorded_leaf.subrole == leaf.subrole:
            score += W_SUBROLE
            breakdown["subrole"] = W_SUBROLE

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
    for element, chain in _walk_with_chains(root, MAX_TREE_DEPTH):
        score, breakdown = _score(anchor, chain)
        if score >= 0:
            candidates.append(_Candidate(element=element, chain=chain, score=score, breakdown=breakdown))

    if not candidates:
        raise AnchorLost(f"No viable element with role {anchor.role} found")

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
