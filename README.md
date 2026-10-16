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
| **Couch mode** | Full-screen, controller-friendly library in the style of SteamOS (F11 or the sidebar button), opening with a 2-second intro (any button skips it): Steam artwork, a shelf of your games, Library grid, Downloads; any Linux game controller, keyboard or mouse. Quiet sounds as you move around and press into things, and a mouse click for the in-app browser's pointer (Settings → App → Sounds, with their own volume; the intro has its own volume too). The big ✕ leaves it — and first closes, completely, a game started from it. |
| **Wishlist** | Its own tab: add any game through Steam's store search (even unreleased ones) or with ♡ on a game page; the app keeps looking for it on every source and notifies you when one has it. |
| **Game updates** | Knows which version is installed (from the archive name) and checks the game's page every few hours; *Update* downloads the new release over the installed game — saves, settings, fixes and the Proton prefix stay. Optional: updates download and install by themselves. |
| **Save backups** | Saves in each game's Proton prefix are zipped after every session and before updates, uninstalls and prefix rebuilds (caches/logs left out); restore with undo; backups survive a reinstall — and, with a synced folder (Syncthing, Nextcloud, Drive), move between your PCs. |
| **Storage** | Disk use per game (files, prefix, downloads, saves), free space per drive, and one-click cleanup of archives, leftovers, cache and unused prefixes. |
| **App updates** | Checks a GitHub releases page once a day; downloads are verified against GitHub's SHA-256 and the app swaps itself in on restart (previous version kept until the new one runs). |
| **Settings** | *Find a setting* search box; folders, queue, torrent network & bandwidth, extraction, sources on/off, cache TTL & clearing, logs, accent colour / contrast / card size / reduced motion, notifications, database backup & restore. |
| **Runs on Linux?** | Every card, couch tile and game page shows how well the game runs under Proton — "Linux 92%" = 92 % of ProtonDB players' reports say it works (offline index; game pages add ProtonDB's own tier). |
| **Organise** | Favourites (always on top), collections, hiding, and the order of the Library (recently played, A–Z, recently added, most played) — on the desktop and in couch mode. |
| **Quick menu** | Hold the PS / Xbox / Steam button while playing (couch mode open): a panel slides in over the game — back to the game, **switch game** (the one you were playing is *suspended*: frozen, using no processor or graphics time, and it continues exactly where you left it), go to couch mode (suspending the game), the game's own volume, its settings, quit (press twice). Suspended games show in couch mode and the menu; A continues them. Only games the app runs — directly or through Steam for its overlay; a game started in Steam itself is never touched. Leaving couch mode or quitting the app lets every suspended game run again. |
| **Screens & controllers** | Couch mode reads each monitor's real resolution from the desktop (KDE's kscreen-doctor, GNOME's Mutter, xrandr) — a 4K monitor at 150 % is 3840 × 2160, not the 5120 × 2880 Qt can report. On Wayland a full-screen window's size is the compositor's answer: couch mode never leaves full screen to "fix" it (that shrank it to 800 × 500 on some desktops) — KWin is asked to size it, and the log records the compositor and every size it sent for the first seconds ("couch display trace"). Interface size up to 200 % fits every page (on a 1440-high window 200 % is about 180 %, the most that fits). "Keep above" is for couch mode only and is released from every window when it closes. A Steam Deck / Steam Controller shows its own buttons (LB/RB on the original controller, L1/R1 on the Deck and the new one), also when Steam hands it to the app as keyboard and mouse: which key is which button is read from the Desktop Configuration Steam itself loaded (named in Steam's controller log — Steam's defaults, or the user's own), so every button means what its prompt says, and prompts for buttons Steam turns into mouse clicks are left out. The Steam button is read from Steam's log too: a tap goes Home (or opens the quick menu over a game) and Big Picture is put away at once. Without Steam, a thumb resting on the left trackpad no longer scrolls. The clock shows 12-hour time. Couch mode and the desktop stay in step: settings changed on either side show on the other at once, couch mode opens on the game selected on the desktop and the desktop selects the game couch mode was on, suspended games show on the desktop (Resume / Stop, and More → Suspend there too). |
| **Couch mode = desktop** | Everything the desktop Library does, by controller: **Finish setup** (a checklist from download to Play, one button for the next step — unpack, the repack's installer, the base game in Steam, the game's program), every fix, Run with, Steam App ID, DLL overrides, winecfg / Winetricks / reset prefix, *See what's wrong* (the debugging assistant's findings with their fixes), collections, rename, update check, repack check, add to Steam, remove from the Library, delete a save backup; and in Settings → Library: check every game for updates, Storage, clean up leftovers, import a game folder. Fix my game's test starts can be stopped (B / *Stop testing*). |
| **Game settings (couch)** | Manage → Game settings, or from the quick menu: Proton version, Steam overlay, launch options (on-screen keyboard with symbols and capitals), FPS limit (DirectX games), MangoHud, GameMode — from the game's next start. |
| **GPU acceleration** | One switch (Settings → Interface, and couch mode's Settings → App), *recommended*, off by default. Couch mode is then drawn by the graphics card (OpenGL, the same picture; ~4× faster frames at 4K) from the next time it opens; after a restart the in-app browser draws pages and decodes video on it (desktop windows are not composed through OpenGL: on Wayland that drew menus garbled). Off, the processor does everything — one switch (the separate "frames through the graphics card" switch is part of it now). If OpenGL does not start, couch mode reopens drawn by the CPU and says so; if a start with it never reaches the window, the next start leaves it out. |
| **Quick ways in** | Drop a `.torrent`, a magnet / download link, a game folder or an archive on the window; a magnet link on the clipboard is offered as a download; tray menu, progress on the taskbar icon, *Keep downloading in the tray* when closing; the app reopens where you left it (page, All Games filters, selected game). |

## Source integration notes (verified)

Both sites were inspected live on **2026-10-09** before the adapters were written.
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
| UnRAR / 7-Zip (**required**) | Both sources ship games as RAR archives |
| umu-launcher | Runs Windows games with Proton even without Steam — downloads GE-Proton and the Steam Runtime itself |
| Steam | Only where umu-launcher is not packaged (Debian/Ubuntu, openSUSE): provides Proton |
| Winetricks (+ zenity) | Windows runtimes in a game's prefix |
| GameMode, MangoHud, gamescope | Launch-option helpers (`gamemoderun`, `mangohud`, `gamescope -- %command%`) |

Already-installed tools are skipped and packages your repositories don't have are reported, not
fatal. The same check lives in the app (**Settings → System check → Install missing tools**, with a
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
part**: do the “I'm not a robot” check if one appears and click the site's own download button. The window then
closes by itself; the transfer continues in the background under **Downloads → In-app browser downloads**
(progress, speed, time left, pause / resume / cancel) and, when it finishes, is **added to your Library
automatically** (SHA-256 checked when the site publishes one, extracted with the password, launch files
detected). Nothing is automated, faked or bypassed: the app never solves checks, never sends a fake Referer and
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
overlay (Shift+Tab any time: friends, chat, browser, screenshots, FPS counter, Steam Input) to games it starts
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

**Quick overlay (fallback)** — for a game Steam does not know yet (you chose *Just play* instead of restarting
Steam), the app loads Steam's overlay into the game itself (`gameoverlayrenderer.so` preloaded — Steam Linux Runtime
carries it into its container — and Steam's Vulkan layer, `ENABLE_VK_LAYER_VALVE_steam_overlay_1`). Steam then only
shows the friends list and Shift+Tab may not reopen it; once the game's shortcut is in Steam, its game id is passed
too. Both can be switched off; *Fixes → Turn off the Steam overlay for this game* keeps it out of one game.

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
* **Quit from the controller:** hold the PS / Xbox / Steam button (1.6 s) while playing a game started from couch
  mode: couch mode comes to the front and asks *Quit <game>?* (*Keep playing* is focused; *Quit game* stops it
  completely, like the ✕). Games started from the desktop are never touched.
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
saves are not zipped again; the newest 10 automatic and 10 safety backups (20 manual) are kept, per game
*title* — so a reinstall finds them. Library → a game → **Saves**: *Back up now*, *Restore…* (your current saves
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

Publishing a release: bump `__version__` in `ezgd/__init__.py`, then
`packaging/build_linux.sh && packaging/publish_github.sh owner/name` (needs `gh auth login`).

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

*Learning on this computer:* every fix the assistant applies is followed up by the game's next start. A fix that
did not help a game ranks lower for it next time ("✗ You tried this for this game before"); one that fixed other
games — especially with the same engine or from the same source — ranks higher ("✓ This fixed 2 other games like
this one").


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
  prefixes or games on NTFS/exFAT/FAT drives, the GPU(s) and driver (`vulkaninfo`).
* **ProtonDB** rating (with a Steam App ID): a *Borked* game is said to be unlikely to have a fix.

**Try fixes for me** tests candidate setups for real — the current one, the fixes the findings recommend, the
other installed Proton versions and WineD3D for graphics problems (at most 6). Each trial launches the game with
detailed logging in a **throw-away copy of its prefix** (an instant reflink copy on btrfs/xfs, so winetricks
installs are kept; your real prefix and saves are never touched), for up to 45 seconds, and reports whether it
kept running, crashed (with the reason) or exited. *Use this setup* saves a setup that kept running; “kept
running” means it did not crash — check it looked right. Trials can be stopped any time.

**The assistant builds its own fixes** (`ezgd/library/fixgen.py`) from the evidence and the symptom you pick
under *What happens?* (doesn't start · crashes after a while · black screen · no sound · runs badly): forcing the
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

**Fix my game** (top of the Diagnose window) is the one-button path: it checks everything, repairs on its
own only what has strong evidence and is reversible (damaged files, Steam-emulator setup, unapplied fix archives
and folders, the release's launch script / the real exe, missing runtimes — installed and waited for, OnlineFix
DLLs), then tests setups in throw-away prefixes, keeps the first that ran (preferring ones that drew frames) and
answers in one line: *Fixed — kept running with …* or *Not fixed: …because…*. **Did it work?** Yes records the
outcome; No undoes the kept setup (file repairs stay) and shows the other options. The window shows your real
fix rate ("fixed for you: 4 of 5 games"). It never resets a prefix, runs admin commands or downloads on its own —
those stay as buttons (when nothing worked and a newer GE-Proton exists: *Install GE-Proton… and try again*). When no single setup works, it also tests up to three **combinations** of the ones that got furthest (e.g. a different Proton *and* DirectX 11 — some games need both); contradicting pairs (two Proton versions, the same variable or DLL set two ways) are skipped.

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
* **Privacy/logs**: no cookies, accounts or credentials are stored. Logs strip
  query strings, URL credentials and magnet trackers. Problem reports are local text files.
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
| *Source unavailable* banner | The site is down or blocked on your network; cached results are shown. *Retry* later. |
| *Layout changed* | The site's HTML changed. Use *Report a problem* (saves a local report in the data folder) and update the app. |
| `CERTIFICATE_VERIFY_FAILED` for a source | Your system clock may be wrong, or a network middlebox intercepts TLS. The app never disables verification. |
| Torrent stays at *Fetching metadata* / *Stalled* | Nobody is sharing it right now, or your firewall blocks the listen port (Settings → Torrents; try enabling UPnP or forwarding the port). |
| “Access denied (HTTP 403)” / “web page instead of a file” | The link expired or needs the browser; open it from the game page and use the final link. |
| RAR extraction: “needs UnRAR or 7-Zip” | Install `unrar` / `7zip` (Linux) or 7-Zip / WinRAR (Windows), or set the tool path. |
| Windows game won't start on Linux | Install Proton (Steam → Tools) or GE-Proton, choose it in the game's *Proton version*; some games simply do not work under Proton (anti-cheat, launchers). |
| Database problems | Settings → Database → *Restore from backup…* (automatic backups are taken before migrations and at exit). |
| Debugging | Settings → *Log level: Debug*, then *Open log folder*; *Recent errors…* lists the stored error summaries. |

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
* **Proton launches** use `proton run` with a per-game prefix outside Steam; titles
  needing the Steam runtime or Steamworks may require launching via the Steam shortcut.
* Theme changes recolour most widgets live; a restart applies them everywhere.
* No `.msi`/AppImage installer — PyInstaller folders/zip/tarball only (Linux builds update themselves from GitHub
  releases; see *App updates*).
