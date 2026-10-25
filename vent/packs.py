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
import math
import platform
import re
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

# The AX actions a press step may perform. An open action name would reopen
# the closed op set: an app decides for itself what an action it defines
# does, so only the standard ones get through.
PRESS_ACTIONS = {
    "AXPress", "AXConfirm", "AXShowMenu", "AXIncrement", "AXDecrement", "AXPick", "AXCancel", "AXRaise",
}

# JSON Schema types a parameter may declare. Arguments are typed into fields
# and compared with what fields show, so only scalars make sense.
PARAM_TYPES = {"string", "number", "integer", "boolean"}
# What MCP clients accept as an input property name.
PARAM_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# A step's own wait. The server runs one call per app at a time, so a step
# allowed to wait for an hour would hold up every later call to its app.
MAX_TIMEOUT_S = 120.0


class PackError(ValueError):
    """A pack failed validation. The message says exactly where."""


def _kind(value: Any) -> str:
    """A JSON type's name, for messages about the wrong one."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return {dict: "object", list: "array", str: "string", type(None): "null"}.get(type(value), type(value).__name__)


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise PackError(f"{where} must be a JSON object, not {_kind(value)}")
    return value


def _array(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise PackError(f"{where} must be a JSON array, not {_kind(value)}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise PackError(f"{where} must be a string, not {_kind(value)}")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise PackError(f"{where} must be true or false, not {_kind(value)}")
    return value


def _scalar(value: Any, where: str) -> Any:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PackError(f"{where} must be a string or a number, not {_kind(value)}")
    return value


def _choice(value: Any, allowed: set, where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PackError(f"{where} {value!r} is not one of {sorted(allowed)}")
    return value


LINK_TEXT = ("role", "label", "identifier")
LINK_NUMBERS = ("ordinal", "index")


def _anchor(data: Any, where: str) -> Anchor:
    """Check an anchor's shape, then build it."""
    data = _object(data, f"{where} anchor")
    if not isinstance(data.get("role"), str) or not data["role"]:
        raise PackError(f"{where} anchor needs a role")
    for key in ("identifier", "window_title", "subrole"):
        if key in data:
            _text(data[key], f"{where} anchor {key}")
    for index, label in enumerate(_array(data.get("labels", []), f"{where} anchor labels")):
        _text(label, f"{where} anchor label {index}")
    for index, link in enumerate(_array(data.get("chain", []), f"{where} anchor chain")):
        link_where = f"{where} anchor chain link {index}"
        link = _object(link, link_where)
        for key in LINK_TEXT:
            _text(link.get(key), f"{link_where} {key}")
        for key in LINK_NUMBERS:
            if isinstance(link.get(key), bool) or not isinstance(link.get(key), int):
                raise PackError(f"{link_where} {key} must be a whole number, not {_kind(link.get(key))}")
        if "subrole" in link:
            _text(link["subrole"], f"{link_where} subrole")
    return Anchor.from_dict(data)


def _timeout(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PackError(f"{where} timeout_s must be a number of seconds, not {_kind(value)}")
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_TIMEOUT_S:
        raise PackError(f"{where} timeout_s must be more than 0 and at most {MAX_TIMEOUT_S:.0f} seconds, got {value!r}")
    return seconds


def _value_spec(value: Any, where: str) -> dict:
    value = _object(value, f"{where} value")
    if len(value) != 1 or not ({"param", "literal"} & value.keys()):
        raise PackError(f'{where} value must be {{"param": name}} or {{"literal": text}}')
    if "param" in value:
        _text(value["param"], f"{where} value param")
    else:
        _scalar(value["literal"], f"{where} value literal")
    return value


def _params(value: Any, tool: str) -> dict[str, dict]:
    """A tool's parameters: each name maps to a schema object with a scalar
    JSON Schema type. The server copies these into the tool's inputSchema,
    so a list or a bare string here once loaded and then broke tools/list."""
    params = _object(value, f"tool {tool} params")
    for name, meta in params.items():
        if not PARAM_NAME.fullmatch(name):
            raise PackError(f"tool {tool}: param name {name!r} must be 1 to 64 letters, digits, '_', '.' or '-'")
        where = f"tool {tool} param {name!r}"
        if not isinstance(meta, dict):
            raise PackError(f'{where} must be a schema object such as {{"type": "string"}}, not {_kind(meta)}')
        _choice(meta.get("type", "string"), PARAM_TYPES, f"{where} type")
        _text(meta.get("description", ""), f"{where} description")
    return params


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
        data = _object(data, where)
        op = data.get("op")
        if not isinstance(op, str) or op not in OPS:
            raise PackError(f"{where}: unknown op {op!r}. Allowed: {sorted(OPS)}")
        anchor = _anchor(data["anchor"], where) if "anchor" in data else None
        if op in {"press", "set_value", "pick", "reveal", "read_value", "wait_for"} and anchor is None:
            raise PackError(f"{where}: op {op!r} requires an anchor")
        value = _value_spec(data["value"], where) if data.get("value") is not None else None
        if op == "set_value" and value is None:
            raise PackError(f"{where}: set_value needs a value, a param or a literal")
        expect = data.get("expect")
        if expect is not None:
            expect = _object(expect, f"{where} expect")
            if "role" in expect:
                _text(expect["role"], f"{where} expect role")
        action = data.get("action", "AXPress")
        if not isinstance(action, str) or action not in PRESS_ACTIONS:
            raise PackError(f"{where}: action {action!r} is not allowed. Allowed: {sorted(PRESS_ACTIONS)}")
        return cls(
            op=op,
            anchor=anchor,
            value=value,
            expect=expect,
            action=action,
            timeout_s=_timeout(data.get("timeout_s", 5.0), where),
            settle=_flag(data.get("settle", True), f"{where} settle"),
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
        data = _object(data, where)
        kind = data.get("kind")
        if not isinstance(kind, str) or kind not in VERIFY_KINDS:
            raise PackError(f"{where}: unknown verify kind {kind!r}")
        if "anchor" not in data:
            raise PackError(f"{where}: verify requires an anchor")
        param = data.get("param")
        literal = data.get("literal")
        return cls(
            kind=kind,
            anchor=_anchor(data["anchor"], where),
            param=None if param is None else _text(param, f"{where} param"),
            literal=None if literal is None else _scalar(literal, f"{where} literal"),
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
        data = _object(data, "a tool")
        name = data.get("name") or ""
        if not isinstance(name, str) or not name.replace("_", "").isalnum():
            raise PackError(f"tool name {name!r} must be snake_case alphanumeric")
        risk = data.get("risk")
        if not isinstance(risk, str) or risk not in RISK_LEVELS:
            raise PackError(f"tool {name}: risk must be one of {sorted(RISK_LEVELS)}, got {risk!r}")
        preconditions = _array(data.get("preconditions", []), f"tool {name} preconditions")
        for index, pre in enumerate(preconditions):
            kind = _object(pre, f"tool {name} precondition {index}").get("kind")
            if not isinstance(kind, str) or kind not in PRECONDITION_KINDS:
                raise PackError(f"tool {name}: unknown precondition {kind!r}")
        steps = [
            Step.from_dict(s, where=f"tool {name} step {i}")
            for i, s in enumerate(_array(data.get("steps", []), f"tool {name} steps"))
        ]
        if not steps:
            raise PackError(f"tool {name}: needs at least one step")
        params = _params(data.get("params", {}), name)
        for step in steps:
            if step.value and "param" in step.value and step.value["param"] not in params:
                raise PackError(
                    f"tool {name}: step references param {step.value['param']!r} "
                    f"which is not declared"
                )
        verify = [
            Verify.from_dict(v, where=f"tool {name} verify {i}")
            for i, v in enumerate(_array(data.get("verify", []), f"tool {name} verify"))
        ]
        for check in verify:
            if check.param is not None and check.param not in params:
                raise PackError(f"tool {name}: verify references param {check.param!r} which is not declared")
        return cls(
            name=name,
            description=_text(data.get("description", ""), f"tool {name} description"),
            risk=risk,
            requires_frontmost=_flag(data.get("requires_frontmost", False), f"tool {name} requires_frontmost"),
            params=params,
            preconditions=preconditions,
            steps=steps,
            verify=verify,
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
        data = _object(data, "a pack")
        version = data.get("format_version")
        if isinstance(version, bool) or not isinstance(version, int) or version not in READABLE_VERSIONS:
            raise PackError(
                f"unsupported pack format_version {version!r}, readable: {sorted(READABLE_VERSIONS)}"
            )
        if not data.get("bundle_id"):
            raise PackError("pack is missing bundle_id")
        _text(data["bundle_id"], "bundle_id")
        if not data.get("app_name"):
            raise PackError("pack is missing app_name")
        _text(data["app_name"], "app_name")
        tools = [
            ToolSpec.from_dict(_object(tool, f"tool {index}"))
            for index, tool in enumerate(_array(data.get("tools", []), "tools"))
        ]
        names = [t.name for t in tools]
        if len(names) != len(set(names)):
            raise PackError("duplicate tool names in pack")
        healed_pending = _array(data.get("healed_pending", []), "healed_pending")
        for index, entry in enumerate(healed_pending):
            # The healer reuses these anchors on a break before any human
            # promotes them, so they pass the same checks as a step's.
            entry = _object(entry, f"healed_pending entry {index}")
            for key in ("original", "healed"):
                _anchor(entry.get(key), f"healed_pending entry {index} {key}")
        return cls(
            bundle_id=data["bundle_id"],
            app_name=data["app_name"],
            app_version=_text(data.get("app_version", ""), "app_version"),
            os_version=_text(data.get("os_version", ""), "os_version"),
            locale=_text(data.get("locale", ""), "locale"),
            tools=tools,
            healed_pending=healed_pending,
        )


def load(path: Path) -> Pack:
    """Read and validate one pack. Every way a file can be wrong, from bytes
    that are not UTF-8 to a list where an object belongs, comes out as a
    PackError naming the file, so a caller skipping broken packs never meets
    a stray AttributeError (SECURITY.md T7)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PackError(f"{path} could not be read: {exc.strerror or exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PackError(f"{path} is not UTF-8 text (byte {exc.start} is invalid)") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackError(f"{path} is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        raise PackError(f"{path} is nested too deeply to be a pack") from exc
    try:
        return Pack.from_dict(data)
    except PackError as exc:
        raise PackError(f"{path}: {exc}") from exc
    except (TypeError, KeyError, ValueError, AttributeError, RecursionError) as exc:
        # Malformed structure the checks above did not anticipate, from a
        # future version or a hand edit. Same outcome as any invalid pack:
        # rejected with the file named.
        raise PackError(f"{path}: malformed pack ({exc!r})") from exc


def save(pack: Pack, path: Path) -> None:
    """Write a pack atomically: a concurrent reader (a server healing while
    the user runs `vent verify`) never sees a half-written file. The temp
    file is in the same directory so os.replace is a same-filesystem rename."""
    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(pack.to_dict(), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pack-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge(base: Pack, added: Pack) -> Pack:
    """Add newly compiled tools to an existing pack for the same app.

    Tools in `added` replace same-name tools in `base` in place (the caller
    asks before that) and the rest are appended, so earlier approvals
    survive a second compile. Quarantined heals carry over. When the two
    were recorded on different app or macOS versions, the pack keeps
    `base`'s versions: its older tools still date from there, and a stale
    pack loads its anchors cautiously (SECURITY.md T7).
    """
    if base.bundle_id != added.bundle_id:
        raise PackError(f"cannot merge a {added.bundle_id} pack into a {base.bundle_id} pack")
    incoming = {tool.name: tool for tool in added.tools}
    tools = [incoming.pop(tool.name, tool) for tool in base.tools] + list(incoming.values())
    return Pack(
        bundle_id=base.bundle_id,
        app_name=added.app_name or base.app_name,
        tools=tools,
        app_version=base.app_version or added.app_version,
        os_version=base.os_version or added.os_version,
        locale=added.locale or base.locale,
        healed_pending=list(base.healed_pending),
    )


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


def load_search_path(dirs: list[Path]) -> list[Pack]:
    """Load packs from several directories, earlier directories winning.
    A pack shadows any pack with the same bundle id in a later directory,
    so a user's healed or re-recorded copy replaces the bundled one instead
    of colliding with it. Two packs for one app in the same directory are
    both kept, so the server's collision check still reports them. A
    directory that does not exist contributes nothing."""
    loaded: list[Pack] = []
    claimed: set[str] = set()
    for packs_dir in dirs:
        found = [pack for pack in load_all(packs_dir) if pack.bundle_id not in claimed]
        loaded.extend(found)
        claimed.update(pack.bundle_id for pack in found)
    return loaded


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
