"""Where packs come from and where they go. An installed vent must find its
bundled packs without a source checkout, and must never write into the
installed package: user packs live under VENT_HOME and shadow bundled ones."""

from dataclasses import replace
from pathlib import Path

import vent
from vent import paths
from vent.packs import load_search_path, save

from .test_packs import sample_pack


def test_vent_home_moves_every_written_file(monkeypatch, tmp_path):
    monkeypatch.setenv("VENT_HOME", str(tmp_path / "home"))
    assert paths.user_packs_dir() == tmp_path / "home" / "packs"
    assert paths.traces_dir() == tmp_path / "home" / "traces"
    assert paths.pack_search_path()[0] == tmp_path / "home" / "packs"


def test_default_home_is_a_dotdir(monkeypatch):
    monkeypatch.delenv("VENT_HOME", raising=False)
    assert paths.home() == Path.home() / ".ventriloquist"


def test_bundled_packs_live_inside_the_package():
    """Inside the package directory is what makes a wheel carry them."""
    package_dir = Path(vent.__file__).resolve().parent
    assert paths.BUNDLED_PACKS_DIR.parent == package_dir
    assert paths.pack_search_path()[-1] == paths.BUNDLED_PACKS_DIR
    assert sorted(paths.BUNDLED_PACKS_DIR.glob("*/pack.json"))


def test_user_pack_shadows_bundled_pack_for_the_same_app(tmp_path):
    bundled = sample_pack()
    save(bundled, tmp_path / "bundled" / bundled.bundle_id / "pack.json")
    other = sample_pack()
    other.bundle_id, other.app_name = "com.example.Other", "Other"
    save(other, tmp_path / "bundled" / other.bundle_id / "pack.json")

    mine = sample_pack()
    mine.tools.append(replace(mine.tools[0], name="mine_only"))
    save(mine, tmp_path / "user" / mine.bundle_id / "pack.json")

    loaded = load_search_path([tmp_path / "user", tmp_path / "bundled"])
    by_id = {pack.bundle_id: pack for pack in loaded}
    assert len(loaded) == 2
    assert [tool.name for tool in by_id["com.apple.TextEdit"].tools] == ["write_document", "mine_only"]
    assert by_id["com.example.Other"].app_name == "Other"


def test_same_directory_duplicates_are_kept_for_the_collision_check(tmp_path):
    """Shadowing works across directories only. Two packs for one app in a
    single directory is a mistake the server must report, not hide."""
    first, second = sample_pack(), sample_pack()
    save(first, tmp_path / "user" / "a" / "pack.json")
    save(second, tmp_path / "user" / "b" / "pack.json")
    assert len(load_search_path([tmp_path / "user"])) == 2


def test_missing_user_directory_contributes_nothing(tmp_path):
    pack = sample_pack()
    save(pack, tmp_path / "bundled" / pack.bundle_id / "pack.json")
    loaded = load_search_path([tmp_path / "does-not-exist", tmp_path / "bundled"])
    assert [p.bundle_id for p in loaded] == [pack.bundle_id]
