"""Finance-automation GUI — översikt över extract/process-status per bolag.

Kör:  py gui.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QFileSystemWatcher, QTimer
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QTableWidget, QTableWidgetItem,
    QPlainTextEdit, QSplitter, QHeaderView, QMessageBox, QGroupBox,
    QStatusBar, QAbstractItemView, QDialog, QCheckBox, QMenu,
)

import shared
import gui_status
from gui_runner import ScriptRunner
from gui_overrides import OverrideEditor

REPO_ROOT = Path(__file__).resolve().parent
LOGS_DIR = REPO_ROOT / "_logs"

EXCLUDED_FG = QColor(150, 150, 150)
EXCLUDED_BG = QColor(245, 245, 245)

STATUS_COLORS = {
    "OK":    QColor(200, 230, 200),
    "WARN":  QColor(255, 235, 175),
    "ERROR": QColor(245, 200, 200),
    "SKIP":  QColor(225, 225, 225),
    "INFO":  QColor(210, 230, 245),
    "START": QColor(235, 235, 250),
    "DONE":  QColor(220, 240, 220),
}

COUNTRY_FILTER_ALL = "Alla"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Finance Automation — Status")
        self.resize(1400, 850)

        self.runner = ScriptRunner(self)
        self.runner.output_line.connect(self._append_log)
        self.runner.finished.connect(self._on_runner_finished)

        self._load_chain: list[tuple[str, list[str]]] = []

        self._build_ui()
        self._build_menu()

        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._schedule_refresh)
        self._fs_watcher.fileChanged.connect(self._schedule_refresh)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._refresh_table)

        self._populate_periods()
        self._refresh_table()

    # --- UI construction ---

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Period:"))
        self.period_combo = QComboBox()
        self.period_combo.setMinimumWidth(120)
        self.period_combo.currentTextChanged.connect(self._on_period_changed)
        toolbar.addWidget(self.period_combo)

        toolbar.addSpacing(20)
        toolbar.addWidget(QLabel("Land:"))
        self.country_combo = QComboBox()
        self.country_combo.addItems([COUNTRY_FILTER_ALL, *gui_status.KNOWN_COUNTRIES, "Other"])
        self.country_combo.currentTextChanged.connect(lambda _: self._refresh_table())
        toolbar.addWidget(self.country_combo)

        toolbar.addSpacing(20)
        self.show_excluded_cb = QCheckBox("Visa exkluderade")
        self.show_excluded_cb.setChecked(False)
        self.show_excluded_cb.toggled.connect(lambda _: self._refresh_table())
        toolbar.addWidget(self.show_excluded_cb)

        toolbar.addSpacing(20)
        refresh_btn = QPushButton("Uppdatera")
        refresh_btn.clicked.connect(self._refresh_table)
        toolbar.addWidget(refresh_btn)
        toolbar.addStretch(1)

        self.summary_label = QLabel("")
        toolbar.addWidget(self.summary_label)
        outer.addLayout(toolbar)

        body_split = QSplitter(Qt.Horizontal)
        outer.addWidget(body_split, 1)

        # Vänster: tabell + logg
        left = QSplitter(Qt.Vertical)
        body_split.addWidget(left)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Land", "Namn", "Extr", "Proc", "Output", "Status", "Senaste meddelande"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.Stretch)
        h.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        left.addWidget(self.table)

        log_box = QGroupBox("Live-logg")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log_view)
        log_btn_row = QHBoxLayout()
        clear_btn = QPushButton("Rensa logg")
        clear_btn.clicked.connect(self.log_view.clear)
        stop_btn = QPushButton("Stoppa körning")
        stop_btn.clicked.connect(self.runner.stop)
        log_btn_row.addWidget(clear_btn)
        log_btn_row.addWidget(stop_btn)
        log_btn_row.addStretch(1)
        log_layout.addLayout(log_btn_row)
        left.addWidget(log_box)
        left.setStretchFactor(0, 3)
        left.setStretchFactor(1, 2)

        # Höger: åtgärder
        actions = QGroupBox("Åtgärder")
        a = QVBoxLayout(actions)
        self.action_buttons: list[QPushButton] = []

        def add_btn(text: str, handler) -> QPushButton:
            b = QPushButton(text)
            b.clicked.connect(handler)
            a.addWidget(b)
            self.action_buttons.append(b)
            return b

        add_btn("Kör extract", lambda: self._run_script("extract.py"))
        a.addSpacing(8)
        add_btn("Process Sweden", lambda: self._run_script("process_sweden.py"))
        add_btn("Process Norway", lambda: self._run_script("process_norway.py"))
        add_btn("Process Finland", lambda: self._run_script("process_finland.py"))
        add_btn("Process Denmark", lambda: self._run_script("process_denmark.py"))
        add_btn("Process Germany", lambda: self._run_script("process_germany.py"))
        a.addSpacing(8)
        add_btn("Run all", lambda: self._run_script("run_all.py"))
        add_btn("Dry run (extract)", lambda: self._run_script("dry_run.py"))
        a.addSpacing(8)
        add_btn("Ladda databas", self._run_load_db_chain)
        a.addSpacing(8)
        reset_btn = add_btn("Reset perioden …", self._on_reset)
        reset_btn.setStyleSheet("QPushButton { color: #a00; }")
        a.addStretch(1)
        body_split.addWidget(actions)
        body_split.setStretchFactor(0, 5)
        body_split.setStretchFactor(1, 1)

        self.setStatusBar(QStatusBar())

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&Verktyg")
        edit_overrides = QAction("Redigera overrides …", self)
        edit_overrides.triggered.connect(self._open_overrides)
        m.addAction(edit_overrides)

        m.addSeparator()
        quit_act = QAction("Avsluta", self)
        quit_act.triggered.connect(self.close)
        m.addAction(quit_act)

    # --- period & data ---

    def _populate_periods(self) -> None:
        periods = gui_status.available_periods()
        default = shared.prev_month_period()
        if default not in periods:
            periods = [default, *periods]
        self.period_combo.blockSignals(True)
        self.period_combo.clear()
        self.period_combo.addItems(periods)
        idx = self.period_combo.findText(default)
        self.period_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.period_combo.blockSignals(False)

    def _current_period(self) -> str:
        return self.period_combo.currentText() or shared.prev_month_period()

    def _on_period_changed(self, _new: str) -> None:
        self._setup_watcher()
        self._refresh_table()

    def _setup_watcher(self) -> None:
        for d in self._fs_watcher.directories():
            self._fs_watcher.removePath(d)
        for f in self._fs_watcher.files():
            self._fs_watcher.removePath(f)
        period = self._current_period()
        log_dir = LOGS_DIR / period
        log_dir.mkdir(parents=True, exist_ok=True)
        self._fs_watcher.addPath(str(log_dir))
        for jsonl in log_dir.glob("*.jsonl"):
            self._fs_watcher.addPath(str(jsonl))

    def _schedule_refresh(self, _path: str) -> None:
        self._refresh_timer.start()
        # nya filer kanske dyker upp i log-mappen — uppdatera watcher
        period = self._current_period()
        log_dir = LOGS_DIR / period
        for jsonl in log_dir.glob("*.jsonl"):
            sp = str(jsonl)
            if sp not in self._fs_watcher.files():
                self._fs_watcher.addPath(sp)

    def _refresh_table(self) -> None:
        period = self._current_period()
        country_filter = self.country_combo.currentText()
        try:
            rows = gui_status.compute_company_status(period)
        except Exception as e:
            QMessageBox.warning(self, "Fel", f"Kunde inte ladda status: {e}")
            return
        if country_filter != COUNTRY_FILTER_ALL:
            rows = [r for r in rows if r.country == country_filter]

        show_excluded = self.show_excluded_cb.isChecked()
        excluded_total = sum(1 for r in rows if r.excluded)
        if not show_excluded:
            rows = [r for r in rows if not r.excluded]

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        ok = warn = err = extracted = dry_only = processed = 0
        for i, r in enumerate(rows):
            id_item = QTableWidgetItem()
            id_item.setData(Qt.DisplayRole, r.bolag_id)
            id_item.setData(Qt.UserRole, r)
            self.table.setItem(i, 0, id_item)
            self.table.setItem(i, 1, QTableWidgetItem(r.country))
            self.table.setItem(i, 2, QTableWidgetItem(r.name))
            if r.extracted:
                extr_glyph, extr_tip = "✓", "Extraherad (filer på disk)"
            elif r.dry_run_matched:
                extr_glyph, extr_tip = "(✓)", "Matchad i senaste dry-run (ingen riktig extract än)"
            else:
                extr_glyph, extr_tip = "—", "Inte matchad"
            extr_item = QTableWidgetItem(extr_glyph)
            extr_item.setToolTip(extr_tip)
            self.table.setItem(i, 3, extr_item)
            self.table.setItem(i, 4, QTableWidgetItem("✓" if r.processed else "—"))
            self.table.setItem(i, 5, QTableWidgetItem(", ".join(r.output_files) if r.output_files else "—"))
            status_text = f"[{r.last_status}]" if r.last_status else ""
            status_item = QTableWidgetItem(status_text)
            color = STATUS_COLORS.get(r.last_status or "")
            if color:
                for c in range(self.table.columnCount()):
                    item = self.table.item(i, c)
                    if item:
                        item.setBackground(color)
            self.table.setItem(i, 6, status_item)
            self.table.setItem(i, 7, QTableWidgetItem(r.last_msg))
            if r.excluded:
                for c in range(self.table.columnCount()):
                    item = self.table.item(i, c)
                    if item:
                        item.setForeground(EXCLUDED_FG)
                        item.setBackground(EXCLUDED_BG)
                name_item = self.table.item(i, 2)
                if name_item:
                    name_item.setText(f"{r.name}  (exkluderad)")
            if r.last_status == "OK":
                ok += 1
            elif r.last_status == "WARN":
                warn += 1
            elif r.last_status == "ERROR":
                err += 1
            if r.extracted:
                extracted += 1
            elif r.dry_run_matched:
                dry_only += 1
            if r.processed:
                processed += 1
        self.table.setSortingEnabled(True)

        excl_part = (
            f" &nbsp;|&nbsp; <span style='color:#888'>Exkluderade dolda: "
            f"{excluded_total}</span>"
            if excluded_total and not show_excluded else ""
        )
        self.summary_label.setText(
            f"<b>{len(rows)}</b> bolag &nbsp;|&nbsp; "
            f"Extr: {extracted} &nbsp;|&nbsp; "
            f"Dry: {dry_only} &nbsp;|&nbsp; "
            f"Proc: {processed} &nbsp;|&nbsp; "
            f"<span style='color:#080'>OK: {ok}</span> &nbsp;"
            f"<span style='color:#a80'>WARN: {warn}</span> &nbsp;"
            f"<span style='color:#a00'>ERROR: {err}</span>"
            f"{excl_part}"
        )
        self._setup_watcher()

    # --- actions ---

    def _run_script(self, script: str) -> None:
        if self.runner.is_running():
            QMessageBox.information(self, "Pågår", "En körning pågår redan. Vänta tills den är klar.")
            return
        period = self._current_period()
        args = ["--period", period]
        self._set_buttons_enabled(False)
        self.runner.run(script, args)

    def _run_load_db_chain(self) -> None:
        if self.runner.is_running():
            QMessageBox.information(self, "Pågår", "En körning pågår redan. Vänta tills den är klar.")
            return
        period = self._current_period()
        self._load_chain = [
            ("db.py", []),
            ("load_inl.py",  ["--period", period]),
            ("load_sie.py",  ["--period", period]),
            ("load_saft.py", ["--period", period]),
        ]
        self._set_buttons_enabled(False)
        self._run_next_in_chain()

    def _run_next_in_chain(self) -> None:
        if not self._load_chain:
            return
        script, args = self._load_chain.pop(0)
        self.runner.run(script, args)

    def _on_reset(self) -> None:
        if self.runner.is_running():
            QMessageBox.information(self, "Pågår", "En körning pågår redan.")
            return
        period = self._current_period()
        msg = (
            f"Reset perioden {period}?\n\n"
            "Detta flyttar Referens/-filer tillbaka och raderar output/. "
            "Använd dry-run först om du är osäker."
        )
        ret = QMessageBox.question(
            self, "Bekräfta reset", msg,
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if ret != QMessageBox.Yes:
            return
        self._set_buttons_enabled(False)
        self.runner.run("reset.py", ["--period", period])

    def _open_overrides(self) -> None:
        dlg = OverrideEditor(self)
        if dlg.exec() == QDialog.Accepted:
            self._append_log("[GUI] Overrides sparade.")

    # --- runner callbacks ---

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _on_runner_finished(self, code: int) -> None:
        self._append_log(f"[GUI] Avslutad med exit-code {code}.")
        if code == 0 and self._load_chain:
            self._run_next_in_chain()
            return
        self._load_chain = []
        self._set_buttons_enabled(True)
        self._refresh_table()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for b in self.action_buttons:
            b.setEnabled(enabled)

    # --- exkludering ---

    def _on_table_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        id_item = self.table.item(index.row(), 0)
        if not id_item:
            return
        company_row: gui_status.CompanyRow = id_item.data(Qt.UserRole)
        if not company_row:
            return
        menu = QMenu(self.table)
        if company_row.excluded:
            act = menu.addAction(f"Återställ bolag {company_row.bolag_id} ({company_row.name})")
            act.triggered.connect(lambda: self._toggle_excluded(company_row.bolag_id, exclude=False))
        else:
            act = menu.addAction(f"Exkludera bolag {company_row.bolag_id} ({company_row.name})")
            act.triggered.connect(lambda: self._toggle_excluded(company_row.bolag_id, exclude=True))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _toggle_excluded(self, bolag_id: int, exclude: bool) -> None:
        ov = shared.load_overrides()
        excluded = {int(i) for i in ov.get("excluded", []) if str(i).strip().lstrip("-").isdigit()}
        if exclude:
            excluded.add(bolag_id)
        else:
            excluded.discard(bolag_id)
        ov["excluded"] = sorted(excluded)
        try:
            shared.save_overrides(ov)
        except OSError as e:
            QMessageBox.critical(self, "Skrivfel", f"Kunde inte spara overrides: {e}")
            return
        verb = "exkluderad" if exclude else "återställd"
        self._append_log(f"[GUI] Bolag {bolag_id} {verb}.")
        self._refresh_table()

    # --- row drilldown ---

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        id_item = self.table.item(row, 0)
        if not id_item:
            return
        company_row: gui_status.CompanyRow = id_item.data(Qt.UserRole)
        if not company_row:
            return
        def _fmt_files(files: list[str]) -> list[str]:
            return [f"  {n}" for n in files] if files else ["  —"]

        lines = [
            f"Bolag {company_row.bolag_id} — {company_row.name} ({company_row.country})",
            "",
            f"Extraherade filer ({len(company_row.extracted_files)}):",
            *_fmt_files(company_row.extracted_files),
            "",
            f"Referens ({len(company_row.referens_files)}):",
            *_fmt_files(company_row.referens_files),
            "",
            f"Output ({len(company_row.output_files)}):",
            *_fmt_files(company_row.output_files),
            "",
            f"Events ({len(company_row.events)}):",
        ]
        for ev in company_row.events:
            lines.append(
                f"  {ev.get('ts', '')[:19]}  [{ev.get('status', '')}]  "
                f"{ev.get('script', '')}: {ev.get('msg', '')}"
            )
        QMessageBox.information(
            self, f"Bolag {company_row.bolag_id}", "\n".join(lines),
        )


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
