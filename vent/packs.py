"""Pack schema: load, save, and validate compiled app capabilities.

A pack is the durable artifact the whole project exists to produce. It is
JSON on disk, human-readable and human-editable on purpose. Hand-writing a
pack is the supported way to bootstrap an app before the explorer handles
it. See ARCHITECTURE.md section 5 for the format rationale and SECURITY.md
T7 for why validation here is strict.

The op set is closed. Adding an op is a breaking change to the threat model
and requires updating SECURITY.md first. That rule is enforced socially,
but the validator enforces the current set mechanically: unknown ops are
rejected at load, so a tampered or hand-edited pack cannot make the runtime
do anything the threat model has not named.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .anchors import Anchor

OPS = {"press", "set_value", "pick", "reveal", "raise_window", "open_app", "wait_for", "read_value"}
RISK_LEVELS = {"read_only", "mutating", "high"}
VERIFY_KINDS = {"value_contains", "value_equals", "element_exists"}
PRECONDITION_KINDS = {"app_running", "window_exists"}
FORMAT_VERSION = 2
READABLE_VERSIONS = {1, 2}  # version 2 added ChainLink.subrole


class PackError(ValueError):
    """A pack failed validation. The message says exactly where."""


@dataclass
class Step:
    op: str
    anchor: Optional[Anchor] = None
    value: Optional[dict] = None  # {"param": name} or {"literal": text}
    expect: Optional[dict] = None  # {"role": "AXTextArea"}
    action: str = "AXPress"  # AX action name, for press steps
    timeout_s: float = 5.0
    settle: bool = True  # opt out for steps whose effect verify covers

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"op": self.op}
        if self.anchor:
            data["anchor"] = self.anchor.to_dict()
        if self.value:
            data["value"] = self.value
        if self.expect:
            data["expect"] = self.expect
        if self.action != "AXPress":
            data["action"] = self.action
        if self.timeout_s != 5.0:
            data["timeout_s"] = self.timeout_s
        if not self.settle:
            data["settle"] = False
        return data

    @classmethod
    def from_dict(cls, data: dict, where: str) -> "Step":
        op = data.get("op")
        if op not in OPS:
            raise PackError(f"{where}: unknown op {op!r}. Allowed: {sorted(OPS)}")
        anchor = Anchor.from_dict(data["anchor"]) if "anchor" in data else None
        if op in {"press", "set_value", "pick", "reveal", "read_value", "wait_for"} and anchor is None:
            raise PackError(f"{where}: op {op!r} requires an anchor")
        return cls(
            op=op,
            anchor=anchor,
            value=data.get("value"),
            expect=data.get("expect"),
            action=data.get("action", "AXPress"),
            timeout_s=float(data.get("timeout_s", 5.0)),
            settle=bool(data.get("settle", True)),
        )


@dataclass
class Verify:
    kind: str
    anchor: Anchor
    param: Optional[str] = None
    literal: Optional[str] = None

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"kind": self.kind, "anchor": self.anchor.to_dict()}
        if self.param:
            data["param"] = self.param
        if self.literal:
            data["literal"] = self.literal
        return data

    @classmethod
    def from_dict(cls, data: dict, where: str) -> "Verify":
        kind = data.get("kind")
        if kind not in VERIFY_KINDS:
            raise PackError(f"{where}: unknown verify kind {kind!r}")
        if "anchor" not in data:
            raise PackError(f"{where}: verify requires an anchor")
        return cls(
            kind=kind,
            anchor=Anchor.from_dict(data["anchor"]),
            param=data.get("param"),
            literal=data.get("literal"),
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    risk: str
    steps: list[Step]
    params: dict[str, dict] = field(default_factory=dict)
    preconditions: list[dict] = field(default_factory=list)
    verify: list[Verify] = field(default_factory=list)
    requires_frontmost: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "requires_frontmost": self.requires_frontmost,
            "params": self.params,
            "preconditions": self.preconditions,
            "steps": [s.to_dict() for s in self.steps],
            "verify": [v.to_dict() for v in self.verify],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ToolSpec":
        name = data.get("name") or ""
        if not name.replace("_", "").isalnum():
            raise PackError(f"tool name {name!r} must be snake_case alphanumeric")
        risk = data.get("risk")
        if risk not in RISK_LEVELS:
            raise PackError(f"tool {name}: risk must be one of {sorted(RISK_LEVELS)}, got {risk!r}")
        for pre in data.get("preconditions", []):
            if pre.get("kind") not in PRECONDITION_KINDS:
                raise PackError(f"tool {name}: unknown precondition {pre.get('kind')!r}")
        steps = [
            Step.from_dict(s, where=f"tool {name} step {i}")
            for i, s in enumerate(data.get("steps", []))
        ]
        if not steps:
            raise PackError(f"tool {name}: needs at least one step")
        params = data.get("params", {})
        for step in steps:
            if step.value and "param" in step.value and step.value["param"] not in params:
                raise PackError(
                    f"tool {name}: step references param {step.value['param']!r} "
                    f"which is not declared"
                )
        return cls(
            name=name,
            description=data.get("description", ""),
            risk=risk,
            requires_frontmost=bool(data.get("requires_frontmost", False)),
            params=params,
            preconditions=data.get("preconditions", []),
            steps=steps,
            verify=[
                Verify.from_dict(v, where=f"tool {name} verify {i}")
                for i, v in enumerate(data.get("verify", []))
            ],
        )


@dataclass
class Pack:
    bundle_id: str
    app_name: str
    tools: list[ToolSpec]
    format_version: int = FORMAT_VERSION
    app_version: str = ""
    os_version: str = ""
    locale: str = ""
    healed_pending: list[dict] = field(default_factory=list)

    def tool(self, name: str) -> ToolSpec:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise PackError(f"pack {self.bundle_id} has no tool {name!r}")

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "bundle_id": self.bundle_id,
            "app_name": self.app_name,
            "app_version": self.app_version,
            "os_version": self.os_version,
            "locale": self.locale,
            "tools": [t.to_dict() for t in self.tools],
            "healed_pending": self.healed_pending,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Pack":
        version = data.get("format_version")
        if version not in READABLE_VERSIONS:
            raise PackError(
                f"unsupported pack format_version {version!r}, readable: {sorted(READABLE_VERSIONS)}"
            )
        if not data.get("bundle_id"):
            raise PackError("pack is missing bundle_id")
        if not data.get("app_name"):
            raise PackError("pack is missing app_name")
        tools = [ToolSpec.from_dict(t) for t in data.get("tools", [])]
        names = [t.name for t in tools]
        if len(names) != len(set(names)):
            raise PackError("duplicate tool names in pack")
        return cls(
            bundle_id=data["bundle_id"],
            app_name=data.get("app_name", ""),
            app_version=data.get("app_version", ""),
            os_version=data.get("os_version", ""),
            locale=data.get("locale", ""),
            tools=tools,
            healed_pending=data.get("healed_pending", []),
        )


def load(path: Path) -> Pack:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise PackError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return Pack.from_dict(data)
    except PackError as exc:
        raise PackError(f"{path}: {exc}") from exc
    except (TypeError, KeyError, ValueError) as exc:
        # Malformed structure from a future version or hand edit. Same
        # outcome as any invalid pack: rejected with the file named.
        raise PackError(f"{path}: malformed pack ({exc!r})") from exc


def save(pack: Pack, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pack.to_dict(), indent=2) + "\n")


def load_all(packs_dir: Path) -> list[Pack]:
    """Load every pack, skipping invalid ones with a warning on stderr.
    One broken pack should not take down the server for the rest."""
    import sys

    packs = []
    for pack_file in sorted(Path(packs_dir).glob("*/pack.json")):
        try:
            packs.append(load(pack_file))
        except PackError as exc:
            print(f"warning: skipping {exc}", file=sys.stderr)
    return packs


def is_stale(pack: Pack, app_version: str) -> bool:
    """True when the environment no longer matches what the pack recorded.

    A stale pack is not rejected; its anchors load as low-confidence, which
    raises the resolver's accept threshold so failures go to the healer
    instead of to a plausible wrong element (SECURITY.md T7).
    """
    if pack.app_version and app_version and pack.app_version != app_version:
        return True
    if pack.os_version and pack.os_version != platform.mac_ver()[0]:
        return True
    return False
