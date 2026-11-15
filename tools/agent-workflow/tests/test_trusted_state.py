"""Tests of trusted_state.py (T-022 fix round 4, R3F1): final path spellings (property tests, every platform), the
Windows handle checks, and the POSIX no-follow walk (run on Linux CI; skipped, so not_run, on Windows)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trusted_state as ts

WIN = pytest.mark.skipif(not ts.WINDOWS, reason="Windows handle checks")
POSIX = pytest.mark.skipif(ts.WINDOWS, reason="POSIX dir_fd and O_NOFOLLOW walk (Linux CI)")

# --- final path spellings ---------------------------------------------------------------------------------------

PART = st.text(alphabet="abcXYZ09_-~. ", min_size=1, max_size=6).filter(lambda s: s.strip(". ") == s and s)
PARTS = st.lists(PART, min_size=1, max_size=4)


def spell(drive_or_unc: tuple[str, ...], parts: list[str], long: bool) -> str:
    if drive_or_unc[0] == "unc":
        head = ("\\\\?\\UNC\\" if long else "\\\\") + "\\".join(drive_or_unc[1:])
    else:
        head = ("\\\\?\\" if long else "") + drive_or_unc[1]
    return head + "\\" + "\\".join(parts)


VOLUME = st.one_of(st.just(("drive", "D:")), st.tuples(st.just("unc"), PART, PART))


@given(VOLUME, PARTS, PARTS, st.booleans(), st.booleans())
def test_inside_holds_for_every_prefix_spelling(volume: Any, root: list[str], rest: list[str], a: bool,
                                                b: bool) -> None:  # fmt: skip
    """Property: the `\\\\?\\` and `\\\\?\\UNC\\` spellings of one path compare equal; a path below the root is
    inside, and its last component is a child of its parent."""
    root_text, child_text = spell(volume, root, a), spell(volume, root + rest, b)
    assert ts.is_inside(child_text, root_text)
    assert ts.is_child(spell(volume, root + rest, b), spell(volume, root + rest[:-1], not b))


@given(VOLUME, PARTS, PART)
def test_a_sibling_or_a_prefix_of_a_name_is_not_inside(volume: Any, root: list[str], extra: str) -> None:
    """Property: `root2` is not inside `root`, and a parent is not inside its child."""
    root_text = spell(volume, root, True)
    sibling = spell(volume, root[:-1] + [root[-1] + extra], True)
    assert not ts.is_inside(sibling, root_text)
    assert not ts.is_inside(spell(volume, root[:-1] or ["x"], True), spell(volume, root + [extra], True))


@given(PARTS.filter(lambda p: any(c.isalpha() for c in "".join(p))))
def test_a_case_or_short_name_difference_refuses(parts: list[str]) -> None:
    """Property, conservative side: both sides come from the same API, so any other spelling of a component (case,
    an 8.3 short name) is not inside."""
    root = spell(("drive", "D:"), parts, True)
    other = spell(("drive", "D:"), [p.swapcase() for p in parts], True)
    if other != root:
        assert not ts.is_inside(other + "\\f", root)
    assert not ts.is_inside(spell(("drive", "D:"), ["PROGRA~1", "f"], True), spell(("drive", "D:"), ["Program Files"], True))


def test_volume_and_unc_spellings_are_kept_apart() -> None:
    assert not ts.is_inside("\\\\?\\UNC\\srv\\share\\x", "\\\\?\\srv\\share")
    assert not ts.is_inside("\\\\?\\E:\\p\\x", "\\\\?\\D:\\p")
    assert ts.final_parts("\\\\?\\UNC\\srv\\share\\x") == ts.final_parts("\\\\srv\\share\\x")


# --- folders and files (real file system, tiny fixtures under tmp_path) --------------------------------------------


def junction(target: Path, link: Path) -> None:
    if ts.WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def unlink_link(link: Path) -> None:
    link.unlink() if link.is_symlink() else os.rmdir(link)  # remove the link as a link


def test_a_normal_inside_file_is_read_written_and_created(tmp_path: Path) -> None:
    """Positive control: a plain folder chain works for every mode."""
    root = tmp_path.resolve()
    with ts.Folder(root, root / "a" / "b", create=True) as folder:
        folder.write_new("new.json", b"[1]")
        assert folder.read_bytes("new.json") == b"[1]" and folder.read_bytes("absent") is None
        with folder.open_file("new.json", "u") as handle:
            ts.rewrite(handle, b"[2]")
        with pytest.raises(FileExistsError):
            folder.write_new("new.json", b"again")
    assert (root / "a" / "b" / "new.json").read_bytes() == b"[2]"


def test_a_missing_folder_without_create_is_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ts.Folder(tmp_path, tmp_path.resolve() / "absent")


def test_a_path_outside_the_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    with pytest.raises(ts.Escape, match="not under the project root"):
        ts.Folder(tmp_path / "root", tmp_path.resolve() / "other")


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_a_linked_component_is_refused(tmp_path: Path, where: str) -> None:
    root = (tmp_path / "root").resolve()
    target = root / "real" if where == "inside" else tmp_path.resolve() / "outside"
    root.mkdir()
    target.mkdir(parents=True, exist_ok=True)
    junction(target, root / "state")
    try:
        with pytest.raises(ts.Escape, match="is a link"):
            ts.Folder(root, root / "state", create=True)
        with pytest.raises(ts.Escape, match="is a link"):
            ts.Folder(root, root / "state" / "deeper", create=True)
    finally:
        unlink_link(root / "state")
    assert list(target.iterdir()) == []  # nothing was created through the link


@WIN
def test_windows_a_held_folder_cannot_be_renamed_or_swapped(tmp_path: Path) -> None:
    """The reason for the share-read handle: while held, a folder of the chain cannot be renamed away."""
    root = tmp_path.resolve()
    with ts.Folder(root, root / "a" / "b", create=True):
        with pytest.raises(PermissionError):
            os.rename(root / "a", root / "moved")
        with pytest.raises(PermissionError):  # the leaf: no held handle below it, only its own share mode
            os.rename(root / "a" / "b", root / "a" / "c")
    os.rename(root / "a", root / "moved")  # released: the same rename now works


@WIN
def test_windows_a_folder_handle_outside_its_parent_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The final path check on each opened folder (labelled double: the API answers a path outside the parent)."""
    root = tmp_path.resolve()
    (root / "a").mkdir()
    real = ts._final_path
    monkeypatch.setattr(ts, "_final_path", lambda fd: real(fd).replace("\\a", "\\elsewhere\\a"))
    with pytest.raises(ts.Escape, match="outside its folder"):
        ts.Folder(root, root / "a")


@WIN
def test_windows_a_file_link_is_refused_by_its_reparse_attribute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file symbolic link opened with OPEN_REPARSE_POINT is the link itself, whose final path is inside; only its
    reparse attribute tells. This host cannot create file links without privilege, so a labelled double reports
    the attribute for the opened file."""
    import stat as st_mod
    from types import SimpleNamespace

    root = tmp_path.resolve()
    (root / "a").mkdir()
    (root / "a" / "f").write_bytes(b"x")
    real = ts.os.fstat

    def fstat(fd: int) -> Any:
        result = real(fd)
        if st_mod.S_ISREG(result.st_mode) and ts._final_path(fd).endswith("\\a\\f"):  # only the fixture file
            return SimpleNamespace(st_file_attributes=ts.REPARSE, st_mode=result.st_mode, st_dev=result.st_dev,
                                   st_ino=result.st_ino)  # fmt: skip
        return result

    monkeypatch.setattr(ts.os, "fstat", fstat)
    with ts.Folder(root, root / "a") as folder, pytest.raises(ts.Escape, match="is a link"):
        folder.read_bytes("f")


@WIN
def test_windows_a_handle_outside_its_folder_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The final path check on the opened handle (labelled double: the API answers an outside path)."""
    root = tmp_path.resolve()
    (root / "a").mkdir()
    (root / "a" / "f").write_bytes(b"x")
    real = ts._final_path
    monkeypatch.setattr(ts, "_final_path", lambda fd: real(fd).replace("\\a\\f", "\\elsewhere\\f"))
    with ts.Folder(root, root / "a") as folder, pytest.raises(ts.Escape, match="outside its folder"):
        folder.read_bytes("f")


@WIN
def test_windows_owned_folder_is_deleted_only_when_it_is_the_same_object(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    with ts.Folder(root, root, create=True) as top:
        owned = top.owned("scan")
        identity = owned.identity()
        (owned.path / "report.json").write_bytes(b"[]")
        owned.close()
        os.unlink(root / "scan" / "report.json")
        assert top._delete_dir("scan", (identity[0], identity[1] + 1)) != [] and (root / "scan").is_dir()
        assert top._delete_dir("scan", identity) == []
    assert not (root / "scan").exists()


@POSIX
def test_posix_a_symbolic_file_link_is_refused(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "a").mkdir()
    (tmp_path / "outside.json").write_bytes(b"[9]")
    (root / "a" / "f").symlink_to(tmp_path / "outside.json")
    with ts.Folder(root, root / "a") as folder, pytest.raises(ts.Escape, match="is a link"):
        folder.read_bytes("f")


@POSIX
def test_posix_an_owned_folder_moved_and_replaced_is_reported_not_followed(tmp_path: Path) -> None:
    """POSIX cannot hold a folder in place, so the owned folder is moved and a link put at its name. The held
    descriptor still reads the real report, cleanup empties the real folder through it, and the moved folder is
    reported as left instead of removing whatever now has the name."""
    root, outside = tmp_path.resolve() / "root", tmp_path.resolve() / "outside"
    root.mkdir()
    outside.mkdir()
    with ts.Folder(root, root / "raw", create=True) as raw:
        owned = raw.owned("scan")
        (owned.path / "report.json").write_bytes(b"[1]")
        os.rename(raw.path / "scan", raw.path / "moved")
        (raw.path / "scan").symlink_to(outside, target_is_directory=True)
        assert owned.read_bytes("report.json") == b"[1]"
        left = raw.remove_owned(owned)
    assert left and "moved or replaced" in left[0]
    assert list((root / "raw" / "moved").iterdir()) == [] and list(outside.iterdir()) == []
    (root / "raw" / "scan").unlink()
