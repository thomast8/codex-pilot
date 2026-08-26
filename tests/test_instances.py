"""Instance discovery: one Codex Desktop install == one CODEX_HOME.

Doppel clones ChatGPT.app and stamps `LSEnvironment.CODEX_HOME` into each
clone's Info.plist, so the installed bundles are the authoritative record of
which instances exist and where their state lives.
"""

from __future__ import annotations

import plistlib
import socket
import tempfile
from pathlib import Path

from codex_pilot.instances import Instance, discover_instances, slug_for


def make_app(root: Path, name: str, codex_home: str | None) -> Path:
    app = root / f"{name}.app" / "Contents"
    app.mkdir(parents=True)
    info: dict[str, object] = {"CFBundleName": name}
    if codex_home is not None:
        info["LSEnvironment"] = {"CODEX_HOME": codex_home}
    with (app / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh)
    return app.parent


# -- slugs --------------------------------------------------------------------


def test_slug_of_the_stock_app_is_default():
    assert slug_for("ChatGPT", is_default=True) == "default"


def test_slug_strips_the_product_prefix():
    assert slug_for("ChatGPT Personal", is_default=False) == "personal"


def test_slug_is_kebab_cased():
    assert slug_for("ChatGPT Work Profile", is_default=False) == "work-profile"


def test_slug_falls_back_to_the_whole_name_when_not_prefixed():
    assert slug_for("Codex Beta", is_default=False) == "codex-beta"


# -- discovery ----------------------------------------------------------------


def test_default_instance_is_always_present(tmp_path):
    home = tmp_path / "codexhome"
    home.mkdir()
    found = discover_instances(search_dirs=[tmp_path / "empty"], default_home=home)
    assert [i.slug for i in found] == ["default"]
    assert found[0].codex_home == home


def test_stamped_clone_is_discovered(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    make_app(apps, "ChatGPT Personal", str(tmp_path / "secondary"))
    found = discover_instances(search_dirs=[apps], default_home=tmp_path / "primary")
    assert {i.slug for i in found} == {"default", "personal"}
    personal = next(i for i in found if i.slug == "personal")
    assert personal.codex_home == tmp_path / "secondary"


def test_unstamped_app_maps_to_the_default_home_and_does_not_duplicate(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    make_app(apps, "ChatGPT", None)
    found = discover_instances(search_dirs=[apps], default_home=tmp_path / "primary")
    assert [i.slug for i in found] == ["default"]


def test_two_apps_sharing_a_codex_home_collapse_to_one_instance(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    make_app(apps, "ChatGPT A", str(tmp_path / "shared"))
    make_app(apps, "ChatGPT B", str(tmp_path / "shared"))
    found = discover_instances(search_dirs=[apps], default_home=tmp_path / "primary")
    homes = [i.codex_home for i in found]
    assert len(homes) == len(set(homes))


def test_missing_search_dir_is_not_an_error(tmp_path):
    found = discover_instances(
        search_dirs=[tmp_path / "nope", tmp_path / "also-nope"], default_home=tmp_path / "h"
    )
    assert [i.slug for i in found] == ["default"]


def test_malformed_plist_is_skipped(tmp_path):
    apps = tmp_path / "Applications"
    (apps / "ChatGPT Broken.app" / "Contents").mkdir(parents=True)
    (apps / "ChatGPT Broken.app" / "Contents" / "Info.plist").write_text("not a plist")
    found = discover_instances(search_dirs=[apps], default_home=tmp_path / "h")
    assert [i.slug for i in found] == ["default"]


def test_non_chatgpt_bundles_are_ignored(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    make_app(apps, "Safari", str(tmp_path / "nope"))
    found = discover_instances(search_dirs=[apps], default_home=tmp_path / "h")
    assert [i.slug for i in found] == ["default"]


def test_default_sorts_first(tmp_path):
    apps = tmp_path / "Applications"
    apps.mkdir()
    make_app(apps, "ChatGPT Alpha", str(tmp_path / "a"))
    found = discover_instances(search_dirs=[apps], default_home=tmp_path / "primary")
    assert found[0].slug == "default"


# -- socket candidates --------------------------------------------------------


def test_primary_socket_is_under_the_codex_home(tmp_path):
    inst = Instance(slug="personal", codex_home=tmp_path / "sec", app_path=None, is_default=False)
    assert inst.socket_candidates()[0] == tmp_path / "sec" / "ipc" / "ipc.sock"


def test_tmpdir_fallback_is_offered_only_to_the_default_instance(tmp_path):
    clone = Instance(slug="personal", codex_home=tmp_path / "s", app_path=None, is_default=False)
    default = Instance(slug="default", codex_home=tmp_path / "p", app_path=None, is_default=True)
    # The tmpdir socket is machine-global, so it cannot disambiguate clones.
    assert len(clone.socket_candidates()) == 1
    assert len(default.socket_candidates()) == 2


def test_socket_path_returns_the_first_existing_candidate():
    # Bound in a short directory: AF_UNIX paths cap at ~104 bytes on macOS and
    # pytest's tmp_path is already longer than that.
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        home = Path(raw)
        (home / "ipc").mkdir()
        sock_path = home / "ipc" / "ipc.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        try:
            inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
            assert inst.socket_path() == sock_path
        finally:
            server.close()


def test_a_plain_file_at_the_socket_path_is_not_mistaken_for_a_socket(tmp_path):
    home = tmp_path / "p"
    (home / "ipc").mkdir(parents=True)
    (home / "ipc" / "ipc.sock").touch()
    inst = Instance(slug="default", codex_home=home, app_path=None, is_default=True)
    assert inst.socket_path() is None


def test_socket_path_is_none_when_nothing_exists(tmp_path):
    inst = Instance(slug="personal", codex_home=tmp_path / "gone", app_path=None, is_default=False)
    assert inst.socket_path() is None
