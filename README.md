# EZ Game Downloader

A cross-platform (Linux / Windows) desktop app that combines game discovery from
**OnlineFix**, **SteamRIP** and **cracked-games.org**, an embedded **BitTorrent** engine, a resumable
**HTTP** download engine, safe **archive extraction**, and a local **game library**
with launch support (native, Proton, Wine, Steam shortcuts).

Built with Python 3.12, PySide6 (Qt 6), SQLite, httpx, Beautiful Soup and libtorrent.

> **Legal note.** The app only uses publicly accessible pages, public APIs and the
> download references a site exposes without login. It does not bypass captchas,
> logins, Cloudflare challenges, DRM or any other access control. Much of what these
> sites list is commercial software; you are responsible for respecting copyright
> and each site's terms in your jurisdiction.

---

## Contents
1. [Features](#features)
2. [Source integration notes (verified)](#source-integration-notes-verified)
3. [Install & run — Linux](#install--run--linux)
4. [Install & run — Windows](#install--run--windows)
5. [Building distributable packages](#building-distributable-packages)
6. [Configuration](#configuration)
7. [Using the app](#using-the-app)
8. [Security model](#security-model)
9. [Architecture](#architecture)
10. [Tests](#tests)
11. [Troubleshooting](#troubleshooting)
12. [Limitations and incomplete features](#limitations-and-incomplete-features)
13. [Still to confirm](#still-to-confirm)

---

## Features

| Area | What it does |
|---|---|
| **Home** | Quick search, active downloads, recently added (live, cached fallback), *Popular on OnlineFix*, recently downloaded, per-source status cards with limitations. |
| **All Games** | One search across enabled sources, search-as-you-type (400 ms debounce), filters (source, genre, platform, download method, library status), sorting (best match, recent, title, most sources), incremental loading, duplicate grouping (title normalisation + Steam App ID), per-source error banners with Retry / Report. |
| **OnlineFix Picks** | The two curated carousels that online-fix.me really shows (“Популярное”/Popular and “Обновления”/Recently updated), cached for 1 h, manual refresh. |
| **Game details** | Cover, metadata, description, source link, every download method labelled *Torrent · in-app*, *Direct · in-app* or *Browser only* with the reason; copy link; report a problem; switch between sources offering the same game. |
| **Downloads** | Queued / active / paused / completed / failed, live progress, speed, ETA, size, peers/seeds, upload speed; pause, resume, cancel, retry, remove (optionally deleting files), priority, change destination, torrent re-check, selective torrent files; concurrency & speed limits; persistent across restarts. |
| **Torrent engine** | libtorrent 2.1 inside the app: magnets and `.torrent` files, resume data, file selection, piece-hash verification, stall detection, disk-error reporting, DHT/LSD/UPnP toggles, rate limits. |
| **HTTP engine** | Streaming, Range/If-Range resume, bounded exponential-backoff retries, shared bandwidth limit, `.part` files + atomic rename, disk-space checks, SHA-256 verification when a trusted checksum exists, rejection of HTML interstitials. |
| **Extraction** | zip, 7z (incl. password), rar (via UnRAR/7-Zip), tar.*, gz/bz2/xz; staging folder, traversal/symlink protection, disk-space check, corruption detection, cancellation, nested archives reported but never auto-extracted. |
| **Library** | Steam-style: every downloaded/imported game with cover, big **Play/Stop**, running state, playtime, launches, setup checklist, Properties dialog; downloaded → extracted → installed as separate states; ranked executable candidates (uses the game page's “run X.exe” hint); confirmation before first launch; duplicate warnings; safe removal; add as non-Steam game. |
| **Proton (Linux)** | Runs Windows games the way Steam does — your Proton build inside the Steam Linux Runtime it requires, one prefix per game; protonfixes keyed by the real Steam App ID (auto-detected); Steam's Proton toggles; DLL-override detection; winecfg/winetricks/reset prefix; Steam launch-option syntax (`VAR=1 gamemoderun %command% -arg`). |
| **Couch mode** | Full-screen, controller-friendly library in the style of SteamOS (F11 or the sidebar button), opening with a 2-second intro (any button skips it): Steam artwork, a shelf of your games, Library grid, Downloads; any Linux game controller, keyboard or mouse. Quiet, warm sounds as you move around and press into things (a soft low "bop", two mellow rising notes — no shrill tones since 0.6.6), and a muffled tap for the in-app browser's pointer (Settings → App → Sounds, with their own volume; the intro has its own volume too). The big ✕ leaves it — and first closes, completely, a game started from it. |
| **Wishlist** | Its own tab: add any game through Steam's store search (even unreleased ones) or with ♡ on a game page; the app keeps looking for it on every source and notifies you when one has it. |
| **Game updates** | Knows which version is installed (from the archive name) and checks the game's page every few hours; *Update* downloads the new release over the installed game — saves, settings, fixes and the Proton prefix stay. Optional: updates download and install by themselves. |
| **Save backups** | Saves in each game's Proton prefix are zipped after every session and before updates, uninstalls and prefix rebuilds (caches/logs left out); restore with undo; backups survive a reinstall — and, with a synced folder (Syncthing, Nextcloud, Drive), move between your PCs. |
| **Storage** | Disk use per game (files, prefix, downloads, saves), free space per drive, and one-click cleanup of archives, leftovers, cache and unused prefixes. |
| **App updates** | Checks a GitHub releases page once a day; downloads are verified against GitHub's SHA-256 and the app swaps itself in on restart (previous version kept until the new one runs). |
| **Settings** | *Find a setting* search box; folders, queue, torrent network & bandwidth, extraction, sources on/off, cache TTL & clearing, logs, accent colour / contrast / card size / reduced motion, notifications, database backup & restore. |
| **Runs on Linux?** | Every card, couch tile and game page shows how well the game runs under Proton — "Linux 92%" = 92 % of ProtonDB players' reports say it works (offline index; game pages add ProtonDB's own tier). |
| **Organise** | Favourites (always on top), collections, hiding, and the order of the Library (recently played, A–Z, recently added, most played) — on the desktop and in couch mode. |
| **Quick menu** | With *Our overlay instead of Steam's* on (Settings → Library & launching, and couch mode's Settings → Playing — off by default; off, Steam's overlay is the one over games and the quick menu does not show at all): hold the PS / Xbox / Steam button while playing (couch mode open) — or press **Shift+Tab** on the keyboard (KDE Plasma; only while one of the app's games has the focus, so Shift+Tab stays everyone's elsewhere, and Shift+Tab closes it again). Steam's overlay stays loaded in the game — its FPS counter and pop-ups keep working — but neither the button nor Shift+Tab opens it: KWin takes Shift+Tab before the game (and Steam's overlay in it) sees it, and the app keeps Steam's overlay from opening on the Steam button through its connection to the Steam client: a panel slides in over the game — back to the game, **switch game** (the one you were playing is *suspended*: frozen, using no processor or graphics time, and it continues exactly where you left it), go to couch mode (suspending the game), the game's own volume, its settings, quit (press twice). Suspended games show in couch mode and the menu; A continues them. Only games the app runs — directly or through Steam for its overlay; a game started in Steam itself is never touched. Over a game started from the desktop window the menu works the same, but couch mode never takes over by itself: quitting the game there (or the game ending) goes back to the desktop window, *Game settings* shows couch mode's settings screen only until you go back to the game, and only *Go to couch mode* opens couch mode. Leaving couch mode or quitting the app lets every suspended game run again. |
| **Screens & controllers** | Couch mode reads each monitor's real resolution from the desktop (KDE's kscreen-doctor, GNOME's Mutter, xrandr) — a 4K monitor at 150 % is 3840 × 2160, not the 5120 × 2880 Qt can report. On Wayland a full-screen window's size is the compositor's answer: couch mode never leaves full screen to "fix" it (that shrank it to 800 × 500 on some desktops); on Wayland *Borderless* is full screen (the same picture — and the only window the compositor always sizes to the monitor), and the desktop window comes back from couch mode the size it was (some KDE versions handed it couch mode's full-screen size) — KWin is asked to size it, and the log records the compositor and every size it sent for the first seconds ("couch display trace"). Interface size up to 200 % fits every page (on a 1440-high window 200 % is about 180 %, the most that fits). "Keep above" is for couch mode only and is released from every window when it closes. A Steam Deck / Steam Controller shows its own buttons (LB/RB on the original controller, L1/R1 on the Deck and the new one), also when Steam hands it to the app as keyboard and mouse: which key is which button is read from the Desktop Configuration Steam itself loaded (named in Steam's controller log — Steam's defaults, or the user's own), so every button means what its prompt says, and prompts for buttons Steam turns into mouse clicks are left out. The Steam button is read from Steam's log too: outside a game a tap goes Home and Big Picture is put away at once. Over a game the button is Steam's alone unless *Our overlay instead of Steam's* is on (Steam's overlay — friends, invites, the game's own *Invite* buttons; the app does nothing then, and no quick menu shows). With **Our overlay instead of Steam's** (Settings → Library & launching, and couch mode's Settings → Playing) games start without Steam's overlay and the button — tap or hold — always opens the quick menu, which sits in the middle between you and Steam: your Steam chats are in it (unread counts, the conversation, replies typed on the on-screen keyboard), **Invite** is one press (Steam's own *Invite to Game* — lobby or connect string; invites that keep coming to the same friend wait out a cool-down that doubles each time, from 15 s up to 5 minutes, while an invite a minute after the last is never held up), a friend's invite shows as **Join <name>** at the top of the menu, and joining goes through Steam — no Steam window opens. Both work the way Steam's own client does (its code, read 2026-10-27): **Invite** sends the game's lobby or its connect string (the private one first) — with neither, Steam offers no invite, and the menu says so instead of sending an empty one nobody can join; **Join** is offered only for invites Steam can join (a lobby, a connect string or a Remote Play session), and with the same link Steam's chat opens. An empty invite (OnlineFix games send `[gameinvite appid="480"]` when the game published no lobby) used to show Join and start Spacewar. Joining a friend's game from the friends list also uses their game's connect string, like Steam's *Join Game*; joining a friend in the game you are playing never suspends it. The friends list then comes from the Steam client itself too — live, without a Steam Web API key: in a game first (green ring), then online (blue; away / snooze dimmer), then offline (grey), like Steam; **X / △** (or typing a name) finds a friend, B shows everyone again. It talks to the Steam client itself, like Decky Loader: off by default; the first time it is switched on, two lines say what it does, Steam's debugging interface is switched on (`<Steam>/.cef-enable-remote-debugging`) and Steam restarts once (never while a game runs); switching it off removes that again (`ezgd/platform/steambridge.py`). A game's own *Invite* button needs Steam's overlay, so it does nothing with this on. (0.5.3 left the overlay out while a Valve controller was connected — that also stopped in-game invites.) Without Steam, a thumb resting on the left trackpad no longer scrolls. The on-screen keyboard has the same shortcuts on every controller, drawn along its bottom in that controller's own buttons: **B** (○) Backspace — on an empty box it closes —, **X / Y** (□ / △) Space, **Start** (Options / Menu) Enter; a Steam controller sent as keys types with A (its Enter) instead of finishing at once, and a held button presses once (only moving and Backspace repeat). The clock shows 12-hour time. Couch mode and the desktop stay in step: settings changed on either side show on the other at once, couch mode opens on the game selected on the desktop and the desktop selects the game couch mode was on, suspended games show on the desktop (Resume / Stop, and More → Suspend there too). |
| **Couch mode = desktop** | Everything the desktop Library does, by controller: **Finish setup** (a checklist from download to Play, one button for the next step — unpack, the repack's installer, the base game in Steam, the game's program), every fix, Run with, Steam App ID, DLL overrides, winecfg / Winetricks / reset prefix, *See what's wrong* (the debugging assistant's findings with their fixes), collections, rename, update check, repack check, add to Steam, remove from the Library, delete a save backup; and in Settings → Library: check every game for updates, Storage, clean up leftovers, import a game folder. Fix my game's test starts can be stopped (B / *Stop testing*). |
| **Steam friends** | The quick menu's *Friends* and couch mode's *Friends* tab: who is online, away or playing, and what — live from Steam's Web API with your own key (Settings → Library & launching → Steam friends; made once at steamcommunity.com/dev/apikey, kept on this computer and sent only to api.steampowered.com, never logged). Without a key, or with a private friends list, the friends Steam keeps on this computer are shown; with *Our overlay instead of Steam's* on, the list comes live from the Steam client itself (see *Screens & controllers*). A friend: **Chat** (Steam's chat window comes up), **Invite to <your game>** (while Steam shows you in a game — or, without a key, while one of the app's games is played: Steam has no link or API that sends an invite, so it opens Steam's chat with them and says to choose *Invite to Game* in the menu by their name; the game is named as in your Library, not Steam's "Spacewar" for OnlineFix games), **Join their game** (when they are in a joinable lobby or server — Steam starts it), **Play** the game they play (through Steam) and their **Steam profile**. |
| **Game settings (couch)** | Manage → Game settings, or from the quick menu: Proton version, Steam overlay, launch options (on-screen keyboard with symbols and capitals), FPS limit (DirectX games), MangoHud, GameMode — from the game's next start. |
| **GPU acceleration** | One switch (Settings → Interface, and couch mode's Settings → App), *recommended*, off by default. Couch mode is then drawn by the graphics card (OpenGL, the same picture; ~4× faster frames at 4K) from the next time it opens; after a restart the in-app browser decodes video on it and draws pages on it wherever Chromium trusts the driver (its own blocklist decides — forcing it past that froze a friend's browser) (desktop windows are not composed through OpenGL: on Wayland that drew menus garbled). Off, the processor does everything — one switch (the separate "frames through the graphics card" switch is part of it now). If OpenGL does not start, couch mode reopens drawn by the CPU and says so; if a start with it never reaches the window, the next start leaves it out. |
| **Quick ways in** | Drop a `.torrent`, a magnet / download link, a game folder or an archive on the window; a magnet link on the clipboard is offered as a download; tray menu, progress on the taskbar icon, *Keep downloading in the tray* when closing; the app reopens where you left it (page, All Games filters, selected game). **Pin to the taskbar**: the app writes its own launcher (`~/.local/share/applications/ezgd.desktop`, pointing at wherever the app is, rewritten at every start) with the icon the window shows, in your accent colour — the pinned icon looks the same open or closed (a launcher from install.sh or a package is left alone). |

## Source integration notes (verified)

The sites were inspected live before the adapters were written (OnlineFix and SteamRIP on **2026-10-09**).
The parsers are tested against trimmed copies of the real pages
(`tests/fixtures/`) and against the live sites (`pytest -m live`).

### OnlineFix

* **`onlinefix.me` (the address originally requested) is a parked domain.** It
  serves a “Redirecting…” page that posts browser fingerprints to an ad network
  (`router.parklogic.com`). The real site is **`https://online-fix.me`**; the
  adapter uses that address and detects parked pages (`LayoutChangedError`).
* online-fix.me's certificate chains to Let's Encrypt's new **ISRG Root YE**,
  which is not yet in certifi 2026.07 or common OS trust stores, and the server
  sends no cross-signed intermediate. Instead of disabling verification, the app
  bundles Let's Encrypt's official Gen-Y roots (`ezgd/resources/certs/`, fetched
  from letsencrypt.org and authenticated by their cross-signature from the
  already-trusted ISRG Root X2). TLS verification stays fully on.
* DataLife Engine site, windows-1251, posts in **Russian or English** (both are
  parsed). Listings: `/`, `/page/N/`, `/games/<genre>/page/N/`; search:
  `/index.php?do=search…` (21 per page, cp1251-encoded query).
* **Picks:** there is no separate picks page; the *OnlineFix Picks* section shows
  the home-page carousels “Популярное” (Popular) and “Обновления” (Updates).
* Download buttons on each game page and how the app treats them:

| Button | Observed behaviour | In the app |
|---|---|---|
| Torrent (`uploads.online-fix.me:2053/torrents/<Game>/`) | public directory listing with the `.torrent` — **except some releases** (e.g. *Minecraft Dungeons II*, seen 2026-10-09) whose folder answers *HTTP 401 Authorization Required* for anyone not signed in | **Downloaded in-app**; sign-in-only torrents become *Browser only* with a link to the game page (no attempt is made to authenticate) |
| Hosters (`hosters.online-fix.me:2053/<Game>`) | JSON list of mirrors (the site flags risky ones) | Pixeldrain → **in-app** (public API, SHA-256 verified); others → browser |
| Server files (`uploads…/uploads/<Game>/`) | listing public, files return **HTTP 403** without a site login | Browser only (not bypassed) |
| Drive (`drive.online-fix.me:2053`) | needs a personal API key | Browser only |

* **Every OnlineFix archive is password-protected.** Each game page states
  “Пароль един для всех архивов: online-fix.me” (one password for all archives).
  The app enters it automatically when extracting, and shows it — with a *Copy
  password* button — wherever an OnlineFix game appears: 🔒 badge and tooltip on
  game cards, a banner in the game page and the download confirmation, a chip and
  menu entry on the download row, and a banner in the Library (the manual *Extract*
  dialog is pre-filled). If a page shows a different password, that one is used.
* **Closed guides are hidden.** OnlineFix withdraws some games and marks them
  “Руководство закрыто” (*the guide is closed*), e.g. “…используйте версию от RUNE и Parsec”
  (*use the version from RUNE and Parsec*) or “…бесплатная игра по сети более невозможна”
  (*free online play is no longer possible*). Listing and search cards carry the marker, so those
  games never appear; the Picks carousels don't, so unknown carousel games are checked in the
  background (1 request/s, cached) and removed when closed. Opening one anyway shows the site's
  notice and no download options. A reopened guide reappears automatically. Toggle:
  *Settings → Sources → Hide games whose guide the source has closed*.
* **Source is always visible:** cards carry full “OnlineFix” / “SteamRIP” / “Cracked-Games” badges,
  and the game page, download confirmation, download row and library entry all name the site.
* OnlineFix torrents include an optional *Fix Repair* archive; it is not
  auto-extracted when the full game archive is present (you can deselect it before downloading).
* robots.txt disallows only `/catalog/`, `/engine/`, `/user/`, `/backup/`, `/uploads/fotos/` — none are used.

### SteamRIP

* WordPress site with the standard public **REST API** (`/wp-json/wp/v2/posts`,
  `/categories`) — used instead of HTML scraping. Title-only search via
  `search_columns=post_title`; covers via `_embed`; totals from `X-WP-Total(Pages)`.
* Game pages list *GAME INFO* (genre, developer, platform, size, version) and
  Steam screenshot URLs, from which the **Steam App ID** is taken (used for duplicate detection).
* **No torrents.** Links go to file hosts, none of which allow non-browser downloads:
  Buzzheavier (Cloudflare challenge), FileDitch (browser gate), Gofile (account
  token), MegaDB (captcha). They are shown as *Browser only* with the reason.
  Workflow: open the link, then paste the final file link into *Add download*, or
  import the downloaded archive in *Library*.
* **FileDitch only accepts visits that come from SteamRIP's game page** (opened directly it redirects to
  the steamrip.com home page — verified with Silver Pines). The app never fakes where a visit comes from;
  its *Open on SteamRIP* button opens the game page and tells you to click *FileDitch* there.
* **robots.txt disallows generic crawlers (`User-agent: *  Disallow: /`).** The
  adapter therefore behaves as a user agent, not a crawler: it only requests the
  page of results, search or game you are looking at, caches responses (the API
  itself sends `max-age=86400`), is rate-limited (1 request/second/host) and never
  indexes the site in the background. Disable the source in Settings if you prefer.

### cracked-games.org

* Laravel site; listings are read from `/games?page=N` (30 cards), genre pages
  `/games/genre/<slug>` and search via the listing filter `/games?search=…`.
  Game pages (`/<slug>-free-download`) provide JSON-LD (genres, dates, OS,
  developer), the build, release group, multiplayer notes, the **archive password**
  (`cracked-games.org` for every game) and, for many games, **CRC32 / SHA-256** of the archive.
* robots.txt allows `/`, the `/games` filter/pagination URLs and explicitly
  `/*-free-download`; it disallows `/download`, `/search`, `/api/`, `/livewire/` and
  account pages. The adapter only requests allowed URLs (enforced by tests), rate-limited and cached.
* **Downloads happen in the browser.** The site hands out files through a POST form
  with a CSRF token and a signed, time-limited token, and `/download` is disallowed
  for automated tools, so the app does not automate it. The game page shows
  *Download in your browser* and **Import downloaded file…**: pick the file you
  downloaded and the app checks it against the published SHA-256 (a mismatch is refused
  unless you choose *Import anyway*), adds it to the Library linked to the page and
  extracts it with the password. Nothing is run. The same import button appears for
  every browser-only game (e.g. SteamRIP), without the checksum step when the site publishes none.
* Listings carry no dates, so the default *Best match* order takes turns between
  sources instead of sorting undated games last.
* Games the site marks **18+ / NSFW** are hidden by default
  (*Settings → Sources → Hide games the source marks 18+*).

### Pixeldrain
`/api/file/<id>/info` exposes size, SHA-256, `can_download` and an `availability`
flag announcing captcha requirements; when a captcha is required the app falls back
to the browser instead of attempting the download.

## Install & run — Linux

### From a release (recommended — installs everything it needs)

```bash
tar -xzf EZGameDownloader-linux-x86_64.tar.gz
cd EZGameDownloader
./install.sh            # add --system to install into /opt for all users, --no-deps to skip packages
```

`install.sh` copies the app to `~/.local/opt/EZGameDownloader`, adds it to the application
menu, puts `ezgd` on your PATH, and then installs every missing tool with your package manager
after one password prompt (Arch/CachyOS/Manjaro, Debian/Ubuntu/Mint, Fedora/Nobara, openSUSE):

| Tool | Why |
|---|---|
| RAR extractor: UnRAR or 7-Zip (**required**) | Both sources ship games as RAR archives (the release bundles 7-Zip; the System check says which one is used) |
| umu-launcher | Runs Windows games with Proton even without Steam — downloads GE-Proton and the Steam Runtime itself |
| Steam | Only where umu-launcher is not packaged (Debian/Ubuntu, openSUSE): provides Proton |
| Winetricks (+ zenity) | Windows runtimes in a game's prefix |
| GameMode, MangoHud, gamescope | Launch-option helpers (`gamemoderun`, `mangohud`, `gamescope -- %command%`) |

Already-installed tools are skipped and packages your repositories don't have are reported, not
fatal. The same check lives in the app (**Settings → System check**: tick the missing tools you want — all but
optional Steam start ticked, umu-launcher is marked **★ highly recommended** — then **Install selected tools**, with a
graphical password prompt), and Home shows a banner if something essential is missing.
`ezgd --setup-system` runs it from a terminal. Remove the app with `uninstall.sh` (`--purge` also
deletes its settings/database; your games are never deleted).

### From source

Tested on CachyOS (Arch-based, kernel 7.2) with Python 3.12.14.

```bash
# 1. Python 3.12 (3.13 should also work) — uv is the easiest way:
curl -LsSf https://astral.sh/uv/install.sh | sh        # if uv is not installed
cd "EZ Game downloader"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
#   (without uv:  python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt)

# 2. Optional system tools
sudo pacman -S unrar          # or: 7zip / p7zip.  Debian/Ubuntu: sudo apt install unrar  (non-free)
#   Proton for Windows games: install via Steam (Library → Tools) or GE-Proton.

# 3. Run
.venv/bin/python -m ezgd
.venv/bin/python -m ezgd --home ~/EZGD-portable   # portable mode: everything in one folder
```

All Python dependencies — including the torrent engine — come as wheels from PyPI;
no external torrent client is needed.

## Install & run — Windows

From a release: unzip and run `powershell -ExecutionPolicy Bypass -File install_windows.ps1`
(Start-menu/desktop shortcuts; installs 7-Zip with winget if no RAR extractor is present).

> Windows support is implemented (paths, launching, file dialogs, RAR tool
> detection) and every dependency ships cp312 `win_amd64` wheels, **but it has
> not been run on Windows in this development environment** — see the test report.

```powershell
# 64-bit Python 3.12 from python.org (tick "Add to PATH"), then in the project folder:
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m ezgd
```
For RAR archives install **7-Zip** or **WinRAR** (detected automatically in
`Program Files`) or set the tool path in *Settings → Extraction*.
Data lives in `%APPDATA%\EZGameDownloader`, cache/logs in `%LOCALAPPDATA%\EZGameDownloader`.

## Building distributable packages

PyInstaller one-folder builds (≈310 MB, mostly Qt; Qt Multimedia plays the couch intro). Build on the target OS.
The couch intro (`ezgd/resources/couch/intro.webm`, VP9 + Opus) is generated by `packaging/make_intro.py` (Qt draws
the frames from the app icon's shapes, the sound is synthesised, ffmpeg encodes) — rerun it after changing the logo.

```bash
packaging/build_linux.sh            # → dist/EZGameDownloader/ezgd  + dist/EZGameDownloader-linux-x86_64.tar.gz
```
```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1   # → dist\EZGameDownloader\ezgd.exe + zip
```
Linux desktop entry: copy `dist/EZGameDownloader` to e.g. `~/.local/opt/`, link
`ezgd` into `~/.local/bin`, and copy `ezgd.desktop` / `ezgd.png` to
`~/.local/share/applications` and `~/.local/share/icons`.

## Configuration

* Everything is configurable in **Settings** and stored in SQLite.
* `config.sample.toml` documents every option. Copy it to the config folder as
  `config.toml` to seed settings on first start, or apply it any time with
  `python -m ezgd --import-config config.toml`.

| | Linux | Windows |
|---|---|---|
| Database, backups, `.torrent` copies, Proton prefixes, reports | `~/.local/share/EZGameDownloader/` | `%APPDATA%\EZGameDownloader\` |
| `config.toml` | `~/.config/EZGameDownloader/` | `%APPDATA%\EZGameDownloader\` |
| Page/cover cache | `~/.cache/EZGameDownloader/` | `%LOCALAPPDATA%\EZGameDownloader\cache\` |
| Logs (`ezgd.log`, rotating) | `~/.local/state/EZGameDownloader/logs/` | `%LOCALAPPDATA%\EZGameDownloader\logs\` |
| Default downloads / installs | `~/Downloads/EZ Games`, `~/Games` | same under your user profile |

`--home DIR` (or `EZGD_HOME`) puts all of the above under one folder.

## Using the app

1. **Find a game** in *Home*, *All Games* or *OnlineFix Picks* and open it.
2. **Choose a download method.** *In-app* methods open a confirmation dialog:
   destination (with free space), auto-extraction folder and, for torrents, the
   file list. *Browser only* methods open the host page and explain why.
3. **Downloads** shows progress; you can close the app at any time — transfers
   resume on the next start (HTTP via Range requests, torrents via resume data).
4. When a transfer finishes it appears in **Library** as *Downloaded*; supported
   archives are extracted (*Extracted*). Pick the executable (best guesses first),
   choose the launch method (Linux: Proton/Wine/native), then **Launch** — the app
   always asks for confirmation the first time and never starts anything on its own.
   *Mark as installed* records that the game is ready.
5. Already have a game or an archive from your browser? *Library → Import game…*

### Download in app (built-in browser)

Every browser-only link (Buzzheavier, VikingFile, Gofile, FileKeeper, FileDitch, cracked-games.org …) has a
**Download in app** button. A small browser window (Qt WebEngine — a real Chromium) pops up **only for your
part**: do the “I'm not a robot” check if one appears and click the site's own download button. Opened from a
game's page (a dialog that waits for you), the browser is the window on top and takes every click and its ✕ — before
0.6.5 Qt blocked it behind that page: clicks and ✕ did nothing while the page kept drawing ("half frozen"). The
window then closes by itself and the game's page has the focus again (0.6.5 left KWin activating the main window,
raised above the page that was blocking it: nothing took a click and the app had to be killed); the transfer continues in the background under **Downloads → In-app browser downloads**
(progress, speed, time left, pause / resume / cancel) and, when it finishes, is **added to your Library
automatically** (SHA-256 checked when the site publishes one, extracted with the password, launch files
detected). The file is saved in the game's own folder, like every download, and **deleted once the game is
extracted from it** (it is kept if extracting fails). Nothing is automated, faked or bypassed: the app never solves checks, never sends a fake Referer and
never copies the browser's pass or cookies into its own downloader — the transfer is done by the same browser
you used. Pop-ups to unrelated sites are blocked unless you clicked them (links that open the host in a new tab work; **Back** returns), and
program downloads (`.exe`, `.msi` … — the fake download buttons on file hosts) are refused; sites cannot
use the camera, microphone, location or notifications; the address bar accepts a pasted link and *Open in
my browser* is always there as a fallback; cookies and cache are kept in memory only and discarded when the app closes. Hosts that only
accept visits from the source's page (FileDitch, Buzzheavier — verified 2026-10-10) open on that page so your
click comes from there. These transfers pause and resume while the app is open but **cannot continue after the
app is closed** (you are warned when quitting); torrent and Pixeldrain downloads can. The component adds
~310 MB to the app.

### Ad blocker (in-app browser)

On by default (switch in the browser window and in *Settings → Sources*):

* **Ad, pop-up, tracker and malware servers are blocked** — 72,500 domains from
  [StevenBlack/hosts](https://github.com/StevenBlack/hosts) (unified adware + malware list, MIT licence,
  bundled in `ezgd/resources/adblock/` with its licence) plus a few pop-under networks it does not list yet.
  Pages cannot load ads, pop-up scripts or overlays from them, and a page cannot redirect itself to one.
* **Filter lists, like uBlock Origin** — about 154,000 rules from EasyList, EasyPrivacy, uBlock filters (+ badware
  risks, + unbreak) and Peter Lowe's list, run by Brave's adblock-rust engine (`adblock` package). They block ad
  scripts by their path even on rotating throw-away domains, `$popup` rules decide new windows and redirects,
  and ad elements are hidden (site-specific rules, plus generic ones for the classes/ids actually on the page).
  Bundled in `ezgd/resources/adblock/filters/` (licences in `LICENSES.txt`) and refreshed daily with the domain list.
* **Pop-unders are stopped**: the browser remembers what you actually pressed (link or form) — on mouse/pointer
  *down*, since pop-under scripts fire then — in an isolated script world the page cannot tamper with. A new
  window only opens if it goes where that link points; one opened when you pressed something else is blocked
  with an *Open it* button. Blank pop-ups (`window.open('')` then sent somewhere by script) are held until they
  navigate and judged by where they go. A page that sends itself to an unrelated site right after you pressed a
  link to another one (“tab-under”, swapped links) is stopped too.
* **Nothing can lock the window**: `alert` / `confirm` / `prompt` never open modal boxes (their text appears in
  the notice bar; questions are answered *No*), full-screen and log-in requests are refused, `window.close()`
  closes only that tab, a crashed page offers *Reload*, and a page whose script stops answering for 8 s offers
  *Stop this page* / *Reload*. Every tab has its own view, so Cancel always works.
* **Kept up to date automatically**: once a day the app asks GitHub whether StevenBlack has published a newer
  list (and each filter list's server whether it changed) (a conditional request — nothing is downloaded when it has not changed; the list is updated about
  weekly). A download only replaces the current list if it parses into a complete list (≥ 30,000 domains);
  otherwise it is ignored and the working list stays. *Settings → Sources* shows the domain count, list date and
  last check, with *Update now* and an auto-update switch. Every build also bundles the newest list
  (`packaging/update_adblock.py`, run by the build scripts), so fresh installs start current.
* The sources and file hosts the app works with, and the Cloudflare / hCaptcha / reCAPTCHA checks, are never
  blocked. The counter next to the switch shows how much was blocked on the current page; if a site
  misbehaves, switch it off and reload.

### Steam overlay

**Full overlay (default)** — *Settings → Library & launching → Full Steam overlay*: Steam only gives its complete
overlay (Shift+Tab any time: friends, chat, browser, screenshots, FPS counter, Steam Input — in those games
Shift+Tab stays Steam's; the quick menu exists only with *Our overlay instead of Steam's*) to games it starts
itself and knows. So every game gets a non-Steam shortcut in Steam that runs this app in play mode
(`ezgd --play <id>`, which starts the game exactly like Play — same Proton, runtime, fixes, launch options —
writes its log and records playtime), and Play asks Steam to start it. Steam reads its shortcut list only when it
starts and rewrites it when it exits, so shortcuts are only written while Steam is closed: the first Play asks
once to restart Steam (all games are added in one go), and later games are added automatically whenever Steam is
closed (checked every minute). `SteamAppId`, which Steam sets for shortcuts and which breaks some releases, is
removed unless the game's Steam App ID option asks for it. *Add to Steam* in a game's menu creates the same shortcut.

**Never twice in Steam.** Before adding a game the app checks every Steam user's list: if Steam already has the game —
a shortcut made outside the app that starts its executable or anything in its folder, or has its name (`PEAK.exe` for
*PEAK*), or the app's shortcut for another copy of it in the Library — nothing is added (that shortcut is left as it
is; Play then starts the game directly with the friends-list overlay and says why). The app's own shortcut is updated
in place, also after the app has moved, extra copies of it from older versions are removed, and the leftover shortcut of
a game removed from the Library is reused rather than doubled.

**With *Our overlay instead of Steam's*** the games are still added to Steam (in its library, the same shortcut — asked
once per session when Steam has to restart for it, never from couch mode), but Play starts them directly. *Never add
games to Steam* in that question switches it off. Steam's overlay is still loaded into them (unless *Steam overlay in
games* or the game's own *Steam overlay* switch is off) — for its FPS counter, pop-ups and in-game Invite buttons — but
the Steam / PS / Xbox button and Shift+Tab open the quick menu instead of it:

* **Shift+Tab** is a KDE global shortcut while one of the app's games has the focus — and while the quick menu itself
  has it, so the Shift+Tab that closes the menu is KWin's too. KWin runs global shortcuts before the focused window gets
  the key (KWin 6.8.1: the press and its release are both held back), and Steam's overlay looks for its key among the
  game's own key events — so it never sees Shift+Tab. (A program that asks KWin to inhibit shortcuts gets every key
  itself; then Shift+Tab is Steam's.) Held down, Shift+Tab counts once.

  *Fixed after the first try:* the key used to be given up while the menu had the focus. The press that closed the
  menu then went to the menu window, which hid at once — and KWin hands a window that gets the focus the keys still
  being held, so the game got that Shift+Tab back and Steam's overlay opened on it (Steam's log: its overlay shown the
  second the menu closed). A key KWin took is neither handed over nor is its release sent to anyone; replayed with
  real key presses against a test window with Steam's overlay: before, the window got the Tab; now nothing of it.
* **The Steam button** is read by Steam itself, before any other program. In current Steam the in-game overlay opens on
  it from Steam's own interface code: each game's overlay registers a handler for the button that opens or closes it
  (checked in Steam's code and with a test window, 2026-10-28). Through the same connection the quick menu uses for
  chats, the app holds that handler back for its own games (by the game ids Steam's overlay runs under), renews this
  every 10 s while such a game runs, and gives the button back to Steam as soon as none does or the setting is switched
  off — if the app stops, Steam has it back within 30 s. Nothing is written to Steam's files. The log says when it is
  held ("the Steam button over game ids … opens the quick menu only") and how many presses it held back.

**Quick overlay (fallback)** — for a game Steam does not know yet (you chose *Just play* instead of restarting
Steam), the app loads Steam's overlay into the game itself (`gameoverlayrenderer.so` preloaded — Steam Linux Runtime
carries it into its container — and Steam's Vulkan layer, `ENABLE_VK_LAYER_VALVE_steam_overlay_1`). Steam then only
shows the friends list and Shift+Tab may not reopen it; once the game's shortcut is in Steam, its game id is passed
too. Both can be switched off; *Fixes → Turn off the Steam overlay for this game* keeps it out of one game.

### Controllers and the screen while playing

**Controllers act as a gamepad, not as Steam's desktop mouse.** While Steam runs it drives the controllers it supports
(always Valve's — Steam Controller, Steam Deck — and PlayStation / Xbox / Switch pads with Steam Input on) and picks
their layout by **window focus**: the focused window of a game Steam knows gets that game's layout; anything else —
the desktop, this app's windows, its quick menu — gets Steam's *desktop layout* (stick or trackpad = mouse, buttons =
keys; the virtual Xbox pad games read is gone). Steam knows its own games, the games it starts from this app's
shortcuts (Steam's gamepad template, or the layout chosen for the shortcut in Steam) and the games that sign in to it
(OnlineFix games, as Spacewar 480). It never knows a game this app starts that does not talk to Steam — emulators (a
friend's Bloodborne, shadPS4 from SteamRIP: "the mouse was moving with the controller"), most repacks.

**Spacewar's layout is not a gamepad** (measured 2026-10-28). The first time anything runs as Spacewar, Steam picks
Valve's own Steam Input API layout for it ("Space War Action Set Config Sample", Workshop 1417465272): every binding
is a Spacewar action, so Steam makes no virtual pad (`mapping uses xinput : false` in its log) and a game that is not
Spacewar gets nothing. That is what every OnlineFix game gets by itself — started by the app or by Steam (Steam had
readied the shortcut's gamepad template, then switched to Spacewar's the moment the game's window had the focus) —
and what 0.6.9 and earlier asked for themselves for the other games. Other controllers did not show it: games read a
PlayStation or Xbox pad themselves. A Steam Controller or a Steam Deck can only reach a game as Steam's virtual pad,
so in those games it did nothing at all (unless the player had changed Spacewar's layout in Steam).

What the app asks Steam for while one of its games runs (`steam://forceinputappid/<app>`, measured in the background
with Steam's logs: Steam switches within ~50–100 ms, and an id it does not know is ignored without a trace):
* **with a Valve controller connected** (Steam Controller, Steam Deck, the new Steam Controller): the game's own layout
  in Steam — the app's shortcut for it, or the player's own shortcut for the same game: Steam's gamepad template, or
  the layout chosen for it in Steam — else the layout of another of the app's Steam shortcuts, and Spacewar's only as
  the last resort. The app checks that Steam made its virtual pad for the layout (`/sys/class/input`, 28de:11ff) and
  moves on to the next one if not; when none does, a message says what helps (add the game to Steam, or choose a
  gamepad layout for it in Steam). OnlineFix games get this too;
* **with other controllers only**: Spacewar's layout for games Steam does not know (as before) — it sends nothing, so
  Steam's desktop layout (stick = mouse) stays out of the game, which reads the controller itself, and there is no
  second, virtual pad (the same controller twice); OnlineFix games are left to Steam;
* **a game Steam started (play mode)**: Steam's own layout for its shortcut, as before — except an OnlineFix game:
  Steam switches it to Spacewar's the moment its window has the focus, and hides the real controller from games it
  starts, so its shortcut's layout is kept while it runs (play mode records which shortcut Steam started) — with a
  Valve controller, or when Steam Input is on for other controllers in that game (Steam's "Opted-in Controller Mask"
  at the start: 0x1000 is every app's, the low bits the other controllers'; measured: a DualSense got a layout with
  1001, none with 1000). Otherwise the game reads its controller itself and Steam's choice is left alone;
* a game that runs as a real Steam app (OnlineFix as the genuine app, *Tell Proton the Steam App ID*) keeps that
  app's layout.

Whether Steam took a layout, and made a pad for it, is read back from Steam's `logs/controller.txt` ("Queueing
activation for controller: 0 app: …" — Steam writes a shortcut's id there as a negative number) and written to the
game's log (`ezgd:steam-input …`). GE-Proton and Proton-CachyOS ignore Steam's virtual pad with *Without Steam Input*,
*Controllers through SDL* or *Native Wayland* (their `winebus`: "ignoring steam input virtual controller"); with a
Valve controller connected the app says so when such a game starts, and *Fix my game* no longer suggests the first two
for controller problems. Off: *Settings → Library & launching* (`ezgd/platform/steaminput.py`).

What Steam does with a forced layout was measured on Steam itself (2026-10-27, reading `controller.txt` and
`controller_ui.txt`, which logs the reason for every choice: "OnFocusWindowChanged … URL Forcing … 480"):
* it is used at once and **kept** through every focus change, through Steam's **Steam button** (its chord layout
  while held, then the forced one again) and through Big Picture;
* `steam://forceinputappid/0` switches **nothing** by itself — the forced layout stays until the next focus change of
  an X11 window. So when the last game of the app ends, the app forces the desktop layout and releases it once Steam
  switched (a game still running keeps its layout); a Steam Controller no longer stays a gamepad on the desktop;
* steam:// links reach Steam asynchronously, so the second link waits until Steam logged the first.

**The quick menu keeps the game's layout.** Over a game whose layout the app asked for, nothing changes: the
controller stays the game's virtual pad and the menu reads that pad (0.6.3 forced the desktop layout for the menu —
the pad went away and came back as a new device, which a game may never pick up again: a friend's Dark Souls III).
Over a game Steam chose the layout for (a game Steam started), the menu taking the focus would make Steam switch to
its desktop layout, so the layout Steam chose for the game (its log names it: "to game window type: AppID …") is
asked for just before the menu shows and released a second after it goes. While the menu is open, Steam's virtual pad
is read by the app alone (the kernel's `EVIOCGRAB`): the game reads the same pad, and a press in the menu — B to
close it, A on *Quit* — reached the game too. The pad is only taken once everything on it is let go (the current
state is read when the pad is opened), and given back if a press from just before it was taken reached the game —
otherwise the game would keep a stick or a button held that it never saw released. While any game of the app runs,
Steam's choices go to the app's log ("Steam switched the controller to the … layout", "Steam controller layout: …"),
and a forced layout Steam lost (Steam restarted) is asked for again; switching back to a suspended game asks for its
own layout again. A layout still forced when the app quits — or left by a crash — is handed back (at the next start);
play mode (a game Steam starts) never does that, and never thaws the games the quick menu froze to switch games — it
ran next to the app and did both.

**The screen stays on.** A controller is not activity to the desktop: KDE and GNOME dim and lock after their idle time
while you play with one (Steam only prevents it for games it starts). While one of the app's games runs — not while it
is suspended from the quick menu, not for hidden test starts — and while couch mode is open, the app holds an idle
inhibitor (`systemd-inhibit --what=idle`, listed by KDE as "EZ Game Downloader: <game> is running"); it ends with the
game, and the system drops it if the app quits or crashes (`ezgd/platform/awake.py`). Off: *Settings → Library &
launching*.

### Uninstall

Library → a game → *Uninstall…* (on the Files card or in the ⋯ menu) lists everything that belongs to the game,
with sizes: installed files, downloaded archives (every part of a multi-part set, and the game's entries in
Downloads), the Proton prefix (where Windows games usually keep their saves — untick it to keep them) and the
Steam shortcut if you added one (a backup of `shortcuts.vdf` is kept). Tick what should go; the log is deleted.
After deleting, the app **checks that every folder is really gone**: if something could not be deleted (a file
in use, no permission), the game **stays in the Library** and the window says in red exactly which paths are left
and why, with *Retry*. Only files inside the app's own download/installation folders are deleted; anything
elsewhere, or still used by another Library entry, is listed in red and left in place. A running game must be
stopped first; every attempt is logged. *Remove from Library (keep files)* removes just the entry.

**Clean up leftovers…** (Library header) finds game folders and archives in the Games/Downloads folders that no
Library entry or download uses any more — e.g. games removed with *keep files* or by older versions. The app
records everything it creates (downloads, extracted games, imports); only those are pre-ticked. Older downloads
are recognised by release names (`-OFME`, `SteamRIP`, `Fix_Repair`, a catalog title + version) and offered
unticked; **anything else you keep there (an engine, tools) is never listed**.

### Couch mode (TV / controller)

Press **Couch mode** (bottom of the sidebar) or **F11**. A 2-second intro plays first — the logo lands, with a short
sound — while couch mode loads behind it; any button, key or click skips it (*Settings → App*: intro on/off, sound
on/off). It takes over the screen like SteamOS's game mode: the
focused game's artwork fills the background, with its playtime, a big **Play** and a shelf of your games
(most recently played first). **LB/RB** (or Page Up/Down) switch between *Home*, *Library* (every game, A–Z) and
*Downloads* (progress of downloads and installs).

* **Controllers:** any game controller Linux sees (Xbox, PlayStation, Switch Pro, 8BitDo, Steam Deck …), plugged
  in before or while couch mode is open. Only controllers are opened, never keyboards or mice. A = select/play,
  B = back, X/Y = details, D-pad/left stick = move (hold to repeat). Steam's virtual controller and the real one
  are not counted twice. Keyboard: arrows, Enter, Esc, I (details); the mouse works too.
* **Store:** grids show 50 games and add the next 50 when you press down at the bottom — what is on screen never
  re-sorts while results arrive. It shows only games that work with a controller — Steam's *Full* or *Partial Controller Support*
  (the Steam App ID comes from the site, ProtonDB's title list or Steam's search; answers are cached). Games
  Steam does not know are hidden too. *Settings → Store* switches it off.
* **Playing:** A starts the game the way Play does (the same confirmation, Proton setup and fixes). Couch mode
  steps aside while you play and comes back when the game ends ("Welcome back"). It never restarts Steam.
* **Quit from the controller:** with *Our overlay* on, hold the PS / Xbox / Steam button while playing to open the
  quick menu; its *Quit* (press twice) stops the game completely, like the ✕. Only games the app runs are touched.
* **The game behind the quick menu:** some games minimize themselves when another window takes the focus (Dark Souls III
  in full screen), and the see-through menu showed the desktop. On KDE the screen is photographed (KWin, ~30 ms) just
  before the menu opens and stays behind it; *Back to the game* brings the game itself to the front again.
* **Organise:** X (Manage) on a game: ★ favourite (favourites come first), hide it, and the Library's order.
* **The ✕** (top right; B on the home shelf jumps to it): leaves couch mode and brings the normal window back. If a
  game you started from couch mode is still running it asks *Quit <game>?* first, then closes that game
  completely — asked to quit, force-stopped after a few seconds if needed (including Wine processes that left the
  game's process group), and checked to be gone. If something would not stop, it says which processes, with
  *Try again* / *Leave anyway*. Alt+F4 does the same as the ✕. Games started from the desktop are not touched.

**Smoothness.** Couch mode draws everything itself and keeps every frame under the screen's refresh time at 4K:
pages are painted once into cached layers and then only copied (a game page with a long Steam description took
0.5–1 s per frame before — now ~6 ms), caches have byte budgets (memory levels off instead of growing past 1.5 GB),
freed memory is handed back to the system, and the long-lived start-up objects are kept out of the garbage
collector's way (its full pass went from 33 ms to ~2 ms). Each 10 s the log gets a `couch frames` line with paint times
and stalls.

### Desktop shortcuts and quick ways in

| | |
|---|---|
| **Drag and drop** | Drop a `.torrent` or a magnet / download link on the window → *Add download*, filled in. Drop a game folder or an archive (all parts of a multi-part set count once) → imported into the Library, with a clean title suggested (`How.to.Fish.v1.0.10-OFME.rar` → *How to Fish*). |
| **Clipboard** | Copy a magnet link (or a link to a `.torrent`) in the browser and come back to the app: it offers *Add download* — once per link, nothing happens without a click. |
| **Tray & taskbar** | The tray icon's menu opens the app or couch mode, pauses / resumes all downloads, or quits; its tooltip shows the speed, progress and time left. Closing the window while downloading offers *Keep downloading in the tray*. On KDE Plasma (and other docks that read Unity launcher entries) the taskbar icon shows the overall progress and the number of downloads (needs the installed `ezgd.desktop`). |
| **Remembered** | The page you were on, All Games' sources / filters / sort and the selected Library game come back at the next start. |
| **Keys** | Ctrl+F search · Ctrl+1…7 pages · Ctrl+N add download · Ctrl+, Settings · F5 refresh the page · F11 couch mode · Esc clears a search box · Enter plays the selected Library game · F1 lists them all. |
| **Badges** | *Wishlist (2 available)* when a site has a wished game; *Downloads (n)* while downloading. |

### Wishlist

The **Wishlist** tab: search Steam's store and press *Add* — any game, also one that is not released yet or that
no site has — or press **♡ Wishlist** on a game's page. Every game is identified by its Steam App ID (and its
title, without edition suffixes such as *Deluxe Edition*), so it is recognised whatever each site calls it. The app
checks the local catalog all the time and searches the sources every few hours (only those that allow it —
SteamRIP is searched only when you press *Check now*; unreleased games are not searched in the background). When a
source has it you get a notification, and *Get it* opens its download options. Closed guides are shown as such.

### Download hours and playing

*Settings → Downloads*: **Download hours** (e.g. 01:00–08:00) — outside them downloads wait in the queue and continue
by themselves when the hours start (downloads you paused stay paused; the sidebar says "Downloads wait until 01:00").
**While a game runs** caps torrent and direct downloads at a speed you choose, so online games do not lag; the normal
limits come back when the game ends. **Start with the computer** puts the app in the tray at sign-in (XDG autostart
on Linux, the Run key on Windows; `ezgd --minimized` does the same by hand).

Before downloading, the game page and the download confirmation show the space needed — the download **plus** the
installed game (the page's own "installed size" when it states one, else an estimate) — and warn when the download
fits but installing it would not.

### Game updates

Each game remembers the release it was installed from (`How.to.Fish.v1.0.10-OFME.rar` → 1.0.10, else the version
the page listed). Every few hours the app re-reads the installed games' pages (Settings → Library to switch it
off; SteamRIP only on *Check for updates*) and tells you when a newer version is out. **Update to …** marks the
game as updating and opens its page: the next download or import of that game goes **over the installed game**
(the same folder, the same Library entry) — its saves are backed up first; its settings, fixes, playtime and
Proton prefix stay. *Cancel update* undoes the mark.

**Install game updates automatically** (*Settings → Library*, off by default): a new version found by the check is
downloaded by itself from the game's page (in the app — torrent first, it resumes) and installed over the game the
same way. A game that is running, a page with only browser downloads or a multi-part set is left to you (you get the
usual "update available" message); each version is tried once.

### Save backups

Windows games keep their saves in their Proton prefix (`AppData`, `Documents`, `Saved Games`, and
`users/Public/Documents` for OnlineFix/RUNE/CODEX releases). The app zips those folders — without shader/DXVK
caches, logs, crash dumps and Windows' own folders — **after every play session** (sessions over a minute), and
**before an update, an uninstall, a prefix rebuild, a fix that changes the prefix and every restore**. Unchanged
saves are not zipped again; each game keeps its newest **3** backups — a new one deletes the oldest (never the one
being restored). *Settings → Library → Keep more than 3 save backups per game* (also in couch mode's Settings → Playing)
keeps the newest 10 automatic and 10 safety backups (20 manual) instead. Backups are kept per game *title* — so a
reinstall finds them. Library → a game → **Saves**: *Back up now*, *Restore…* (your current saves
are backed up first), delete, open the folder. Backups live in the app's data folder (`save-backups/`).

**Synced save folder** (*Settings → Library*): choose a folder that Syncthing, Nextcloud, Google Drive … keeps in
sync, and backups are written to `EZ Game saves/<game>` there — another PC with the same folder and this app lists
and restores them. Backups made before are copied in; if the folder is not there (an unplugged drive), backups stay
on this PC.

### Storage

Library → **Storage…**: free space on the drives the app uses, what the app keeps (game files, Proton prefixes,
downloads/archives, save backups, cache), every game's size, and what can be freed: archives of installed games,
leftover folders, the cache, and prefixes no game uses any more (games removed from the Library, older copies
kept when a prefix was rebuilt). Nothing is deleted without asking.

### App updates (GitHub releases)

Settings → **App updates**: enter the GitHub repository that publishes the app's releases (`owner/name`). The app
checks once a day (or *Check now*), *Download update* fetches the release's
`EZGameDownloader-linux-x86_64.tar.gz` and checks it against the SHA-256 GitHub publishes for every uploaded file
(or a `SHA256SUMS` file) — without a checksum nothing is installed — unpacks it next to the app and runs its
`--version` to make sure it starts. **Restart and update** quits the app; a small script swaps the folders and
starts the new version (if the swap fails, the old version is put back and started). The previous version is kept
next to the app until the new one has run for a minute. Only packaged builds update themselves (Linux; on Windows
the release page has the download).

**Signed releases.** The checksum only proves the download is the file on the release page — not who put it there:
whoever got hold of the GitHub account or a token could publish a "new version" that every copy installs by itself.
So every release file is also signed (Ed25519) over its SHA-256, version and name with a private key that never leaves
the developer's computer (`~/.local/share/ezgd-release-signing/ed25519.key` — back it up; not in the repository), and
the app installs nothing whose `<file>.sig` does not verify against the public key it carries
(`ezgd/core/release_keys.py`). A signature of one version cannot be passed off as another.

Publishing a release: bump `__version__` in `ezgd/__init__.py`, then
`packaging/build_linux.sh && packaging/publish_github.sh owner/name` (needs `gh auth login`; it signs with
`packaging/sign_release.py`, made once with `--init`).

### Your own sites

*Settings → Sources → Add a site…* adds a game download site like SteamRIP. Supported are WordPress sites with
the public posts API (`/wp-json/wp/v2/posts`) — the same interface SteamRIP is read through — so listing,
search and game pages work reliably; magnet and `.torrent` links download in the app, file-host links use the
in-app browser. Before adding, the app checks the site and refuses it, **with the reason in red**, when:
it is a video/music/social/shopping platform (YouTube, Spotify, Netflix, Twitch, Reddit, Discord …) or an
official store (Steam, Epic, GOG …); it is already built in; its robots.txt asks tools not to read it; it is
behind a Cloudflare bot check or a login; it has no public WordPress posts API; or its newest posts do not
contain PC game downloads (torrent/magnet or file-host links with game details). Verified live: FitGirl Repacks
is accepted (8/10 posts with downloads, magnets in-app); YouTube, Spotify, example.com, TechCrunch (robots.txt)
and DODI (API switched off) are refused with their reasons. Added sites can be switched off or removed.

### Debugging assistant (Diagnose)

Library → a game → **Diagnose** explains why a game does not run, from evidence: the app's log of the last run
(exit code and run time, Wine/Proton output), the engine's own log (Unity `Player.log`, Unreal `Saved/Logs`,
Godot), crash markers, the game's files and the system (64/32-bit Vulkan drivers). *Run a diagnostic launch*
starts the game once with Wine's error, DLL-loading and exception logging and analyses that run when it exits.
It names the most likely cause with the log lines that prove it and offers the matching fix — e.g. a missing
Visual C++ / DirectX 9 / XNA / .NET runtime → install exactly that into the game's prefix (winetricks); DirectX
12 failing in an Unreal/Unity game → `-dx11` / `-force-d3d11`; a crash inside DXVK → WineD3D; Vulkan not
starting for a 32-bit game → the missing 32-bit driver; videos not decoding → GE-Proton; a launcher that
exits at once → the real game executable; the release's own checks (OnlineFix DLLs, launch scripts, Godot
renderer). It says plainly when a cause has **no fix under Proton** (anti-cheat, a CPU instruction the CPU
lacks, an old crack patching Windows code — the Black Ops II case) and when **nothing** in the logs matches,
instead of guessing.

*Learning on this computer:* every fix the assistant applies is followed up by the game's next start. A fix that
did not help a game ranks lower for it next time ("✗ You tried this for this game before"); one that fixed other
games — especially with the same engine or from the same source — ranks higher ("✓ This fixed 2 other games like
this one").

**Error pop-ups are read, not guessed:** every Windows game is started with Wine's `msgbox` channel, so the text of
any error box it shows ("VCRUNTIME140_1.dll was not found", "DirectX 12 is not supported" …) lands in the game's log.
Diagnose quotes it first and offers the matching fix; a diagnostic launch or test run that hits an error box reads
it and closes the game at once instead of waiting.

**Deeper checks** (no game run needed):
* **Import tables**: the game's exe and the DLLs it loads from its folder are read (never run) and every
  imported DLL is checked against what the *selected Proton actually ships*, the game folder and the prefix — a
  missing runtime is predicted before the game starts; 32/64-bit mix-ups (a 32-bit mod DLL in a 64-bit game →
  “bad image”) are caught.
* **Crash dumps**: `.dmp` files the game, its engine (Unreal `Saved/Crashes`, Unity `Crashes`) or a crack wrote
  are read for the exception and the module + offset it happened in.
* **Unimplemented functions**: when Wine's own runtime lacks a function the game calls, the fix is Microsoft's
  runtime (winetricks); when Wine lacks it entirely, it says a newer Proton is the only hope.
* **Crashes while closing** (after the engine's normal shutdown in its log) are reported as such — harmless if you
  quit yourself — instead of being blamed for the problem.
* **System health**: `vm.max_map_count` (big games crash at loading below 1048576 — the exact command to raise
  it is offered, since it needs administrator rights), the open-file limit, free space on the game/prefix drive,
  prefixes or games on NTFS/exFAT/FAT drives, the GPU(s) and driver (read from the kernel's lists — no GPU tool is
  started: one `vulkaninfo` at every app start made the whole desktop stutter on NVIDIA).
* **ProtonDB** rating (with a Steam App ID): a *Borked* game is said to be unlikely to have a fix.

**Try fixes for me** tests candidate setups for real — the current one, the fixes the findings recommend, the
other installed Proton versions and WineD3D for graphics problems (at most 6). Each trial launches the game with
detailed logging in a **throw-away copy of its prefix** (an instant reflink copy on btrfs/xfs, so winetricks
installs are kept; your real prefix and saves are never touched), for up to 45 seconds, and reports whether it
kept running, crashed (with the reason) or exited. *Use this setup* saves a setup that kept running; “kept
running” means it did not crash — check it looked right. Trials can be stopped any time.

**The assistant builds its own fixes** (`ezgd/library/fixgen.py`) from the evidence and the symptom you pick
under *What happens?* (doesn't start · crashes after a while · black screen · no sound · runs badly · controller
doesn't work · can't play online): forcing the
discrete GPU on hybrid-graphics PCs (`DXVK_FILTER_DEVICE_NAME`, `VKD3D_FILTER_DEVICE_NAME`, PRIME offload),
ignoring an injected DLL in the game folder that crashed (ReShade `dxgi.dll`, mod loaders → Wine's own DLL via
`WINEDLLOVERRIDES`, nothing deleted), turning NVAPI off when it errors, engine renderer switches (Unity
`-force-vulkan/-force-d3d11/-force-glcore`, Unreal `-dx11/-vulkan/-dx12`, Godot `--rendering-driver …`, skipping
what is already in effect), windowed start for black screens, turning off Unity's native graphics jobs in
`boot.config` (edited with a backup), a larger PipeWire buffer or the game's own XAudio2 for sound, and forcing
large-address-aware for 32-bit games that run out of memory. Each shows its exact change; none is applied until
*Try fixes for me* has tested it (config edits are made on the trial copy or undone right after). When you keep a
generated fix it is **remembered** (engine + problem → fix) and tried first for similar games, labelled with the
game it worked for.

**Fix my game** (top of the Diagnose window) is the one-button path (a change replaces the game's own setup only
when a test shows it doing better — past start-up, drawing frames, further along; an unknown picture is not better
than a black one: 0.6.3 swapped Wildlands' working GE-Proton for another Proton on such a result): it checks everything, repairs on its
own only what has strong evidence and is reversible (damaged files, Steam-emulator setup, unapplied fix archives
and folders, the release's launch script / the real exe, missing runtimes — installed and waited for, OnlineFix
DLLs), then tests setups in throw-away prefixes, keeps the first that ran (preferring ones that drew frames) and
answers in one line: *Fixed — kept running with …* or *Not fixed: …because…*. **Did it work?** Yes records the
outcome; No undoes the kept setup (file repairs stay) and shows the other options. The window shows your real
fix rate ("fixed for you: 4 of 5 games"). It never resets a prefix, runs admin commands or downloads on its own —
those stay as buttons (when nothing worked and a newer GE-Proton exists: *Install GE-Proton… and try again*). When no single setup works, it also tests up to three **combinations** of the ones that got furthest (e.g. a different Proton *and* DirectX 11 — some games need both); contradicting pairs (two Proton versions, the same variable or DLL set two ways) are skipped.

**It follows the error, like a person debugging it** (`ezgd/library/tracer.py`). Instead of working through a list,
Fix my game starts the game once with Wine's detailed logging and reads that run: which process is the game, what it
loaded and from where, which of the game's own DLLs Wine swapped for its built-in ones (a mod loader, OnlineFix's
loader), how it ended (the error message, or the crash — the module, the offset, a read of address 0), what it did
just before, how far it got. From the end backwards it asks *why*, and writes the chain down: *crashed reading
address 0 in the game → right after Steam's client loaded → the game's own winmm.dll and dwmapi.dll were replaced by
Wine's → they are OnlineFix's and UE4SS's loaders, so OnlineFix64.dll and UE4SS.dll never loaded*. The suspect it ends
on is tested in a throw-away copy, with a prediction of what the test must show if it is right (*both DLLs load from
the game's folder, OnlineFix64.dll and UE4SS.dll load*). If the prediction comes true or the game gets further, the
change is kept and the **next** error is followed the same way (Expedition 33: loader → OnlineFix's one-time notice →
main menu); if nothing changes, the next suspect is tested. Only when the trail goes cold does it fall back to testing
setups. What it kept is saved for real only after the game has run twice; the reasoning of every step is shown in the
Diagnose window and in the answer.

What it draws on, strongest first:
* **Its memory of solved failures** (`crash_memory.json` in the data folder): every failure it solves is stored as a
  fingerprint — where it crashed (module, offset, the game build), which loader DLLs were replaced, the error
  message, missing DLLs or registry keys, the engine — with the fix. The same failure in any game is tried with that
  fix first ("the very same crash", "the game's own dwmapi.dll replaced by Wine's, as in Expedition 33"). Changes
  that did not help a game are remembered too and not tested on it again. A switch of Proton — the most common fix
  by far — is kept as *a Proton of that kind* (GE-Proton, Proton-CachyOS, Valve's), so another game, or another
  computer, uses its own newest build of that kind (0.6.7; before, it only ever helped the same game). A run that
  just kept going leaves no fingerprint and is not remembered (a "fix" saved on such a run was wrong), and *Did it
  work? No* takes back what that Fix my game remembered.
* **The last run that worked** (`investigations/good-<id>.json`): what that run loaded — the game's own DLLs, OnlineFix,
  UE4SS, Steam's DLLs — and which Proton build it used. A failing run is compared with it ("then it loaded its own
  winmm.dll, now Wine's"; "it worked with GE-Proton11-5") — and when "Proton-GE Latest" was updated in place, the build
  that worked is offered back if it is still installed under its own name. Files that were missing in the run that
  worked are never blamed.
* **The run itself** (above).
* **A closer look** when the trail goes cold: one more run recording every file and registry key the game asks for.
  What did not exist is worked out from the test copy afterwards (case-insensitively) and only that is kept — "just
  before it crashed it looked for `Config\Engine.ini` and `HKLM\Software\Vendor\Game`", with the files every big
  game probes (caches, locale data, crash reporters, GPU vendor probes) left out. A missing key that a `.reg` file of
  the release creates becomes the next test (imported into the copy; into the real prefix once it worked). (Wine's own
  "file not found" warnings cannot be used: with GE-Proton they pile up unterminated until Wine aborts.)
* **Games that just quit** — no crash, no error, often exit code 0 or 1 — were the weakest case. Now: every log file the
  game wrote during the run, whatever its engine (Source's console.log, Godot's, Ren'Py's traceback.txt, CryEngine's
  Game.log, Unity's Player.log, a launcher's error.log …, in its folder and its Windows user folders; text files only,
  installers' logs left out) is read, and its last error-like lines are quoted when the run failed (lines a run that
  worked also had are not); what Steam's API said when it refused ("[S_API FAIL] SteamAPI_Init() failed", "No appID
  found", "Steam must be running"); and in the closer look, **who ended the game** — Wine traces just the calls that
  end a process or start another (`ExitProcess`, `TerminateProcess`, `CreateProcess`, `ShellExecute`), in the test
  copy only, with the address they came from: "steam_api64.dll ended the game — it called ExitProcess(1): Steam's
  API ends a game when Steam is not running for it…", "OnlineFix64.dll ended it", "Game.exe tried to start
  …\Real-Win64-Shipping.exe, and that failed — it is a launcher" (then *Start Real-Win64-Shipping.exe directly* is
  tested). Verified on Expedition 33: its launcher's `CreateProcessW` of the Unreal executable, the game still at its
  menu under the trace.
* **Patience**: a test still starting up after 45 s (shader compiling) gets one 120 s run before it counts as stuck.
* **Games that quit at random** (0.6.7): when the game's recent starts quit by themselves later than a test lasts —
  Ghost Recon Wildlands quit anywhere from 5 s to 4 minutes in — every test of that Fix my game (and *Try fixes for
  me*) watches it past the latest of those times (one and a half times it, at most 8 minutes), and still ends as
  soon as the game fails. The current setup then usually fails in the test too, and that run is followed; a fix has
  to survive the long watch twice. If the current setup survives both, the answer says the tests were lucky rather
  than calling the launch setup fine.
* **How far it got**, without the game's own log (0.6.7): the graphics API the game loaded (`d3d11`, `d3d12`,
  `dxgi`, Vulkan), DXVK's own log (Proton switches it off; test runs switch it back on, into the run's log only —
  no file in the game folder; and DXVK writes "swapchain", which the old pattern "swap chain" never matched), the
  game's window appearing in KWin, and frames (its picture moving or showing a menu, or MangoHud counting frames).
  Every report used to end at "Proton started".

A fix that needed several changes is **trimmed**: each change is left out once, and kept only if the game fails without
it. Every investigation is written to `reports/investigation-<game>-<time>.txt` (the Diagnose window's *Open the
report*), so the reasoning can be read later.

**Test starts out of sight** (KDE Plasma, `ezgd/platform/hidden.py`; *Settings → Library*): a KWin script loaded
before a test takes every Proton window that appears (`steam_app_<id>`, `steam_proton`) — transparent first, off the
taskbar, Alt+Tab and pager, out of full screen — and moves it beyond every monitor, where it is opaque again so the
game keeps drawing at full speed (Expedition 33 reached its menu as fast as when shown). If the game moves itself back,
it is made transparent at once and moved away again; if it takes the focus, the window you were using gets it back.
Its sound is muted (its streams, found by process). A window that is not the test's is put back. Nothing to install:
KWin, journald, busctl and pactl are part of the desktop; elsewhere tests are shown as before.

**Judged by its picture** (`ezgd/library/picture.py`): the hidden window is photographed (KWin's ScreenShot2
`CaptureWindow`, through busctl) a few times per test; each picture is checked for black, frozen (no change between
looks), a menu (Tesseract reads the text — dim text is stretched to full contrast first: Expedition 33's grey
"PRESS ANY BUTTON" on black is read) or an error the game drew itself. "Kept running 45 s" no longer passes a black
screen or a frozen loading screen; a menu counts as having reached the game.

**Frames counted, not only photographed** (0.6.7): every test run also logs its frames with an invisible MangoHud
(already loaded into every game the app starts). A window that is photographed black while frames are being drawn
(Ghost Recon Wildlands' always was) is no longer judged a black screen; frames that stop while the game keeps running
are judged a freeze; a DirectX/Vulkan game that presented no frame at all drew nothing. MangoHud only sees Vulkan
(DXVK, vkd3d-proton): for OpenGL / WineD3D games it says nothing. Should MangoHud itself crash a game in a test, the
test is repeated without it and the setup is not blamed.

**Crashes and freezes while playing** (`ezgd/library/crashes.py`): a test start lasts 45 seconds; a crash 20 minutes
in only happens while someone plays. Every start records which DLLs were loaded from where (`+loaddll`, a few hundred
lines), and when a session ends in a crash the app keeps, in `crashes/<id>/<time>/`, the session's log, the game's
crash dumps (Unreal, Unity, Sentry's `.sentry-native`, Crashpad, Windows' CrashDumps — read by mapping, not loading:
Sentry writes 1 GB dumps), its engine log and the traced cause, and says so. A game that uses no processor time for a
minute is probably frozen: what each of its threads is doing is read from Linux itself (`/proc/<pid>/task`: name,
state, the system call it waits in and on what — Proton's `ntsync` waits, the GPU driver, the disk) and summed up (a
deadlock, an endless loop, a hang in the graphics driver, a disk stuck in the kernel). *It froze — stop it and keep
what happened* (Library ⋯ menu, and the quick menu) does the same on request. *Fix my game* then starts from the real
crash: each idea is checked with a hidden start (the game must still start as well as before), the first is applied
**on trial**, and your next sessions judge it — played well past the old crash time: kept (and remembered); the same
crash: undone and the next idea set; quit early: not judged yet.

**What the system saw, and the problem itself** (`ezgd/library/syslog.py`, `probes.py`, `netprobe.py`). Some failures
never reach the game's log, so the assistant also reads:

* **The system journal** for each test run and play session: a game the kernel (or systemd-oomd) killed because
  memory ran out, a GPU fault under it (NVIDIA *Xid*, amdgpu ring timeouts, i915 hangs), a crash Wine could not catch,
  a core dump. A game killed for memory used to look like a normal quit (the same SIGKILL as stopping it); now it is
  kept as a crash, with what to do (swap / zram when there is none, fewer programs open). A GPU fault on the game's
  work is followed with tests that must make the fault disappear (DirectX 11, another DXVK / VKD3D-Proton, NVAPI off);
  a hardware fault (*the GPU has fallen off the bus*) is said as such. Where the journal cannot be read, Diagnose says
  so and offers the command that grants access — never "nothing found".
* **The problem you reported, while the test runs.** *No sound*: did the game open an audio stream in PipeWire, did it
  play, into which output (muted, or not your default — that is the sound setup, and no game fix is tested for it)?
  *Controller*: which of the connected controllers the game opened — none, or the same one twice (its raw HID and its
  event device, or the pad and Steam Input's virtual one). *Runs badly*: MangoHud's frame times (1 % lows, hitches,
  stutter that fades = shaders being built, CPU, GPU, swap); a fix is kept only when it is measurably smoother than
  the current setup, and the next real play session can be measured (the overlay stays invisible). A test that keeps
  running with the problem still there is no longer called *fixed*.
* **Online play** (*Can't play online*): the ports the game listens on while you play, and the firewall's blocks of
  exactly those ports during the session (UFW / netfilter log lines; the rule that lets it through is offered as a
  command, never applied), Wine's TLS errors, and Steam not running for an OnlineFix release.

**New games get a quick check before their first start** (about 30 seconds): after a game is installed — as soon as
no game is running (a game you start pauses it) — common problems are looked for without starting it (the game's own
loader DLLs Wine would replace, a Proton without a DLL the game imports, the wrong executable) and strong, easily undone
fixes are set; then it is started once, out of sight and muted, for at most 25 seconds (done at once when its picture
shows its menu, or when it fails). If that start fails, one quick read of it may name a common cause that is set
right away; anything deeper is left to *Fix my game*. The Library shows *Checked: ready to play*, *Fixed …* or
*Its first start failed — Fix my game*. (Expedition 33: 28 s, and its OnlineFix loader was found before the start.)

**Built shaders are kept** (`LibraryManager.shader_cache_env`): every game started by the app gets its own GPU-driver
shader cache in the app's data folder (`shadercache/game-<id>`), 20 GB large and never cleaned up by the driver —
NVIDIA's `__GL_SHADER_DISK_CACHE_*`, Mesa's `MESA_SHADER_CACHE_*` (AMD, Intel), as Steam does for its own games. Before,
all of them shared the driver's one cache (755 MB on the developer's PC), and its size limit made big games push
each other's shaders out and build them again on every start. A game's own caches (Unreal's pipeline caches …) live
in its prefix and move with its saves when it gets another prefix. Storage shows each game's built shaders; uninstall
removes them; caches of games no longer in the Library are offered for cleanup. (Expedition 33: 49 MB after its first
start; the second start was at its menu in 40 s.)

**Knowledge between computers**: every solved failure keeps the computer it happened on (`ezgd/platform/machine.py`:
distro, kernel, desktop, Wayland/X11, GPU vendor, model and driver, CPU model and instruction sets, RAM,
`vm.max_map_count`, audio server; the machine id hashed). A fix from a different kind of computer counts for less and
says so ("solved in X on another computer (NVIDIA vs AMD graphics)"); a crash inside one GPU vendor's driver DLL
(nvapi64, amdxc64 …) is never carried to another vendor's card. Each computer
shares what it solved with everyone who uses the app (below).

**Everyone's knowledge** (`ezgd/library/crowd.py`, `server/knowledge/`). When a game is fixed, what fixed it is sent to
the app's server — a Cloudflare Worker with a D1 database — and every copy of the app fetches what everyone solved
(every 12 hours) and tries those fixes first ("solved in games on 3 other computers"). Settings → *Share what fixed
which crash with everyone* switches it off (then nothing is sent or used).

* **What is sent:** the kind of failure without the game — exception code, the module it was in (`<game>` for the
  game's own), which DLLs were replaced or missing, the error-box category — the fix as rebuilt from the allow-list
  below (free text that could name a game or folder left out) and the GPU vendor. Not sent: game titles, the game's
  build, registry keys, paths, machine ids, hardware models, the exact setup of a game, anything typed.
* **Sealed with a new random key for every report:** an X25519 key made for that one report, agreed with the server's
  public key (HKDF-SHA256 → AES-256-GCM). Only the server can open it — not the network, a proxy or other users.
* **The server** checks every field against a strict schema, keeps no IP addresses (a salted hash of the network,
  IPv4 /24, counts confirmations from different places and limits each network to 40 reports an hour), and publishes
  failure pattern → fix → on how many networks it worked (top 5 fixes per pattern), **signed with Ed25519**. The app
  uses nothing whose signature does not verify against the key it carries (`ezgd/core/crowd_keys.py`) and checks it
  again whenever it reads it.
* **Nobody can make your computer run anything through it**, not even with a poisoned report the server accepted:
  whatever arrives is rebuilt from an allow-list before it is used (`tracer.clean_record`) — no launch-option lines,
  no file edits, no per-game setups, game arguments without shell characters, only known Proton options and Fixes
  switches, DLL overrides with plain names, winetricks verb names, and environment variables from a list of
  graphics/Proton/audio settings, never ones that load code or name files (`LD_PRELOAD`, `*PATH*`, Vulkan ICDs/layers
  …). The worst a forged fix can do is be tried, uselessly, in a test copy of a game. A fix confirmed in more places
  counts for more.

What the app can read, anyone who has the app can read: the published knowledge is public by design — which is why it
holds nothing about anyone. The reports themselves only the server can read.

**Running the server** (free tier: 100,000 reports a day): `cd server/knowledge && npm install`, `npx wrangler login`,
`npx wrangler d1 create ezgd-knowledge` (put the printed id into `wrangler.toml`), `../../.venv/bin/python make_keys.py`
(writes the public keys into `ezgd/core/crowd_keys.py`; the private ones go to
`~/.local/share/ezgd-crowd-keys/secrets.json` — back it up), `../../.venv/bin/python make_keys.py --put` (hands them to
Cloudflare as secrets), `npx wrangler deploy`, then put the worker's URL into `SERVER` in `ezgd/core/crowd_keys.py` and
release. `npm run dev` runs it locally (test keys in `.dev.vars`, never the real ones).

**Other DXVK / VKD3D-Proton versions** (`ezgd/library/dxlayers.py`): when the crash is inside the DirectX translation
layer (d3d11/dxgi → DXVK, d3d12/d3d12core → VKD3D-Proton), versions around the bundled one are tested. Proton copies its
own layer into the prefix on every start, so the app makes a **Proton variant** instead — a folder that mirrors the
game's Proton through links with only that layer swapped (verified against GitHub's SHA-256), e.g. *Proton-GE Latest +
VKD3D-Proton 3.0.1*; it shares the game's prefix and leaves the real Proton untouched.

**Release repair.** *Integrity*: every archive the game was extracted from is compared with the installed files
(the archive's own file list and sizes; later fix archives win; settings/save files and files the release's own
script renamed are ignored); missing or damaged files are re-extracted — only those. *Steam emulators*: Goldberg,
CODEX/RUNE, ALI213, SmartSteamEmu, CreamAPI, OnlineFix and Valve's own API are recognised; a missing
`steam_appid.txt` (DRM-free releases restart through Steam without it), an empty/wrong App ID in the emulator's
config, or the emulator DLL not next to the exe that loads it are fixed (with backups). *Fix archives & folders*:
a release's *Fix Repair* / update / patch archive or *Crack*/*Fix*/*Update* folder that was not applied is applied
the way its instructions say, with a backup of every replaced file; for OnlineFix games that still fail, its
published *Fix Repair* can be downloaded (in-app) and applied.

**GE-Proton on demand**: the newest GE-Proton from its official GitHub releases (SHA-512 verified, safely
unpacked into `compatibilitytools.d`) — offered by *Fix my game* and in *Settings → Library*.

**ProtonDB players' fixes**: ProtonDB's open report data (≈70 MB, ODbL, refreshed monthly; *Settings → Library*)
is indexed per Steam App ID (≈390 000 reports, 34 000 games, < 1 MB index). The launch options players used when
they got a game working become "Players' fix" candidates — **only** options made of `NAME=value` settings, known
wrappers, `%command%` and game arguments; anything with shell syntax is dropped; overlays/logging/frame caps are
not counted as fixes. Data from ProtonDB.com.

**Steam App ID detection** (needed by protonfixes, ProtonDB fixes and `steam_appid.txt`): the release's own
files (steam_appid.txt, emulator configs; Spacewar 480 ignored; only when they agree), the catalog, ProtonDB's
title index (offline), then Steam's store search with title variants ("… Online" from OnlineFix, edition
suffixes, roman numerals) — accepted only when unambiguous.

**Rebuild prefix** moves the old prefix aside instead of deleting it; afterwards the assistant offers to copy your
saves and settings (AppData, Documents, Saved Games) into the new one.

### Multi-part downloads

Big games come in parts (`Game.part1.rar … part8.rar`, `part01`, `Game.7z.001 …`, `Game.zip.001`, or old-style
`Game.rar` + `Game.r00 …`). Whichever way the parts arrive — in-app (Pixeldrain), the in-app browser, or
*Import downloaded file…* — they are collected into **one** Library entry (“Multi-part download: 3 of 7 parts
downloaded”) and extracted from the first part only once every part is there. The part count comes from the
game page's file names; when a page does not list them, a set counts as complete when the parts are
contiguous and the last one is smaller than the first (volumes are fixed-size except the last). In-app part
sets get one **Download all N parts** button (options confirmed once). Split `.7z.001`/`.zip.001` sets are read
as one continuous file; RAR volumes are handled by UnRAR.

### Launch files (executable detection)

The Library page lists every launch file it finds and what it is: the release's own launch scripts
(`.cmd`/`.bat` that prepare files and then start the game, e.g. Black Ops II's `mp.cmd` / `zm.cmd` /
`sp.cmd`) rank first; modes are recognised from names (Multiplayer, Zombies, Campaign, Vulkan, DirectX
12/11, server, editor); exes named in the release's Readme or on the game page (cracked-games.org
“Run … to play”) get priority; language switchers, config tools, crash handlers and files in `_extras`
folders are marked *probably not the game*; 64-bit builds beat 32-bit duplicates. Files are only listed and
read as text — nothing runs until you press Play and confirm. *Use this* switches Play to that file.

### Fixes screen

Every game has a **Fixes** button right next to **Play** (also in the game's ⋯ menu and in
Properties → Compatibility). It lists every fix that can apply to that game as a checkbox, grouped
into *Recommended for this game*, Graphics, Audio, Stability, Performance & overlays, Compatibility
and Troubleshooting, each with a plain explanation. *Recommended* fixes come from checking the
game's files and its own logs (e.g. OnlineFix's `winmm.dll` → "Load the game's own DLLs";
Direct3D 12 errors in a Godot log → "Use Vulkan rendering"), and the button shows how many are
recommended but not yet on. Fixes are stored as switches separate from your launch options, so
unticking one undoes it completely; a live preview shows the exact command. Included: game DLL
overrides, Godot Vulkan/OpenGL renderer, all Proton options (WineD3D, esync/fsync, NVAPI, hide
NVIDIA, Wayland, large-address-aware, old GL string, Proton log, DXVK HUD), protonfixes, Steam Linux
Runtime, SteamAppId, audio buffer (`PULSE_LATENCY_MSEC=60`, affects only that game), GameMode and
MangoHud (offered once installed).

### Playing games: Proton, fixes and launch options

Open a game in **Library** and press **Play**. The ⚙ button opens Steam-style **Properties**.

**General → Launch options** use Steam's syntax:

| You type | What runs |
|---|---|
| `-windowed -dx11` | `game.exe -windowed -dx11` (appended, like Steam) |
| `PROTON_LOG=1 %command%` | the game with `PROTON_LOG=1` set |
| `MANGOHUD=1 gamemoderun %command% -nolauncher` | `gamemoderun <proton chain> game.exe -nolauncher` with `MANGOHUD=1` |
| `gamescope -W 1920 -H 1080 -- %command%` | the game inside gamescope |
| `systemctl --user restart pipewire wireplumber && %command%` | restarts PipeWire, then starts the game only if that worked |
| `systemctl --user restart pipewire wireplumber ; %command%` | restarts PipeWire, then starts the game either way |
| `%command% ; notify-send "Game closed"` | runs a command after the game exits (`&&` = only if it exited cleanly) |

This is **exactly Steam's behaviour on Linux**: `%command%` is replaced by the full, shell-quoted
command (Steam Linux Runtime + Proton + game, or umu-run / Wine / the native binary), options
without `%command%` are appended after it, and the resulting line is run by `/bin/sh -c`. Anything
that works in Steam works here — `&&`, `;`, `||`, pipes, redirections, `$(…)`, `bash -c '…' -- %command%`.
The app's own settings (prefix, Proton options, DLL overrides, protonfixes id) are passed as
environment variables, so `VAR=… %command%` in the options overrides them just like in Steam.
The exact line is previewed live (with a warning if a named program isn't installed), syntax errors
are rejected before saving, and the line is shown again in the launch confirmation. Output goes to
the game log. As in Steam, launch options can run any command — only use ones you understand.
On Windows the options are appended to the game's command line, as Steam does there.

**Compatibility** (Linux):
* **How it launches** — the same chain Steam uses:
  `SteamLinuxRuntime_*/_v2-entry-point --verb=waitforexitandrun -- <Proton>/proton waitforexitandrun game.exe`.
  The runtime is whichever one the Proton build's `toolmanifest.vdf` requires, found through
  Steam's `appmanifest_<id>.acf` (on the dev machine: Steam Linux Runtime 4.0). Each game gets
  its own prefix in the app's data folder. If the runtime is not installed, Proton runs directly.
* **Per-game fixes** — GE-Proton and Proton-CachyOS include *protonfixes*. The app passes the
  game's real **Steam App ID** as `UMU_ID`, which selects that game's fix (the dialog tells you
  whether your Proton has one). The ID is found automatically from the SteamRIP page, the same
  game on another source, or Steam's store search (exact title matches only); you can type it too.
  Without an ID, `UMU_ID=umu-0` is set so protonfixes never guesses an ID from the prefix path.
* **Games that need the Steam client (OnlineFix)** — `UMU_ID` also tells GE-Proton "not a Steam game", and since
  **GE-Proton11-7** its `proton` script then switches Wine's bridge to the Steam client off (`lsteamclient=d`) unless
  `UMU_USE_STEAM=1`. OnlineFix's fix needs that bridge: Risk of Rain 2 and Slay the Spire 2 failed with GE 11-7 in the
  app ("Failed to load original steamclient") while they worked with GE in Steam (which never sets `UMU_ID`) and with
  Valve's Proton. Up to GE-Proton11-6 (and Proton-CachyOS 11.0) the switch worked the other way round, so 11-5 was
  fine. The app reads which way the chosen build works from its own `proton` script and, for OnlineFix games, sets
  `UMU_USE_STEAM=1` only where that keeps the bridge on (unknown builds: no `UMU_ID` at all, as Steam starts them).
  Checked with the real GE-Proton11-7: before, `umu.exe` and `lsteamclient=d`; now `steam.exe`, the bridge on. The
  "Runs with" line says *Steam client on*.
  Exporting `SteamAppId` (Valve Proton's per-app tweaks) is optional and off by default, because
  games whose Steam emulation reads it — such as OnlineFix's multiplayer fix — can break.
* **Proton options** — WineD3D, disable esync/fsync, NVAPI, hide NVIDIA, native Wayland,
  large-address-aware, old GL string, Proton log, DXVK FPS HUD (Proton's documented env vars).
* **DLL overrides** — *Detect* finds DLLs the game ships that Wine would replace with its own
  built-ins (winmm, winhttp, version, dinput8, …) and suggests `name=n,b` overrides; it also
  notes when the OnlineFix fix is present. OnlineFix publishes no Linux instructions, so this is
  a suggestion, not a guarantee.
* **Prefix tools** — open prefix, winecfg, winetricks, game log, reset prefix.

Playtime is recorded when the game process exits; **Stop** ends the game and everything it
started. Not every Windows game works under Proton (anti-cheat, some launchers).

## Security model

* **URLs**: only `http(s)`, no embedded credentials, no local/private/link-local/
  CGNAT/reserved addresses (incl. IPv4-mapped IPv6 and numeric host tricks).
  DNS is resolved by a custom network backend that refuses non-public answers and
  connects to the vetted IP itself (defeats DNS rebinding); TLS still verifies the
  real host name. Redirects are followed manually: every hop is validated, max 5,
  no HTTPS→HTTP downgrades, `Referer` dropped across hosts. Proxy environment
  variables are ignored. Responses are size-capped.
* **Sources**: adapters only fetch their own domains; HTML/JSON is parsed
  defensively; a changed layout produces a clear *layout changed* status instead of a crash.
* **Files**: remote file names are normalised (no paths, reserved names, control
  characters); downloads never overwrite existing files; archives are validated
  before extraction (absolute paths, `..`, drive letters, `:` streams, escaping
  symlinks), links/devices are never extracted, the staging tree is audited after
  extraction; nested archives are not auto-extracted.
* **Deletion**: only the exact content of a download inside its destination, or
  library folders inside your configured download/install folders, can be deleted —
  always after explicit confirmation.
* **Execution**: nothing downloaded is executed automatically; launching needs
  confirmation; marking a Linux binary executable needs separate consent; no system
  security settings are changed.
* **Privacy/logs**: no cookies, accounts or passwords are stored. The one exception you choose yourself: a Steam Web API key (Steam friends) — stored in the local settings database, sent only to api.steampowered.com, kept out of the logs. Logs strip
  query strings, URL credentials and magnet trackers. Problem reports are local text files.
* **Archive tools**: 7-Zip and UnRAR get `--` before the archive, so a member named like a switch (`-spf`, `@list`)
  stays a name when a repair passes member names to them.
* **App updates**: installed only with a valid Ed25519 signature from the developer's release key (see *App updates*)
  — a stolen GitHub account cannot ship an update.
* **Other people's input**: everyone's crash knowledge is signed by the server and rebuilt from an allow-list (no
  launch lines, file edits or code-loading variables — see *Everyone's knowledge*); ProtonDB launch lines are reduced
  to safe parts and lose code-loading variables (`LD_PRELOAD`, `*PATH*`, Vulkan ICDs/layers). Steam chat invites only
  offer *Join* when their connect string is a plain `+connect` / `+connect_lobby` / `-join` form (a friend's
  message cannot start a game with `+exec` or `-game`), friends' server addresses must be addresses, and only Steam's
  join, chat and profile links are ever handed to Steam (a Remote Play invite's session part only as plain tokens).
* **The synced save folder** is not trusted (anyone with access to it, and sync tools carry symlinks): nothing is
  read or written through a link, writes go to a fresh random name first, sizes are capped, and each computer marks
  its own backups with a key only it has — only those are ever pruned, so fake "newer" backups cannot get real ones
  deleted. Restores stay inside the game's prefix (`users/…`) and refuse zip bombs.
* **Local**: the app's single-instance socket only accepts its own user and only ever means "show the window". The
  in-app browser keeps Chromium's sandbox (seccomp + its own user namespace — checked), denies camera, location and
  notifications, and blocks program downloads.
* **What remains**: the games themselves run as you, with your files visible through Wine's `Z:` drive — a malicious
  release is the biggest risk, as with any launcher; download from sources you trust. *Our overlay* switches on
  Steam's debugging port (127.0.0.1:8080; web pages cannot use it — Steam checks the Host header and the session
  address is secret — but any program on the computer, including a game, could control Steam through it): switch it
  off when you do not use it (Settings → Library & launching), then restart Steam.
* **Database**: WAL mode, transactions for every write, integrity check on start
  (a damaged file is moved aside and a fresh one created), backup before every
  migration and on exit, manual backup/restore with validation.

## Architecture

```
ezgd/
  app.py              entry point (CLI flags, start-up checks)
  core/               settings, logging, file-system helpers, AppCore (wires everything)
  net/                SSRF-safe HTTP client, URL validation, rate limiting, TLS roots
  database/           SQLite connection, versioned migrations, repositories
  catalog/            normalized Game / DownloadMethod models, normalisation, search & dedup
  sources/            adapter interface, OnlineFix, SteamRIP, cracked-games.org, file hosts, cache, registry, reports
  downloads/          task model, HTTP engine, libtorrent engine, queue manager
  installation/       safe extraction, post-download pipeline
  library/            library entries, executable discovery, launching, Steam integration
  platform/           OS paths, launcher (native/Proton/Wine/Steam), Steam & VDF
  ui/                 Qt bridge (core runs on its own asyncio thread), theme, icons, pages, dialogs, widgets
tests/                unit, integration, UI and live tests + trimmed site fixtures
packaging/            PyInstaller spec, build scripts, desktop entry
```

The core (network, downloads, database) runs on a dedicated asyncio thread; the Qt
GUI talks to it through `ui/bridge.py`, so network or disk work never blocks the
interface (verified by `tests/test_ui.py`).

Adding a source: implement `SourceAdapter` (`latest`, `search`, `details`,
`resolve`, optionally `featured`/`genres`/`expand`) returning normalized `Game`
records, register it in `ezgd/sources/__init__.py`, and add fixture-based tests.

## Tests

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest            # offline suite (~40 s)
.venv/bin/python -m pytest -m live                                # live site checks (network)
```
Torrent tests run a real local swarm (two libtorrent sessions on 127.0.0.1);
HTTP tests use a scriptable local server (drops, 5xx, range quirks, redirects).
See **TEST_REPORT.md** for the results of the last run.

## Troubleshooting

| Symptom | What to do |
|---|---|
| *Source unavailable* banner | The site is down or blocked on your network; cached results are shown. *Retry* later. A site whose address could not be found or that refused the connection is tried twice more first (after 1 s and 3 s; 0.6.8) — a moment's DNS hiccup no longer needs *Try again* — and the message says so in words ("… could not be found", "could not connect to …"). |
| *Layout changed* | The site's HTML changed. Use *Report a problem* (saves a local report in the data folder) and update the app. |
| `CERTIFICATE_VERIFY_FAILED` for a source | Your system clock may be wrong, or a network middlebox intercepts TLS. The app never disables verification. |
| Torrent stays at *Fetching metadata* / *Stalled* | Nobody is sharing it right now, or your firewall blocks the listen port (Settings → Torrents; try enabling UPnP or forwarding the port). |
| “Access denied (HTTP 403)” / “web page instead of a file” | The link expired or needs the browser; open it from the game page and use the final link. |
| RAR extraction: “needs UnRAR or 7-Zip” | Install `unrar` / `7zip` (Linux) or 7-Zip / WinRAR (Windows), or set the tool path. |
| Windows game won't start on Linux | Install Proton (Steam → Tools) or GE-Proton, choose it in the game's *Proton version*; some games simply do not work under Proton (anti-cheat, launchers). |
| Database problems | Settings → Database → *Restore from backup…* (automatic backups are taken before migrations and at exit). |
| Debugging | Settings → *Log level: Debug*, then *Open log folder*; *Recent errors…* lists the stored error summaries. |
| Lag, a frozen window, the controller | Send `~/.local/state/EZGameDownloader/logs/ezgd.log` (Windows: `%LOCALAPPDATA%\EZGameDownloader\logs\ezgd.log`). Without changing any setting it records: every **UI stall** over a quarter of a second ("UI stall of … ms on the downloads page — stuck in: …", the page or couch tab and the line it was stuck in), every visit to the **Downloads** page / couch tab (how many downloads, how long until drawn), couch mode's frame times, every **controller layout** Steam chose while a game ran and why ("Steam switched the controller to the … layout"), Shift+Tab and quick-menu joins and invites. |

## Limitations and incomplete features

* **Windows was not tested** in this environment (Linux only). The code paths are
  written for Windows, but expect rough edges until it is run there.
* **SteamRIP downloads cannot be automated**: all observed hosts require a browser
  (by design the app does not bypass Cloudflare, captchas or token requirements).
* **OnlineFix server/Drive downloads** require a site login/API key → browser only.
* **cracked-games.org downloads** use a signed browser form → browser only, then *Import downloaded file…*.
  Some games publish no checksum (e.g. ELDEN RING NIGHTREIGN); those are imported unverified, and the app says so.
* The **download-method filter** only knows methods for games whose page was opened
  (listings do not show methods; fetching every game page would hammer the sites).
* **RAR** needs an external UnRAR/7-Zip binary (no permissively licensed pure-Python RAR decoder exists).
  RAR extraction progress is estimated from bytes written.
* **Add to Steam** writes the shortcut only; choosing the Proton version for it is done
  in Steam (*Properties → Compatibility*). Steam must be closed while adding.
* **Proton launches** run outside Steam (Steam Linux Runtime + `proton waitforexitandrun`, one prefix per game);
  titles needing Steamworks may require launching via the Steam shortcut.
* Theme changes recolour most widgets live; a restart applies them everywhere.
* No `.msi`/AppImage installer — PyInstaller folders/zip/tarball only (Linux builds update themselves from GitHub
  releases; see *App updates*).

## Still to confirm

The to-do list from a tester's log (KDE Plasma on Wayland, Steam Controller) is done; what only someone at
that PC can check:

* **Risk of Rain 2 and Slay the Spire 2 with GE-Proton on the tester's PC** (GE-Proton11-7, OnlineFix). Switch them
  back to GE-Proton (Slay the Spire 2's later launches in the log use Proton - Experimental), play online once, and check the
  game's "Runs with" line / log says *Steam client on (UMU_USE_STEAM)*.

* **The tester's log after this release** — that full screen, borderless and custom size fill the screen, and that
  the desktop window comes back from couch mode with its title bar. KWin 6.8 rejects
  `workspace.clientArea(KWin.FullScreenArea, output)` (TypeError), which is why the full-screen / borderless scripts
  did nothing there; they now measure the monitor with `output.geometry`. Checked on a real KWin 6.8.0: fill the
  screen (2560×1440), force full screen, custom 1920×1080 real pixels at 150 % (1280×720), and the desktop window
  restored from "no border" 1711×989 to 1711×961 with its title bar (KWin adds the title bar after the script, so the
  size is asked for once more 0.3 s later). The desktop window is now put back after *any* size change.
  0.5.6's log showed one more step: full screen filled the monitor, then about a second later couch mode got the
  desktop window's size again (1711×989).

  *Found and fixed after 0.6.10 (no more filling it again):* that was never KWin — KWin's source has nothing that
  resizes a window as it appears or is activated except window rules (applied before the first full-screen answer)
  and session restore (needs an id the app never sets). It is a window-memory KWin script: such scripts save an
  app's windows when the app's last window closes and give that place to the app's next window. "Remember Window
  Positions" (rxappdev; its code read) does it at once and again a second later — exactly the tester's log
  (+0.03 s and +1.03 s after showing; the desktop window coming back with couch mode's size, then the app putting it
  back, then the script again a second later). Couch mode hid the desktop window as it opened, so couch mode was the
  app's "next window". Couch mode, its quick menu and its browser now have their own app id (`ezgd-couch-<monitor>`,
  `ezgd-quick-menu-<monitor>`, `ezgd-couch-browser-<monitor>`; checked on KWin 6.8.1: KWin sees each window under
  its own id): such a script only gives them their own last place, on the same monitor. Couch mode closed while
  minimized is un-minimized unseen first, so it is never remembered minimized. A full screen the desktop makes
  smaller is no longer filled again (that fought the script, choppy) — only logged. The couch display trace now
  lists the KWin scripts switched on (kwinrc), so the tester's next log says which one it is.

* **The Steam Controller after the quick menu (0.6.4)** — in an OnlineFix game (Expedition 33, Risk of Rain 2), in
  Dark Souls III and in Bloodborne, with *Our overlay* on and off: hold the Steam button, close the menu, play on.
  Steam's rules behind it were measured on Steam itself (a forced layout survives focus changes, the Steam button and
  Big Picture; `forceinputappid/0` switches nothing until the focus moves), but not with a Steam Controller — the log
  says what Steam chose at every step. **Reported broken on 0.6.7** (2026-10-28): after opening and closing the quick
  menu the game takes up/down but not A, the mouse pointer shows over the menu, and with *Our overlay* off the
  controller does nothing in the game at all. Up/down on the stick, nothing on A, B, X, Y and a mouse pointer is
  exactly what Steam's own desktop layout for the Steam Controller sends (stick = arrow keys, A Enter, B Space, X/Y
  Page Up/Down, right pad = mouse), which points at Steam's desktop layout while the game had the screen (unconfirmed:
  the tester uses a saved desktop layout of their own); Steam picks the layout by which X11 window has the focus.
  Found in the code since, and fixed in 0.6.9: for a game started from the desktop, the request that gives
  the game the focus back after the menu was dropped whenever listing the game's processes took longer than 400 ms; a
  controller Steam takes away (its virtual pad goes whenever Steam switches layouts) left anything held at that moment
  stuck (a stick kept scrolling, a held guide button opened the menu by itself, a held A blocked A on the next pad);
  the menu never hid the mouse pointer. A second pass found more: which desktop layout the player uses was looked for
  only in the last 256 KB of Steam's controller logs (past that, the player's own layout silently became Steam's
  default, and with it what each button means); with Steam closed, the keyboard's Space, Tab and Page Down still
  counted as a Steam Controller's B, ✕ and Manage; Flatpak's and Snap's Steam never counted as running (no Steam
  button, no Steam keys); a held d-pad repeated twice as fast on pads that repeat keys themselves; with two games
  running, a quick menu opened from the desktop picked either one. Whether the first of these is what happened on the
  tester's PC is not known yet. The log now says, each time the menu hands a game the screen back, whether KWin was
  asked to bring its window forward and which X11 window had the focus 0.7 s later, next to Steam's own choices
  ("Steam controller layout: …"). **The cause, measured on Steam (2026-10-28, a controller, the real quick menu):**
  Steam gives a controller the layout of the focused X11 window's game (its process: a game Steam started, or one
  carrying SteamGameId / signed in like OnlineFix). The quick menu is a Wayland window, so the moment it took the
  focus Steam switched to its desktop layout, which removed the virtual pad the game reads from, and made a new one
  when the game had the focus again — every opening and closing, four times in four tries; a game that does not take a
  re-plugged pad back loses the controller. Forcing the layout Steam already uses keeps the pad (same device), also
  through focus changes. So the menu now forces the game's own layout (Steam's last decision for it) before it takes
  the focus and releases it a second after it went: in the same test the pad — and the game's open handle on it,
  checked in a real Proton game — stayed for all four openings, and afterwards Steam followed the focus as usual. Only
  when there is a virtual pad to keep (a layout that sends keys keeps the old behaviour). Found on the way: a second
  press right after closing the menu of a desktop start was lost (the hidden couch mode closed under the opening
  menu). The earlier note that Steam makes no virtual pad without a game process was wrong: it was a layout that sends
  no pad at all.
  **The deeper cause (2026-10-28, after 0.6.9):** that layout is Spacewar's own — Valve's Steam Input API layout for
  Spacewar, which Steam picks by itself for anything running as 480. Every OnlineFix game (started by the app or by
  Steam) and every game the app asked Spacewar's layout for got no gamepad at all from a Steam Controller, unless
  Spacewar's layout had been changed in Steam. The app now asks for the game's own Steam layout with a Valve controller
  (see *Controllers and the screen while playing*), checks that Steam made a pad, and says what helps when it cannot.
  Also fixed: the quick menu over a game Steam started waited the full 1.5 s for Steam every time (Steam writes a
  shortcut's id as a negative number), menu presses also reached the game, *Without Steam Input* never counted, a game
  started before Steam got nothing once Steam ran, and handing the layout back after Steam had quit started Steam
  again. **Still to check with a real controller:** the game's own Steam layout in an OnlineFix game and in a repack
  with a Steam Controller (the game log's `ezgd:steam-input` line says whether Steam made a pad), and that Steam's
  overlay (Steam button tap) can still be moved through with the D-pad while a layout is asked for — a 2022 Special K
  report said Steam's overlay lost D-pad input after `forceinputappid`.

* **Pages of a site that is "not found" for a moment (0.6.8)** — a tester's cracked-games.org page said *could not
  resolve … Name or service not known* and loaded on the second *Try again*; the app now tries twice more by itself.

* **Joining from the quick menu (0.6.5)** — a friend opens a lobby in the game (hosts / creates a lobby), sends an
  invite from the quick menu (*Invite* now says when the game gave Steam nothing to invite to), the other presses
  *Join <name>*. An invite without a lobby no longer offers Join (it started Spacewar); whether Join lands in the
  session is then up to the game, exactly as with Steam's own Join button.

* **Download in app from a game's page (0.6.6)** — after the site's download starts and the browser closes, the game's
  page is in front and takes clicks (on KDE Wayland 0.6.5 left the main window in front, frozen; checked here on
  KWin 6.8.1, where KWin's window stack now shows the page on top and active).

* **Shift+Tab** over a game (KDE Plasma, *Our overlay* on) opens the quick menu and closes it again; Shift+Tab keeps
  working normally in other windows.

* **Steam's FPS counter with *Our overlay* on (after 0.6.10)** — Steam's overlay is loaded into the app's games again;
  in a real game with a real controller, the Steam / PS button and Shift+Tab should open only the quick menu (Steam's
  overlay stays closed, its FPS counter keeps showing). The Steam button is checked here only up to Steam's own handler
  with a test window (no real button press yet); the log line "Steam's overlay held back from N Steam-button
  press(es)" shows it working. Shift+Tab open → close → open again was replayed with real key presses (a test window
  with Steam's overlay, not a real game).

* **The Downloads section lag** a tester reported (not reproduced here: 10–23 ms with 120 downloads) — the log now has
  a "Downloads page opened" / "couch Downloads tab opened" line per visit and a "UI stall" line naming the code if it
  hangs.

* **Ghost Recon Wildlands (EMPRESS)** is not fixed: with the performance-cores fix and GE-Proton it still quits with
  code 255 at a random moment during loading (a clean run: 210 s of black, then 255). The cause inside EMP.dll is
  still unknown. 0.6.7 no longer presents the performance-cores fix as the cure (it is offered as something to try,
  never switched on by itself), and Fix my game now watches such a game long enough to see it quit.

(The developer's PC had *Steam overlay in games* switched off, so in-game invite buttons could not work; it is on now.)
