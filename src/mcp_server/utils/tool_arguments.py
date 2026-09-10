"""Deterministic argument conformance against an MCP tool ``inputSchema``.

``call_external_tool`` is a generic proxy: an LLM composes ``arguments`` for
a tool whose schema lives on a remote server. The model routinely writes the
property names in its own convention (``job_id``) while the server declares
another (``jobId``); the remote then rejects the call and the model must
discover the schema and retry — or, more often, gives up and reports the
error. This module makes the proxy conform the call to the declared schema
BEFORE the network round-trip, so that class of failure never happens:

* ``normalize_arguments`` renames a key to the schema's property when the
  match is unambiguous (same letters/digits ignoring case, ``_`` and ``-``),
  recursing into nested ``object`` properties. It never invents values, never
  drops keys and never renames when two properties collapse to the same
  canonical form — the remote stays the authority for anything ambiguous.
* ``validate_arguments`` reports, with the schema in hand, what the remote
  would reject anyway: missing ``required`` properties and unknown keys when
  the schema declares ``additionalProperties: false``.

Only JSON-Schema fragments actually used by MCP servers are interpreted
(``properties``, ``required``, ``additionalProperties``, nested ``object``).
Anything else (``anyOf``, ``$ref``…) is passed through untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def canonical_key(key: str) -> str:
    """``job_id`` / ``jobId`` / ``JOB-ID`` → ``jobid``."""
    return _NON_ALNUM.sub("", key.lower())


def _properties(schema: Any) -> dict[str, Any]:
    props = schema.get("properties") if isinstance(schema, dict) else None
    return props if isinstance(props, dict) else {}


@dataclass
class Normalization:
    """Outcome of :func:`normalize_arguments`."""

    arguments: dict[str, Any]
    # ``old.path`` → ``new.path`` for every key that was renamed (dotted for
    # nested objects). Empty when the call already matched the schema.
    renamed: dict[str, str] = field(default_factory=dict)


def normalize_arguments(
    arguments: dict[str, Any], schema: Any, _path: str = ""
) -> Normalization:
    """Return ``arguments`` re-keyed to the schema's property names.

    A key is renamed only when (a) it is not itself a declared property,
    (b) exactly one declared property has the same canonical form and
    (c) that property was not also supplied explicitly. Values are never
    modified except to recurse into nested objects that the schema types as
    ``object`` with ``properties``.
    """
    props = _properties(schema)
    if not props:
        return Normalization(arguments=dict(arguments))

    canon_index: dict[str, list[str]] = {}
    for name in props:
        canon_index.setdefault(canonical_key(name), []).append(name)

    out: dict[str, Any] = {}
    renamed: dict[str, str] = {}
    for key, value in arguments.items():
        target = key
        if key not in props:
            candidates = canon_index.get(canonical_key(key), [])
            if (
                len(candidates) == 1
                and candidates[0] not in arguments
                and candidates[0] not in out
            ):
                target = candidates[0]
                renamed[f"{_path}{key}"] = f"{_path}{target}"

        sub_schema = props.get(target)
        if (
            isinstance(value, dict)
            and isinstance(sub_schema, dict)
            and sub_schema.get("type") == "object"
            and _properties(sub_schema)
        ):
            nested = normalize_arguments(value, sub_schema, f"{_path}{target}.")
            out[target] = nested.arguments
            renamed.update(nested.renamed)
        else:
            out[target] = value
    return Normalization(arguments=out, renamed=renamed)


@dataclass
class Validation:
    """Outcome of :func:`validate_arguments`; ``ok`` when nothing is wrong."""

    missing_required: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_required and not self.unknown


def validate_arguments(
    arguments: dict[str, Any], schema: Any, _path: str = ""
) -> Validation:
    """Report what the remote server would reject for ``arguments``.

    ``missing_required`` lists declared ``required`` properties absent from
    the call; ``unknown`` lists supplied keys that are not declared when the
    schema says ``additionalProperties: false`` (when additional properties
    are allowed — the JSON-Schema default — unknown keys are the server's
    business and are not reported). Both recurse into nested objects and use
    dotted paths.
    """
    result = Validation()
    if not isinstance(schema, dict):
        return result
    props = _properties(schema)

    required = schema.get("required")
    if isinstance(required, list):
        result.missing_required.extend(
            f"{_path}{name}"
            for name in required
            if isinstance(name, str) and name not in arguments
        )

    if props and schema.get("additionalProperties") is False:
        result.unknown.extend(f"{_path}{key}" for key in arguments if key not in props)

    for key, value in arguments.items():
        sub_schema = props.get(key)
        if (
            isinstance(value, dict)
            and isinstance(sub_schema, dict)
            and sub_schema.get("type") == "object"
        ):
            nested = validate_arguments(value, sub_schema, f"{_path}{key}.")
            result.missing_required.extend(nested.missing_required)
            result.unknown.extend(nested.unknown)
    return result
