"""Dark gaming theme: palette + generated Qt style sheet."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from ezgd.core.settings import ACCENTS

SOURCE_COLORS = {"onlinefix": "#38bdf8", "steamrip": "#f472b6", "crackedgames": "#34d399", "fitgirl": "#c084fc", "ankergames": "#fb923c"}
SOURCE_NAMES = {"onlinefix": "OnlineFix", "steamrip": "SteamRIP", "crackedgames": "Cracked-Games", "fitgirl": "FitGirl", "ankergames": "AnkerGames"}


BUILT_IN_SOURCES = ("onlinefix", "steamrip", "crackedgames", "fitgirl", "ankergames")
_EXTRA_COLORS = ["#f59e0b", "#a78bfa", "#fb7185", "#22d3ee", "#a3e635", "#f97316", "#e879f9", "#60a5fa"]


def register_sources(sources: list[dict]) -> None:
    """Add/remove user-added sites in the name/colour tables (built-in ones stay as they are)."""
    custom = [s for s in sources if s.get("custom")]
    ids = {s["id"] for s in custom}
    for sid in [k for k in SOURCE_NAMES if k not in BUILT_IN_SOURCES and k not in ids]:
        SOURCE_NAMES.pop(sid, None)
        SOURCE_COLORS.pop(sid, None)
    for i, s in enumerate(custom):
        SOURCE_NAMES[s["id"]] = s["name"]
        SOURCE_COLORS.setdefault(s["id"], _EXTRA_COLORS[i % len(_EXTRA_COLORS)])


def source_name(source_id: str | None) -> str:
    return SOURCE_NAMES.get(source_id or "", source_id or "Unknown source")


def source_password(source_id: str | None) -> str | None:
    """Password a source says all of its archives use (e.g. OnlineFix), else None."""
    from ezgd.sources import ADAPTER_CLASSES
    cls = ADAPTER_CLASSES.get(source_id or "")
    return cls.info.archive_password if cls else None


@dataclass(frozen=True)
class Palette:
    bg0: str
    bg1: str
    bg2: str
    bg3: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    success: str = "#22c55e"
    warning: str = "#f59e0b"
    danger: str = "#ef4444"
    info: str = "#38bdf8"


def _shift(hex_color: str, factor: float) -> str:
    c = QColor(hex_color)
    return c.lighter(int(100 * factor)).name() if factor >= 1 else c.darker(int(100 / factor)).name()


def make_palette(accent_key: str = "violet", contrast: str = "dark") -> Palette:
    accent = ACCENTS.get(accent_key, ACCENTS["violet"])
    if contrast == "midnight":
        base = dict(bg0="#05060a", bg1="#0a0c12", bg2="#10131b", bg3="#181c27", border="#1f2431")
    else:
        base = dict(bg0="#0d1017", bg1="#121620", bg2="#181d29", bg3="#212736", border="#2a3142")
    return Palette(**base, text="#e7eaf2", muted="#8c95ab", accent=accent, accent_hover=_shift(accent, 1.15))


CURRENT = make_palette()


def stylesheet(p: Palette) -> str:
    return f"""
* {{ outline: 0; }}
QWidget {{ background: {p.bg0}; color: {p.text}; font-size: 10pt; }}
QWidget#sidebar {{ background: {p.bg1}; border-right: 1px solid {p.border}; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 20pt; font-weight: 700; }}
QLabel[role="h2"] {{ font-size: 13pt; font-weight: 650; }}
QLabel[role="muted"] {{ color: {p.muted}; }}
QLabel[role="small"] {{ color: {p.muted}; font-size: 8.5pt; }}
QLabel[role="logo"] {{ font-size: 15pt; font-weight: 800; color: {p.text}; }}
QLabel[role="chip"] {{ border-radius: 9px; padding: 2px 8px; font-size: 8.5pt; font-weight: 600; }}
QFrame#card, QFrame[card="true"] {{ background: {p.bg2}; border: 1px solid {p.border}; border-radius: 12px; }}
QFrame#card .QWidget, QFrame[card="true"] .QWidget, QFrame#hero .QWidget {{ background: transparent; }}
QFrame#hero {{ border-radius: 16px; border: 1px solid {p.border};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {_shift(p.accent, 0.35)}, stop:0.55 {p.bg2}, stop:1 {p.bg1}); }}
QFrame[banner="info"] {{ background: rgba(56,189,248,0.10); border: 1px solid rgba(56,189,248,0.35); border-radius: 10px; }}
QFrame[banner="warning"] {{ background: rgba(245,158,11,0.10); border: 1px solid rgba(245,158,11,0.40); border-radius: 10px; }}
QFrame[banner="error"] {{ background: rgba(239,68,68,0.10); border: 1px solid rgba(239,68,68,0.40); border-radius: 10px; }}
QFrame[banner="success"] {{ background: rgba(34,197,94,0.10); border: 1px solid rgba(34,197,94,0.40); border-radius: 10px; }}
QFrame#toast {{ background: {p.bg3}; border: 1px solid {p.border}; border-radius: 12px; }}

QPushButton {{ background: {p.bg3}; border: 1px solid {p.border}; border-radius: 9px; padding: 7px 14px;
    color: {p.text}; font-weight: 600; }}
QPushButton:hover {{ border-color: {p.accent}; background: {_shift(p.bg3, 1.15)}; }}
QPushButton:pressed {{ background: {p.bg2}; }}
QPushButton:disabled {{ color: {p.muted}; border-color: {p.bg3}; background: {p.bg2}; }}
QPushButton[variant="primary"] {{ background: {p.accent}; border: 1px solid {p.accent}; color: white; }}
QPushButton[variant="primary"]:hover {{ background: {p.accent_hover}; }}
QPushButton[variant="primary"]:disabled {{ background: {_shift(p.accent, 0.5)}; color: {p.muted}; }}
QPushButton[variant="danger"] {{ background: transparent; border: 1px solid {p.danger}; color: {p.danger}; }}
QPushButton[variant="danger"]:hover {{ background: rgba(239,68,68,0.15); }}
QPushButton[variant="play"] {{ background: {p.success}; border: 1px solid {p.success}; color: white;
    font-size: 13pt; font-weight: 800; padding: 11px 34px; border-radius: 10px; }}
QPushButton[variant="play"]:hover {{ background: #2fd66b; }}
QPushButton[variant="play"]:disabled {{ background: {p.bg3}; border-color: {p.border}; color: {p.muted}; }}
QPushButton[variant="stop"] {{ background: {p.danger}; border: 1px solid {p.danger}; color: white;
    font-size: 13pt; font-weight: 800; padding: 11px 34px; border-radius: 10px; }}
QPushButton[variant="setup"] {{ background: {p.accent}; border: 1px solid {p.accent}; color: white;
    font-size: 12pt; font-weight: 700; padding: 11px 26px; border-radius: 10px; }}
QPushButton[variant="ghost"] {{ background: transparent; border: 1px solid transparent; }}
QPushButton[variant="ghost"]:hover {{ background: {p.bg3}; border-color: {p.border}; }}
QPushButton[variant="chip"] {{ border-radius: 14px; padding: 4px 12px; font-weight: 600; background: {p.bg2}; }}
QPushButton[variant="chip"]:checked {{ background: {_shift(p.accent, 0.45)}; border-color: {p.accent}; color: white; }}
QPushButton#nav {{ text-align: left; padding: 10px 14px; border: none; border-radius: 10px; background: transparent;
    color: {p.muted}; font-size: 10.5pt; font-weight: 600; }}
QPushButton#nav:hover {{ background: {p.bg3}; color: {p.text}; }}
QPushButton#nav:checked {{ background: {_shift(p.accent, 0.40)}; color: white; }}
QPushButton#couch {{ text-align: left; padding: 11px 14px; border-radius: 12px; color: white; font-size: 10.5pt;
    font-weight: 800; border: 1px solid {p.accent};
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {p.accent}, stop:1 {_shift(p.accent, 0.55)}); }}
QPushButton#couch:hover {{ background: {p.accent_hover}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 5px; }}
QToolButton:hover {{ background: {p.bg3}; border-color: {p.border}; }}
QToolButton::menu-indicator {{ image: none; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{ background: {p.bg2}; border: 1px solid {p.border};
    border-radius: 9px; padding: 7px 10px; selection-background-color: {p.accent}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {p.accent}; }}
QLineEdit#search {{ font-size: 11pt; padding: 10px 14px 10px 36px; border-radius: 12px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {p.bg2}; border: 1px solid {p.border}; selection-background-color: {p.accent}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {p.border}; background: {p.bg2}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QRadioButton {{ background: transparent; }}

QProgressBar {{ background: {p.bg3}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 4px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
    stop:0 {p.accent}, stop:1 {p.accent_hover}); }}
QProgressBar[state="done"]::chunk {{ background: {p.success}; }}
QProgressBar[state="error"]::chunk {{ background: {p.danger}; }}
QProgressBar[state="paused"]::chunk {{ background: {p.muted}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.bg3}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.border}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.bg3}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QListView, QListWidget, QTreeWidget, QTableWidget {{ background: transparent; border: none; }}
QListWidget::item {{ border-radius: 8px; padding: 6px; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {_shift(p.accent, 0.4)}; color: white; }}
QListWidget::item:hover {{ background: {p.bg2}; }}
QHeaderView::section {{ background: {p.bg2}; color: {p.muted}; border: none; padding: 6px; }}
QTreeWidget {{ alternate-background-color: {p.bg1}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {p.muted}; padding: 8px 14px; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom-color: {p.accent}; }}
QToolTip {{ background: {p.bg3}; color: {p.text}; border: 1px solid {p.border}; border-radius: 6px; padding: 6px; }}
QMenu {{ background: {p.bg2}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.accent}; color: white; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}
QDialog {{ background: {p.bg1}; }}
QMessageBox {{ background: {p.bg1}; }}
QSplitter::handle {{ background: {p.border}; }}
"""


def apply_theme(app: QApplication, accent: str = "violet", contrast: str = "dark") -> Palette:
    global CURRENT
    CURRENT = make_palette(accent, contrast)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, CURRENT.bg0), (QPalette.Base, CURRENT.bg2),
                        (QPalette.AlternateBase, CURRENT.bg1), (QPalette.Text, CURRENT.text),
                        (QPalette.WindowText, CURRENT.text), (QPalette.Button, CURRENT.bg3),
                        (QPalette.ButtonText, CURRENT.text), (QPalette.Highlight, CURRENT.accent),
                        (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, CURRENT.bg3),
                        (QPalette.ToolTipText, CURRENT.text), (QPalette.PlaceholderText, CURRENT.muted)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    font = QFont(app.font())
    font.setPointSizeF(10)
    app.setFont(font)
    app.setStyleSheet(stylesheet(CURRENT))
    return CURRENT


def chip_style(color: str) -> str:
    c = QColor(color)
    return (f"background: rgba({c.red()},{c.green()},{c.blue()},0.16); color: {color}; "
            f"border: 1px solid rgba({c.red()},{c.green()},{c.blue()},0.45);")


STATUS_COLORS = {
    "queued": "#8c95ab", "metadata": "#38bdf8", "checking": "#38bdf8", "downloading": "accent",
    "paused": "#f59e0b", "verifying": "#38bdf8", "completed": "#22c55e", "failed": "#ef4444",
    "cancelled": "#8c95ab", "downloaded": "#38bdf8", "extracting": "#38bdf8", "extracted": "#a78bfa",
    "installed": "#22c55e", "broken": "#ef4444",
}


def status_color(status: str) -> str:
    c = STATUS_COLORS.get(status, CURRENT.muted)
    return CURRENT.accent if c == "accent" else c
