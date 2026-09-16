"""What may be synced to the cloud, as a whitelist.

A blacklist would default to allowing, and the next config file someone adds --
a camera backend cache, a DLL path, a model location -- would travel to another
machine before anyone noticed. So the bundle builder takes its file list *only*
from here. There is deliberately no code path anywhere that walks the user data
directory and syncs what it finds.

The three categories:

**User configuration** travels. Per-game mappings, motions and voice bindings
describe intent, not hardware, so they mean the same thing on any machine.

**Device configuration** never travels. Camera backend probe results, absolute
model paths, the ViGEm DLL location: every one of these is a fact about one
installation, and on another machine they are simply wrong.

**Spatial calibration** never travels, and this is the one that is easy to get
wrong because it looks like user configuration. ``scene_layout.json`` is a
homography onto one frame captured from one camera in one physical position,
and ``scene_reference.jpg`` is that frame. scene_layout.py already refuses to
load the layout when the reference photo is missing. Copied to another machine
the file parses, the app starts, and the six zones sit somewhere silently
wrong -- which is worse than an error.

``head_profile.json`` sits between the categories and is therefore excluded for
now. Its tuning (sensitivity, deadzone, chosen control mode) is a preference
and would travel fine; its calibration centre, camera jitter estimate and
personal PnP geometry are tied to one person in front of one camera. Splitting
the file is the prerequisite for syncing half of it, and that is not done yet.
"""

from __future__ import annotations

USER_CONFIGURATION = "user_configuration"
DEVICE_CONFIGURATION = "device_configuration"
SPATIAL_CALIBRATION = "spatial_calibration"

# The only keys a cloud bundle may ever contain.  Keys are the same ones
# motioncontrol_shared.user_layout uses, so a file is named once.
CLOUD_SYNC_ALLOWLIST = {
    "profile_selection",
    "motion_mappings",
    "voice_mappings",
}

# Every user-data key, classified.  Anything absent from this map is a
# programming error rather than an implicit "do not sync": the test suite
# asserts the two stay in step, so a new file has to be sorted deliberately.
CLASSIFICATION = {
    "profile_selection": USER_CONFIGURATION,
    "motion_mappings": USER_CONFIGURATION,
    "voice_mappings": USER_CONFIGURATION,
    # Registered, deliberately not synced -- see the module docstring.
    "head_profile": DEVICE_CONFIGURATION,
    "camera_backend": DEVICE_CONFIGURATION,
    "paired_devices": DEVICE_CONFIGURATION,
    "require_paired_devices": DEVICE_CONFIGURATION,
    # Someone self-hosting points their own install at their own server;
    # carrying that address to another machine would silently redirect it.
    "cloud_endpoint": DEVICE_CONFIGURATION,
    "scene_layout": SPATIAL_CALIBRATION,
    "scene_reference": SPATIAL_CALIBRATION,
}


def may_sync(key: str) -> bool:
    return key in CLOUD_SYNC_ALLOWLIST


def syncable_keys() -> tuple[str, ...]:
    """The bundle builder's only source of truth for what to include."""
    return tuple(sorted(CLOUD_SYNC_ALLOWLIST))


def classify(key: str) -> str:
    try:
        return CLASSIFICATION[key]
    except KeyError:
        raise KeyError(
            f"{key!r} is not classified; add it to CLASSIFICATION in "
            "motioncontrol_shared/sync_allowlist.py as user configuration, "
            "device configuration or spatial calibration"
        ) from None
