"""Built shaders are kept per game (the driver's one shared cache made big games rebuild them on every start)."""

from test_release_fixes import core, install  # noqa: F401 — the shared AppCore fixture


async def test_each_game_keeps_its_own_built_shaders(core, tmp_path):  # noqa: F811
    e, _root = install(core, tmp_path, {}, exe="Game/Game.exe")
    lib = core.library
    env = lib.shader_cache_env(e.id)
    cache = lib.shader_cache_dir(e.id)
    assert env["__GL_SHADER_DISK_CACHE_PATH"] == str(cache / "nvidia")
    assert env["__GL_SHADER_DISK_CACHE_SKIP_CLEANUP"] == "1" and int(env["__GL_SHADER_DISK_CACHE_SIZE"]) >= 10 << 30
    assert env["MESA_SHADER_CACHE_DIR"] == str(cache / "mesa")
    plan = lib.launch_plan(e.id)
    if lib.protons():                                         # (a Proton launch on this computer)
        assert plan.env["__GL_SHADER_DISK_CACHE_PATH"] == str(cache / "nvidia")
    # the game's own choice wins (a launch option / fix that sets it)
    cfg = lib.get(e.id).compat
    lib.update(e.id, launch_args="__GL_SHADER_DISK_CACHE_SIZE=1 %command%", compat=cfg)
    plan = lib.launch_plan(e.id)
    assert "__GL_SHADER_DISK_CACHE_SIZE=1" in (plan.shell_script or " ".join(plan.argv))
    # Storage counts it, Uninstall removes it, an orphan is offered for cleanup
    (cache / "nvidia" / "blob").write_bytes(b"x" * 4096)
    rep = await core.storage_report()
    g = next(g for g in rep["games"] if g["entry_id"] == e.id)
    assert g["shaders"] >= 4096 and g["total"] >= g["shaders"]
    items = {i["key"]: i for i in core._uninstall_items(e.id)}
    assert items["shadercache"]["default"] and items["shadercache"]["paths"][0]["ok"]
    orphan = core.paths.data / "shadercache" / "game-999"
    (orphan / "nvidia").mkdir(parents=True)
    rep = await core.storage_report()
    assert any(o["path"] == str(orphan) for o in rep["old_prefixes"])
    res = await core.delete_old_prefixes([str(orphan)])
    assert res["deleted"] == [str(orphan)] and not orphan.exists()
    res = await core.delete_old_prefixes([str(cache)])        # a cache in use is never deleted this way
    assert not res["deleted"] and cache.exists()
