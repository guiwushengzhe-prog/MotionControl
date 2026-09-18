"""Native model loaders must get a path they can open.

Vosk's loader goes through the narrow-character Windows API, so a model under
D:\\游戏 or C:\\Users\\张三 fails with "does not contain model files" while the
folder sits there complete. The target users are on Chinese Windows and will
put the app in exactly those places, and the only symptom is one line of
English in a console window they were told not to close.

These cover the resolution logic. The end-to-end proof -- four real path
shapes, each loaded through Vosk itself -- is a manual script, because it needs
the 66 MB model and directory junctions; its results are recorded in the commit
that introduced this module.
"""

from __future__ import annotations

import sys

import pytest

from motioncontrol import ascii_model_path as amp


@pytest.fixture
def model(tmp_path):
    """A stand-in with the shape of a Vosk model directory."""
    source = tmp_path / "vosk-model-small-cn-0.22"
    (source / "am").mkdir(parents=True)
    (source / "conf").mkdir()
    (source / "README").write_text("model", encoding="utf-8")
    (source / "am" / "final.mdl").write_bytes(b"\x00" * 1024)
    (source / "conf" / "mfcc.conf").write_text("--sample-frequency=16000", encoding="utf-8")
    return source


def test_ascii_detection():
    assert amp.is_ascii_path("C:/MotionControl/models")
    assert not amp.is_ascii_path("D:/游戏/models")
    assert not amp.is_ascii_path("C:/Users/张三/桌面")


def test_ascii_path_is_used_directly(model):
    resolved, tier = amp.resolve_loadable_model_path(model)
    assert tier == "direct"
    assert resolved == model


def test_non_ascii_path_is_mirrored(model, tmp_path):
    cjk = tmp_path / "游戏" / "vosk-model-small-cn-0.22"
    cjk.parent.mkdir()
    import shutil

    shutil.copytree(model, cjk)

    cache = tmp_path / "cache"
    mirrored = amp.mirror_to_ascii(cjk, cache)

    assert mirrored is not None
    assert amp.is_ascii_path(mirrored)
    assert (mirrored / "am" / "final.mdl").read_bytes() == b"\x00" * 1024
    assert (mirrored / "conf" / "mfcc.conf").is_file()


def test_mirror_is_reused_on_the_second_call(model, tmp_path):
    cache = tmp_path / "cache"
    first = amp.mirror_to_ascii(model, cache)
    marker = first / "am" / "extra-marker"
    marker.write_text("kept", encoding="utf-8")

    second = amp.mirror_to_ascii(model, cache)

    assert second == first
    assert marker.is_file(), "an unchanged model must not be re-copied"


def test_changed_model_produces_a_new_mirror(model, tmp_path):
    cache = tmp_path / "cache"
    first = amp.mirror_to_ascii(model, cache)
    (model / "am" / "final.mdl").write_bytes(b"\x01" * 2048)
    second = amp.mirror_to_ascii(model, cache)
    assert second != first, "a replaced model must not keep loading the old mirror"
    assert (second / "am" / "final.mdl").read_bytes() == b"\x01" * 2048


def test_interrupted_copy_is_never_reused(model, tmp_path):
    """The sentinel is written last, so a partial mirror is not mistaken for one."""
    cache = tmp_path / "cache"
    mirrored = amp.mirror_to_ascii(model, cache)
    assert mirrored is not None
    (mirrored / amp._SENTINEL).unlink()

    again = amp.mirror_to_ascii(model, cache)

    assert again is not None
    assert (again / amp._SENTINEL).is_file()
    assert (again / "am" / "final.mdl").is_file()


def test_mirror_directory_name_is_ascii(model, tmp_path):
    cjk = tmp_path / "模型目录"
    import shutil

    shutil.copytree(model, cjk)
    mirrored = amp.mirror_to_ascii(cjk, tmp_path / "cache")
    assert mirrored is not None
    assert amp.is_ascii_path(mirrored), mirrored


def test_cache_root_is_itself_ascii():
    """The cache is pointless if it lands under C:\\Users\\张三 as well."""
    root = amp.ascii_cache_root()
    if root is not None:
        assert amp.is_ascii_path(root), root


@pytest.mark.skipif(sys.platform != "win32", reason="Windows short paths")
def test_short_path_returns_none_when_unchanged(tmp_path):
    """8.3 generation off means Windows hands back the long name, not an error.

    Treating "it returned something" as success would silently pass the
    original non-ASCII path straight through to the loader.
    """
    result = amp.windows_short_path(tmp_path)
    assert result is None or str(result) != str(tmp_path)


def test_unavailable_tier_returns_the_original(model, tmp_path, monkeypatch):
    """With nowhere to mirror, report the loader's real error, not a made-up one."""
    cjk = tmp_path / "游戏" / "model"
    cjk.parent.mkdir()
    import shutil

    shutil.copytree(model, cjk)
    monkeypatch.setattr(amp, "ascii_cache_root", lambda: None)
    monkeypatch.setattr(amp, "windows_short_path", lambda path: None)

    resolved, tier = amp.resolve_loadable_model_path(cjk)

    assert tier == "unavailable"
    assert resolved == cjk
