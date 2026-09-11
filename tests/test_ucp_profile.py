from __future__ import annotations

import json

from ucp.models import AgentProfile
from ucp.profile import UCP_VERSION, build_profile, write_profile


def test_build_profile_has_required_fields() -> None:
    profile = build_profile()

    assert profile.version == UCP_VERSION
    assert profile.services == {}
    assert profile.payment_handlers == {}


def test_to_dict_matches_minimal_platform_schema_shape() -> None:
    profile = AgentProfile(version="2026-08-25")

    document = profile.to_dict()

    assert document == {
        "ucp": {
            "version": "2026-08-25",
            "services": {},
            "payment_handlers": {},
        }
    }


def test_to_dict_omits_empty_capabilities_but_includes_when_set() -> None:
    empty = AgentProfile(version="2026-08-25")
    assert "capabilities" not in empty.to_dict()["ucp"]

    with_caps = AgentProfile(version="2026-08-25", capabilities={"com.example.thing": []})
    assert with_caps.to_dict()["ucp"]["capabilities"] == {"com.example.thing": []}


def test_build_profile_declares_the_capabilities_the_pipeline_uses() -> None:
    """Live-observed requirement (Phase 21): a business only activates a
    capability for us if our own profile declares that same name too —
    without this, tools/call answers "Tool not found" even for tools
    tools/list shows. Declaring exactly what search/cart/checkout/
    fulfillment need, nothing broader."""
    capabilities = build_profile().to_dict()["ucp"]["capabilities"]

    assert set(capabilities.keys()) == {
        "dev.ucp.shopping.catalog.search",
        "dev.ucp.shopping.catalog.lookup",
        "dev.ucp.shopping.cart",
        "dev.ucp.shopping.checkout",
        "dev.ucp.shopping.fulfillment",
    }
    for entries in capabilities.values():
        assert len(entries) == 1
        entry = entries[0]
        assert entry["version"] == UCP_VERSION
        assert entry["spec"].startswith("https://ucp.dev/")
        assert entry["schema"].startswith("https://ucp.dev/")


def test_profile_document_has_no_secret_looking_keys() -> None:
    document = build_profile().to_dict()
    serialized = json.dumps(document).lower()

    for forbidden in ("token", "secret", "password", "key", "card", "cvv"):
        assert forbidden not in serialized


def test_write_profile_creates_valid_json_file(tmp_path) -> None:
    output_path = tmp_path / "agent-profile.json"

    written = write_profile(output_path)

    assert written == output_path
    document = json.loads(output_path.read_text(encoding="utf-8"))
    assert document["ucp"]["version"] == UCP_VERSION


def test_write_profile_creates_parent_directories(tmp_path) -> None:
    output_path = tmp_path / "nested" / "docs" / "agent-profile.json"

    write_profile(output_path)

    assert output_path.exists()
