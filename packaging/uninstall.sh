#!/usr/bin/env bash
# Remove EZ Game Downloader (the app only; your downloads, games and library database are kept).
#   --purge   also delete the app's settings, database, cache, logs and Proton prefixes
#             (downloaded games in your download/install folders are never deleted)
set -euo pipefail
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

remove() { [ -e "$1" ] || [ -L "$1" ] || return 0; if [ -w "$(dirname "$1")" ]; then rm -rf "$1"; else sudo rm -rf "$1"; fi; }

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
for d in "$HOME/.local/opt/EZGameDownloader" /opt/EZGameDownloader; do remove "$d"; done
for f in "$HOME/.local/bin/ezgd" /usr/local/bin/ezgd "$DATA/applications/ezgd.desktop" /usr/share/applications/ezgd.desktop \
         "$DATA/icons/hicolor/256x256/apps/ezgd.png" /usr/share/icons/hicolor/256x256/apps/ezgd.png; do
    remove "$f"
done

if [ "$PURGE" = 1 ]; then
    for d in "$DATA/EZGameDownloader" "${XDG_CONFIG_HOME:-$HOME/.config}/EZGameDownloader" \
             "${XDG_CACHE_HOME:-$HOME/.cache}/EZGameDownloader" "${XDG_STATE_HOME:-$HOME/.local/state}/EZGameDownloader"; do
        remove "$d"
    done
    echo "Removed the app and its data (your games and downloads were not touched)."
else
    echo "Removed the app. Your library, settings and downloads are kept (use --purge to delete app data)."
fi
echo "System packages installed for it (unrar, umu-launcher, gamemode, …) are left in place."
