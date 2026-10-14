"""Add your own game site: the app checks it first and explains in red why a site cannot be added."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QVBoxLayout

from ezgd.ui import theme
from ezgd.ui.widgets.common import button, label


class AddSiteDialog(QDialog):
    def __init__(self, actions, parent=None):
        super().__init__(parent)
        self.actions = actions
        self.bridge = actions.bridge
        self.added: dict | None = None
        self.setWindowTitle("Add a site")
        self.resize(640, 380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(10)
        lay.addWidget(label("Add your own game site", "h2"))
        lay.addWidget(label("Sites like SteamRIP that are built on WordPress with its public posts API can be added. "
                            "The app checks the site first: it must be a game download site whose newest posts "
                            "contain game downloads, it must allow apps to read it (robots.txt), and it must not be "
                            "behind a bot check or a login. Video, music, social sites, shops and official stores "
                            "cannot be added.", "muted", wrap=True))
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com")
        self.url.returnPressed.connect(self._check)
        row.addWidget(self.url, 1)
        self.go = button("Check and add", "check", "primary", on_click=self._check)
        row.addWidget(self.go)
        lay.addLayout(row)
        self.status = label("", "muted", wrap=True)
        lay.addWidget(self.status)
        self.error = label("", wrap=True, selectable=True)
        self.error.setStyleSheet(f"color: {theme.CURRENT.danger}; font-weight: 600;")
        self.error.hide()
        lay.addWidget(self.error)
        self.ok = label("", wrap=True)
        self.ok.setStyleSheet(f"color: {theme.CURRENT.success}; font-weight: 600;")
        self.ok.hide()
        lay.addWidget(self.ok)
        lay.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        self.bb = bb
        lay.addWidget(bb)

    def _check(self) -> None:
        url = self.url.text().strip()
        self.error.hide()
        self.ok.hide()
        if not url:
            self._fail("Enter the site's web address, for example https://example.com.")
            return
        self.go.setEnabled(False)
        self.url.setEnabled(False)
        self.status.setText("Checking the site… (robots.txt, its WordPress posts API and its newest posts)")
        self.bridge.call(self.bridge.core.add_custom_source(url), self._added, self._failed, owner=self)

    def _added(self, r: dict) -> None:
        self.added = r
        self.status.setText("")
        examples = ", ".join(r.get("examples", [])[:3])
        self.ok.setText(f"Added {r['name']}. {r['posts_with_downloads']} of its {r['posts_checked']} newest posts "
                        f"have game downloads" + (f" (e.g. {examples})" if examples else "") + ". Its games now "
                        "appear in All Games and search.")
        self.ok.show()
        self.go.setEnabled(False)
        self.bb.clear()
        done = self.bb.addButton("Done", QDialogButtonBox.AcceptRole)
        done.setProperty("variant", "primary")

    def _failed(self, err: BaseException) -> None:
        self.go.setEnabled(True)
        self.url.setEnabled(True)
        self.status.setText("")
        self._fail(f"This site can't be added: {err}" if str(err) else "This site can't be added.")

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


def open_add_site(actions, parent) -> dict | None:
    dlg = AddSiteDialog(actions, parent)
    dlg.exec()
    return dlg.added
