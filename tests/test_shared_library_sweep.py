r"""Run the real profile library through the shared rules, on any platform.

This is the test the Linux CI job exists to run.  The static boundary scan in
test_shared_boundary.py can only see imports it recognises; importing the
package for real on Linux and pushing every shipped profile through it is what
actually proves the cloud can validate uploads the same way the desktop does.

The pinned digest is the cross-platform contract.  If someone drops
``ensure_ascii=False``, writes through Python's text layer so "\n" becomes
"\r\n" on Windows, or stops sorting keys, this literal stops matching --
which is the whole point of pinning it rather than recomputing it.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.catalog import load_catalog, load_profile
from motioncontrol_shared.motion_conflicts import validate_motion_bindings
from motioncontrol_shared.profile_schema import normalize_bindings

REPO = pathlib.Path(__file__).resolve().parent.parent
LIBRARY = REPO / "game_profiles"
FIXTURE = REPO / "tests" / "fixtures" / "canonical_selection.json"

# Byte-for-byte identical on Windows and Linux.  Do not "fix" this by pasting
# in whatever the current run prints; a mismatch means the canonical form
# changed, and that is a wire-contract change.
EXPECTED_SHA256 = "d8cf183cf462b89383b69e424268f8e3a3fd4858fa17e241d02ed75022778433"


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(LIBRARY)


def test_catalog_loads(catalog):
    assert catalog["count"] >= 199, catalog["count"]


def test_every_shipped_profile_validates(catalog):
    """Load and re-validate all of them; report every failure, not just the first."""
    failures = []
    for entry in catalog["games"]:
        try:
            profile = load_profile(LIBRARY, entry["id"], catalog)
            normalize_bindings(profile["bindings"])
            validate_motion_bindings(profile["bindings"])
        except Exception as exc:  # noqa: BLE001 - we want the whole list
            failures.append(f"{entry['id']}: {type(exc).__name__}: {exc}")
    assert not failures, f"{len(failures)}/{catalog['count']} profiles failed: " + "; ".join(failures[:10])


def test_canonical_digest_is_stable_across_platforms():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = canonicalize("profile_selection", raw)
    assert result.sha256 == EXPECTED_SHA256


def test_canonical_bytes_have_no_crlf_and_no_trailing_space():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload = canonicalize("profile_selection", raw).payload
    assert b"\r" not in payload
    assert payload.endswith(b"\n")
    assert not any(line.endswith((b" ", b"\t")) for line in payload.split(b"\n"))


def test_canonical_preserves_non_ascii_verbatim():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    text = canonicalize("profile_selection", raw).payload.decode("utf-8")
    assert "左手上区" in text, "ensure_ascii=False must keep Chinese labels readable"
    assert r"\u" not in text


def test_canonicalize_is_idempotent():
    """Feeding canonical output back in must not change the digest."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    once = canonicalize("profile_selection", raw)
    twice = canonicalize("profile_selection", json.loads(once.payload.decode("utf-8")))
    assert twice.sha256 == once.sha256


def test_canonicalize_rejects_unknown_document_type():
    with pytest.raises(ValueError, match="unknown document type"):
        canonicalize("not_a_real_type", {})


def test_canonicalize_rejects_unknown_schema():
    with pytest.raises(ValueError, match="unsupported selection schema"):
        canonicalize("profile_selection", {"schema": "motioncontrol.profile_selection.v99",
                                           "selected_id": "x", "overrides_by_profile": {}})


def test_canonicalize_rejects_invalid_binding():
    """The shared validator's own Chinese message must reach the caller."""
    with pytest.raises(ValueError, match="Xbox"):
        canonicalize("profile_selection", {
            "schema": "motioncontrol.profile_selection.v2",
            "selected_id": "generic-xbox",
            "overrides_by_profile": {"generic-xbox": {
                "zone.leftHandUpper": {"action": {"type": "gamepad", "target": "Z"}}}},
        })


def test_canonicalize_migrates_v1_selection():
    v1 = {"schema": "motioncontrol.profile_selection.v1", "selected_id": "generic-xbox",
          "overrides": {"zone.leftHandUpper": {"action": {"type": "gamepad", "target": "A"}}}}
    result = canonicalize("profile_selection", v1)
    assert result.data["schema"] == "motioncontrol.profile_selection.v2"
    assert "zone.leftHandUpper" in result.data["overrides_by_profile"]["generic-xbox"]
