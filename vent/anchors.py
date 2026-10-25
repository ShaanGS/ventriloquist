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
- Window identity is part of the address. AppKit window identifiers are
  nib-derived and identical across documents, so the window title is the
  only thing separating "vent-demo.txt" from "taxes-2025.txt". A candidate
  in a window whose non-empty title differs from the recorded one is
  disqualified, the same rule as a conflicting identifier; a penalty was
  outscored by the documents' shared identifier, and a tool recorded in
  vent-demo.txt overwrote taxes-2025.txt.
- An anchor with no recorded window title is window-agnostic on purpose
  (a shared pack's "frontmost document" tool). When its close candidates
  sit in different windows, the one in the app's focused window wins,
  because that is the document the user is looking at. Ties inside one
  window, and ties for title-pinned anchors, still refuse.

Scoring reads only the chain links captured during the walk, never the
live element again; every attribute read is a cross-process call and the
walk already paid for them once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterator, Optional

from .ax import Element
from .snapshot import MAX_TREE_DEPTH, ChainLink, Node, app_children


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
        from dataclasses import fields as dc_fields

        known = {f.name for f in dc_fields(ChainLink)}
        return cls(
            role=data.get("role", ""),
            identifier=data.get("identifier", ""),
            labels=list(data.get("labels", [])),
            window_title=data.get("window_title", ""),
            # Unknown keys from future format versions are dropped rather
            # than crashing; packs.py gates on format_version for real
            # incompatibilities.
            chain=[
                ChainLink(**{k: v for k, v in link.items() if k in known})
                for link in data.get("chain", [])
            ],
        )


def build(node: Node) -> Anchor:
    """Create an anchor from a surfaced snapshot node.

    Window links in the chain get their labels blanked: window identity is
    scored through the anchor's own window_title field, and window labels
    embed volatile user data (document names, "Notes - 38 notes") that
    would otherwise leak into shared packs and booby-trap future scoring.
    """
    chain = [
        ChainLink(
            role=link.role,
            label="" if link.role == "AXWindow" else link.label,
            identifier=link.identifier,
            ordinal=link.ordinal,
            index=link.index,
            subrole=link.subrole,
        )
        for link in node.chain
    ]
    return Anchor(
        role=node.role,
        identifier=node.identifier,
        labels=[node.label] if node.label else [],
        window_title=node.window_title,
        chain=chain,
    )


@dataclass
class _Candidate:
    element: Element
    chain: tuple[ChainLink, ...]
    score: float = 0.0
    breakdown: dict = field(default_factory=dict)


def _walk_with_chains(
    root: Element, max_depth: int, include_menus: bool = True
) -> Iterator[tuple[Element, tuple[ChainLink, ...]]]:
    """Yield every element with its ancestor chain, same shape as snapshot.

    With include_menus off, menu bar subtrees are skipped wherever they
    appear, the rule snapshot applies by default. Indices still count the
    skipped menu bars, so chains line up with a snapshot's link for link.
    The root's children come from app_children, as in a snapshot, so a
    window on another Space is still searched.
    """

    # Ancestor-path cycle guard, not a global visited set: AX trees are
    # DAGs and the same element can be legitimately reachable through two
    # paths. Only a true ancestor cycle (an element inside itself) is cut.
    path_keys: set[int] = set()

    def visit(element: Element, chain: tuple[ChainLink, ...], depth: int):
        key = element.ref_key()
        if key is not None and key in path_keys:
            return
        if key is not None:
            path_keys.add(key)
        try:
            yield element, chain
            if depth >= max_depth:
                return
            role_counts: dict[str, int] = {}
            children = app_children(element) if depth == 0 else element.children()
            for index, child in enumerate(children):
                role = child.role
                ordinal = role_counts.get(role, 0)
                role_counts[role] = ordinal + 1
                if not include_menus and role == "AXMenuBar":
                    continue
                link = ChainLink(
                    role=role,
                    label=child.label,
                    identifier=str(child.attribute("AXIdentifier") or ""),
                    ordinal=ordinal,
                    index=index,
                    subrole=child.subrole,
                )
                yield from visit(child, chain + (link,), depth + 1)
        finally:
            if key is not None:
                path_keys.discard(key)

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

    candidate_title = _candidate_window_title(chain)
    if anchor.window_title and candidate_title and candidate_title != anchor.window_title:
        # Another document, not a degraded match (see the module docstring).
        # An untitled window is still searched, as before.
        return -1.0, {"disqualified": "window title mismatch"}

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

    if anchor.window_title and candidate_title == anchor.window_title:
        score += W_WINDOW
        breakdown["window"] = W_WINDOW

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

    return score, breakdown


def semantic_drift(anchor: Anchor, element: Element) -> bool:
    """Whether a resolved element wears a label the anchor has never seen.

    Surviving a relabel is durability. Surviving a relabel the anchor has
    no record of may mean the control's meaning changed while its position
    did not (SECURITY.md T11): an archive button becoming a delete button
    binds just as confidently. Electron apps relabel server-side with no
    version bump, so this is judged on the mismatch itself. The caller
    decides what to do; this only says "a human should look".
    """
    if not anchor.labels:
        return False
    live = element.label
    return bool(live) and live not in anchor.labels


def in_menu_bar(anchor: Anchor) -> bool:
    """Whether an anchor may point into a menu bar.

    Snapshots leave menu bars out unless exploration asks for them, so an
    anchor whose recorded chain never passes through an AXMenuBar cannot
    live in one. An anchor with no chain can't rule it out.
    """
    return not anchor.chain or any(link.role == "AXMenuBar" for link in anchor.chain)


def resolve(root: Element, anchor: Anchor, low_confidence: bool = False) -> Element:
    """Find the element an anchor describes, or refuse.

    low_confidence raises the accept threshold. Pack loading sets it when
    the app version or locale no longer matches what the pack recorded,
    so a stale pack fails toward the healer instead of toward a plausible
    wrong element.
    """
    threshold = ACCEPT_THRESHOLD + (2.0 if low_confidence else 0.0)

    # The menu bar is most of a native app's tree (about 365 of TextEdit's
    # roughly 410 elements), and walking it was most of every tool call, so
    # it is walked only for anchors that can live there.
    candidates: list[_Candidate] = []
    ruled_out: dict[str, int] = {}
    for element, chain in _walk_with_chains(root, MAX_TREE_DEPTH, in_menu_bar(anchor)):
        score, breakdown = _score(anchor, chain)
        if score >= 0:
            candidates.append(_Candidate(element=element, chain=chain, score=score, breakdown=breakdown))
        elif breakdown["disqualified"] != "role mismatch":
            reason = breakdown["disqualified"]
            ruled_out[reason] = ruled_out.get(reason, 0) + 1

    if not candidates:
        # Say what was ruled out, so "lost" can be told apart from "found,
        # but in another window".
        why = ", ".join(f"{count} {reason}" for reason, count in ruled_out.items())
        raise AnchorLost(
            f"No viable element with role {anchor.role} found" + (f" (ruled out: {why})" if why else "")
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]

    if best.score < threshold:
        raise AnchorLost(
            f"Best candidate for {anchor.role} {anchor.labels or anchor.identifier} "
            f"scored {best.score:.1f}, below threshold {threshold:.1f} ({best.breakdown})"
        )

    if len(candidates) > 1 and best.score - candidates[1].score < AMBIGUITY_MARGIN:
        settled = None if anchor.window_title else _settle_by_focused_window(root, candidates, threshold)
        if settled is None:
            raise AnchorAmbiguous(
                f"Two candidates for {anchor.role} scored {best.score:.1f} and "
                f"{candidates[1].score:.1f}, within the ambiguity margin. Refusing to guess."
            )
        return settled.element

    return best.element


def _settle_by_focused_window(
    root: Element, candidates: list[_Candidate], threshold: float
) -> Optional[_Candidate]:
    """The one close candidate inside the app's focused window, or None.

    Only called for window-agnostic anchors. Returns None, so the caller
    refuses, when the app reports no focused window, when no close
    candidate is in it, or when more than one is.
    """
    focused_index = _focused_window_index(root)
    if focused_index is None:
        return None
    best = candidates[0]
    in_focus = [
        c
        for c in candidates
        if best.score - c.score < AMBIGUITY_MARGIN
        and _top_level_window_index(c.chain) == focused_index
    ]
    if len(in_focus) != 1 or in_focus[0].score < threshold:
        return None
    return in_focus[0]


def _focused_window_index(root: Element) -> Optional[int]:
    """Position of the app's focused window among the root's children,
    counted the way the walk counts them."""
    focused = root.attribute("AXFocusedWindow")
    if focused is None:
        return None
    for index, child in enumerate(app_children(root)):
        if child.wraps(focused):
            return index
    return None


def _top_level_window_index(chain: tuple[ChainLink, ...]) -> Optional[int]:
    """The candidate's window position, matching _focused_window_index."""
    if len(chain) > 1 and chain[1].role == "AXWindow":
        return chain[1].index
    return None
