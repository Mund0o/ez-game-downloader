# Test report

Date: 2026-10-09 · Environment: CachyOS Linux (kernel 7.2.9), x86_64, Python 3.12.14,
PySide6-Essentials 6.12.0, libtorrent 2.1.1, httpx 0.28.1, UnRAR 7.30 beta 1.
Qt ran with `QT_QPA_PLATFORM=offscreen`.

## Final run

```
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -m "live or not live or proton" -rs
248 passed, 1 skipped in 63.10s
SKIPPED tests/test_archives.py: 'rar' (to create test archives) is not installed
```

The default offline suite (`pytest`, live tests deselected) was also run four times in a
row after the last flakiness fix (170 passed, 1 skipped each time); after adding the source/password labelling it is 172 passed; after the Steam-style library / Proton work the offline suite is 191 passed; with the installer / umu-launcher / sign-in-only-torrent work it is **201 passed; with closed-guide filtering it is 207 passed; with Steam-identical launch options (`/bin/sh -c` with `%command%` substitution) it is 223 passed; with the Fixes screen and compatibility advisor it is 230 passed; with the cracked-games.org source and the browser-download import it is **244 passed, 1 skipped** (offline) and **248 passed, 1 skipped** including the live and Proton tests.
Static check: `ruff check --select F,E9` → all checks passed.

| File | Tests | Covers |
|---|---:|---|
| test_urls.py | 35 | URL/SSRF validation (private, loopback, link-local, CGNAT, IPv4-mapped IPv6, numeric hosts, credentials, schemes), DNS-level blocking of a real local server, redirect-to-private, HTTPS→HTTP downgrade, redirect limit, cross-host Referer stripping, response size cap, bundled TLS roots |
| test_archives.py | 24 (1 skipped) | zip/tar/7z extraction, password 7z, traversal variants rejected before writing, symlinks skipped, truncated & CRC-corrupt archives, destination untouched on failure, cancellation, disk-space check, nested archives reported, multi-volume detection, Fix-Repair selection, UnRAR listing parser, missing RAR tool, corrupt RAR via real `unrar` |
| test_http_engine.py | 18 | progress, resume after mid-stream drops (Range), server without Range support, changed file (If-Range), 5xx retries, bounded retries, 404/410/403, HTML interstitial rejection, SHA-256 verified/mismatch, disk full, no overwrite, redirects, bandwidth limit, cancellation keeps `.part`, filename/header helpers |
| test_proton_launch.py | 20 | Steam launch-option syntax (`%command%`, env, wrappers, rejection of shell operators), Steam Linux Runtime resolved from toolmanifest + appmanifest, exact Steam command chain, `UMU_ID` (never guessed from the prefix path), SteamAppId opt-in, Proton options, DLL-override validation and detection (OnlineFix marker), runtime fallback, v1→v2 migration, playtime, Stop kills child processes, Steam App ID lookup (catalog then store search, exact matches only), **real run of a Windows program through the installed Proton-CachyOS inside Steam Linux Runtime 4.0** (`-m proton`) |
| test_sources_onlinefix.py | 16 | real-page fixtures (RU and EN posts), cards, carousels (Picks), search & empty search, details, hosters JSON, torrent index, parked domain, changed layouts, missing fields, RU/EN dates, end-to-end adapter with cache, non-torrent payload, foreign URL refusal, cp1251 query encoding |
| test_pipeline_library.py | 14 | auto-extraction into install dir, archive deletion only when enabled, corrupt archive state, folder downloads, interrupted-extraction recovery, executable ranking, trust/confirmation, native launch, duplicate detection, deletion safety, Proton/Wine plans, binary VDF + Steam shortcut, Proton discovery, text VDF |
| test_torrent.py | 11 | invalid/hostile torrents, full download from a local swarm, selective files, pause/resume, **restart recovery from resume data**, recheck detects corruption, magnet metadata wait, disk error |
| test_database.py | 9 | migrations, schema-too-new, corrupt file quarantine, incremental migration with backup, failed migration rollback, transactions, backup/validate/restore |
| test_sources_steamrip.py | 9 | REST API listing/details fixtures, API error shapes, malformed posts, genre→category ids, title-only search, foreign URL refusal, host classification, Pixeldrain resolve / captcha fallback / 404 |
| test_catalog.py | 7 | title cleaning, dedup keys, genre mapping, duplicate grouping (title + Steam App ID), filters and sorting, relevance, unknown vs empty methods |
| test_manager.py | 7 | concurrency limit, pause/resume/cancel/retry/remove, failure messages, duplicates & invalid input, **restart mid-download resumes via Range**, paused state survives restart, priority |
| test_registry_cache.py | 6 | conditional requests (ETag/304), stale cache offline, 5xx doesn't poison cache, one source down, layout-changed status + offline catalog, featured cache |
| test_ui.py | 9 | navigation, Home rows, Picks sections, **debounced search (1 request for 4 keystrokes) with GUI timers still firing during a 1.5 s network delay**, details dialog → download options → queued torrent, live settings, library Set up → Properties (launch options + live command preview) → Play → Stop, source names + 🔒 password on cards/filter chips |
| test_sources_crackedgames.py | 11 | real-page fixtures (listing with an 18+ card, search, empty search, Hytale game page with JSON-LD, password, CRC32/SHA-256), changed layout, foreign/disallowed URLs refused, adapter requests only robots-allowed GET URLs (never `/download`, `/search`, `/api`, `/livewire`), 403 explained, browser-only resolve, **import of a browser download: SHA-256 verified → library entry → extracted with the password**, mismatch refused unless confirmed, sources without checksums, 18+ hiding |
| test_settings.py | 6 | persistence, clamping/validation, listeners, `config.sample.toml` import, invalid stored values |
| test_core_integration.py | 5 | **catalog → details → resolve → torrent from local swarm → auto-extract → library with exe hint**, browser-only methods, cross-source duplicates & live settings, core restart resumes a partial HTTP download, archive-password fallbacks (download → game page → source) |
| test_live_sites.py | 3 | live online-fix.me, steamrip.com and cracked-games.org parsing, search, details, torrent metadata resolution (no game content transferred) |

## Manual / smoke verification performed

* Launched the app from source and the **PyInstaller Linux bundle**
  (`packaging/build_linux.sh`, offscreen) against the live sites: both sources
  reported `ok`, 67 games cached, covers loaded, torrent engine started.
* Screenshots of every page and of the game-details dialog (live OnlineFix and
  SteamRIP data) were reviewed; fixed: cover clipping (odd-even fill), details
  dialog overflow, missing search icon, stale Picks headings, wide settings fields.

## Installer verification

* `packaging/install.sh --no-deps` run against a throw-away `$HOME`: app copied to `~/.local/opt`, `ezgd` symlink, `.desktop` entry with absolute `Exec`, icon; `uninstall.sh` removed every file again.
* `ezgd --setup-system` (frozen build) on CachyOS listed the tools and planned `pacman -S --needed --noconfirm umu-launcher gamemode lib32-gamemode mangohud lib32-mangohud gamescope` (Steam/UnRAR/Winetricks correctly skipped). The actual package installation needs the user's sudo password and was not run by the developer.

## Bugs found by the tests and fixed

`™` turned into "TM" before stripping (duplicates missed) · zip entries without
Unix type bits were skipped as "special" files (nothing extracted) · progress never
reported for short downloads · BEP 47 pad files shown as torrent files · shutdown
race submitting work to a closed loop · Settings page briefly showing stale values ·
library/peer test flakiness (single `connect_peer` attempt) · sign-in-only OnlineFix torrents (HTTP 401) surfaced as a raw error instead of a browser fallback · a Hosters page with no mirrors yet was reported as a layout change · protonfixes could read the app's prefix number as a Steam App ID · the Library’s duplicate check passed an id where an entry was expected · OnlineFix games with closed guides (“Руководство закрыто”) were listed and downloadable · dateless cracked-games.org listings all sorted below every dated game · a source added in a new version was filtered out for users whose saved settings predate it · releases that start each mode through a .cmd which swaps steam_api.dll first (Call of Duty: Black Ops II's mp.cmd/zm.cmd/sp.cmd) were launched by their exe directly, so the game loaded the wrong DLL and tried to restart through Steam — the advisor now offers the script, verified with a real Proton run.

## Not tested

* **Windows**: no Windows machine was available. Windows-specific code (paths,
  `os.startfile`, process flags, registry lookup for Steam, WinRAR/7-Zip discovery,
  PyInstaller build script) has not been executed.
* **Real RAR creation**: `rar` is not installed, so the password-protected RAR
  extraction test is skipped (UnRAR listing parsing and corrupt-RAR handling were tested).
* **Real swarm downloads from the internet** and real Proton game launches were not
  performed (no copyrighted game content was downloaded); torrent behaviour was
  verified with a local two-session swarm, launching with a native script.
* macOS is not a target platform.

## Version 0.2.0 (2026-10-13): couch mode, wishlist, game updates, save backups, storage, app updates

Offline suite: **481 passed, 1 skipped** (`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/`).

| File | Tests | Covers |
|---|---:|---|
| test_features_v2.py | 25 | save backups (only saves — caches/logs/temp/DXVK/symlinks left out; unchanged saves not zipped again; pruning; umu prefix layout; restore refuses paths outside the prefix; restore with an undo backup; automatic backup after a session; backups found again after a reinstall; uninstall backs up first) · versions from archive names and pages · update check → notification once → *Update* → the next import goes over the same Library entry and folder, saves backed up first · sites that ask not to be crawled are only checked on request · first check of a page-only version is a baseline · interrupted extraction finished on the next start · wishlist (Steam search/IDs, edition-suffix matching, never across different App IDs, notification once, closed guides, unreleased games and shy sites not searched in the background, toggle from a game page, “in your library”) · storage report and deleting unused prefixes (refuses anything else) · learning from fix outcomes · app updates (GitHub API, SHA-256 verified download and mismatch, no checksum → refused, the new build must report its version, real folder swap + relaunch by the script, previous version removed) · controller mapping (buttons, d-pad, stick hysteresis, Steam virtual pad duplicates, detection from /sys that never opens keyboards) · stopping a game completely (ignores SIGTERM, a helper that left its process group is force-stopped too, an unrelated process survives) |
| test_couch_ui.py | 9 | couch mode full screen (main window hidden, messages inside, every tab drawn at 4 sizes without errors), controller navigation to the ✕, the ✕ with a game started from couch mode (asks, Cancel preselected, Alt+F4 = ✕, the game process is really gone, the window comes back), a game that will not close (*Try again* / *Leave anyway*), cancel at “Play?”, leaving while a game is still starting (closed when it appears), input ignored while playing, Wishlist page (Steam search → add → remove), Library update card / saves card / Storage dialog, Settings → App updates |

Also checked by hand: screenshots of couch mode (Home, Library, Downloads, Details, “Quit …?”, the focused ✕), the
Wishlist (live: Hollow Knight: Silksong found on OnlineFix, FitGirl and AnkerGames), Library, Storage and Settings,
from a copy of the real library (Steam artwork via Steam's store API); the packaged build (PyInstaller) starts couch
mode and the Wishlist; the updater unpacks and verifies the real release tarball (relative symlinks kept).

## Version 0.3.0 (2026-10-14): couch mode does everything, controllers, speed

Offline suite: **496 passed, 1 skipped**.

* Couch mode: Store (search with an on-screen keyboard, filters, pages that keep loading), game pages and downloads,
  Wishlist, Settings, a Manage menu per game, Downloads (pause / resume / cancel), all by controller; the in-app
  browser driven by the controller (pointer on the left stick, real clicks, typing, menu).
* Controllers: presses of every controller were dropped before (the notifier's argument replaced the device path) —
  now tested through a real device node; PlayStation / Xbox / Steam button prompts; triggers; guide button.
* Couch mode stays on top while a game starts (KWin scripting on KDE Wayland), fills the screen it is on, and sends
  frames at a steady rate (VRR flicker).
* Speed: covers decoded and cards drawn once off the GUI thread, site pages parsed in a helper process, zip and 7z
  extracted with 7-Zip (40× faster for encrypted zips, the app stays responsive), closing the app stops a running
  extraction (it resumes on the next start).
