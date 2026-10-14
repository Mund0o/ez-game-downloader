#!/usr/bin/env bash
# EZ Game Downloader — Linux installer.
#
# Run it from the unpacked release folder:   ./install.sh
#
#  1. copies the app to ~/.local/opt/EZGameDownloader (or /opt with --system)
#  2. adds it to your application menu and puts `ezgd` on your PATH
#  3. installs the tools the app uses with your package manager (one password
#     prompt): UnRAR/7-Zip, umu-launcher (or Steam where umu is not packaged),
#     Winetricks, GameMode, MangoHud, gamescope
#
# Options:  --system   install for all users into /opt (needs sudo)
#           --no-deps  skip installing system packages
#           --yes      do not ask before installing packages
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SYSTEM=0; DEPS=1; YES=""
for arg in "$@"; do
    case "$arg" in
        --system) SYSTEM=1 ;;
        --no-deps) DEPS=0 ;;
        --yes|-y) YES="--yes" ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ ! -x "$SRC/ezgd" ] || [ ! -d "$SRC/_internal" ]; then
    echo "error: run install.sh from the unpacked EZGameDownloader folder (ezgd and _internal/ must be next to it)" >&2
    exit 1
fi

if [ "$SYSTEM" = 1 ]; then
    DEST=/opt/EZGameDownloader; BIN=/usr/local/bin; APPS=/usr/share/applications
    ICONS=/usr/share/icons/hicolor/256x256/apps; SUDO=sudo
else
    DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
    DEST="$HOME/.local/opt/EZGameDownloader"; BIN="$HOME/.local/bin"; APPS="$DATA/applications"
    ICONS="$DATA/icons/hicolor/256x256/apps"; SUDO=""
fi

echo "==> Installing EZ Game Downloader to $DEST"
if [ "$(readlink -f "$SRC")" != "$(readlink -f "$DEST" 2>/dev/null || true)" ]; then
    $SUDO rm -rf "$DEST.new"
    $SUDO mkdir -p "$(dirname "$DEST")"
    $SUDO cp -a "$SRC" "$DEST.new"
    $SUDO rm -rf "$DEST"
    $SUDO mv "$DEST.new" "$DEST"
fi

$SUDO mkdir -p "$BIN" "$APPS" "$ICONS"
$SUDO ln -sf "$DEST/ezgd" "$BIN/ezgd"
$SUDO install -m644 "$DEST/ezgd.png" "$ICONS/ezgd.png"
$SUDO tee "$APPS/ezgd.desktop" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=EZ Game Downloader
Comment=Game catalog, torrent/HTTP download manager and library
Exec="$DEST/ezgd"
Icon=ezgd
Terminal=false
Categories=Game;Network;FileTransfer;
StartupWMClass=ezgd
EOF
command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$APPS" || true
command -v gtk-update-icon-cache >/dev/null && $SUDO gtk-update-icon-cache -q -t "$(dirname "$(dirname "$ICONS")")" || true

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "note: $BIN is not on your PATH; start the app from the menu or run $DEST/ezgd" ;;
esac

if [ "$DEPS" = 1 ]; then
    echo
    echo "==> Checking the tools the app uses"
    "$DEST/ezgd" --setup-system $YES || echo "warning: some packages could not be installed; see Settings → System check"
fi

echo
echo "Done. Start “EZ Game Downloader” from your application menu, or run: ezgd"
echo "Uninstall with: $DEST/uninstall.sh"
