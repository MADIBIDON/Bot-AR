"""Minimal UCP (Universal Commerce Protocol, https://ucp.dev) data model
— just enough to publish our own agent profile and talk to a merchant's
MCP endpoint. Not a full UCP implementation: no cryptographic request
signing (Web Bot Auth), no capability negotiation beyond what's needed
to call the shopping tools already confirmed live on Kairyu/RelicTCG
(Phase 20 recon) — see ucp/client.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """A UCP "platform profile" — the document a calling agent publishes
    at a stable, redirect-free HTTPS URL so a merchant's UCP endpoint can
    fetch and validate it before answering any tool call (see
    https://ucp.dev/2026-08-25/schemas/ucp.json#/$defs/platform_schema).

    Only `version` and empty `services`/`payment_handlers` registries are
    required by the schema — we declare no services of our own (we only
    ever call a merchant's services) and no payment handlers (we never
    handle payment ourselves). No secret ever belongs here: this is a
    public document by definition.
    """

    version: str
    services: dict[str, list] = field(default_factory=dict)
    payment_handlers: dict[str, list] = field(default_factory=dict)
    capabilities: dict[str, list] = field(default_factory=dict)

    def to_dict(self) -> dict:
        ucp: dict[str, object] = {
            "version": self.version,
            "services": self.services,
            "payment_handlers": self.payment_handlers,
        }
        if self.capabilities:
            ucp["capabilities"] = self.capabilities
        return {"ucp": ucp}


@dataclass(frozen=True, slots=True)
class UCPDiscovery:
    """Parsed result of GET https://<shop_domain>/.well-known/ucp — a
    business profile. Only the fields this project actually uses."""

    version: str
    mcp_endpoint: str


@dataclass(frozen=True, slots=True)
class UCPToolError:
    """A JSON-RPC error returned by a merchant's MCP endpoint. `code` is
    the JSON-RPC error code; `kind` classifies it for
    purchase/engine.py's gates (see ucp/client.py::classify_error)."""

    code: int
    message: str
    kind: str
    detail: str | None = None
