"""Override-editor — QDialog för att redigera _params/overrides.json."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget, QWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
)

from shared import load_overrides, load_dotterbolag_full

REPO_ROOT = Path(__file__).resolve().parent
OVERRIDES_PATH = REPO_ROOT / "_params" / "overrides.json"
DOTTERBOLAG_PATH = REPO_ROOT / "_params" / "Dotterbolagslista.xlsx"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".overrides_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class OverrideEditor(QDialog):
    """Dialog med tre flikar: subject / attachment / country overrides."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Redigera overrides")
        self.resize(900, 600)

        self._companies = load_dotterbolag_full(DOTTERBOLAG_PATH)
        self._valid_ids = set(self._companies.keys())

        layout = QVBoxLayout(self)

        info = QLabel(
            "Redigera mappningar i <code>_params/overrides.json</code>. "
            "BolagsID måste finnas i Dotterbolagslistan. "
            "Tomma rader ignoreras vid sparning."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        data = load_overrides()
        self.subject_table = self._build_subject_tab(data.get("subject_overrides", {}))
        self.attachment_table = self._build_attachment_tab(data.get("attachment_overrides", []))
        self.country_table = self._build_country_tab(data.get("country_overrides", {}))
        self.alias_table = self._build_alias_tab(data.get("aliases", {}))
        self.excluded_table = self._build_excluded_tab(data.get("excluded", []))

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Spara")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        button_row.addWidget(cancel_btn)
        button_row.addWidget(save_btn)
        layout.addLayout(button_row)

    def _build_subject_tab(self, data: dict[str, int]) -> QTableWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "<b>Subject overrides</b> — mappar msg_path.stem (filnamn utan .msg) till bolagsID."
        ))
        table = QTableWidget(0, 3, page)
        table.setHorizontalHeaderLabels(["msg_stem", "bolag_id", "bolag (info)"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for stem, bolag_id in sorted(data.items()):
            self._append_subject_row(table, stem, int(bolag_id))
        table.itemChanged.connect(lambda _it, t=table: self._refresh_company_info(t, id_col=1, info_col=2))
        v.addWidget(table, 1)
        v.addLayout(self._row_buttons(table, lambda: self._append_subject_row(table, "", 0)))
        self.tabs.addTab(page, "Subject")
        return table

    def _build_attachment_tab(self, data: list[dict]) -> QTableWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "<b>Attachment overrides</b> — när ett mail har bilagor för flera bolag. "
            "Matchar (msg_stem, lowercase-substring i bilagans filnamn) → bolagsID."
        ))
        table = QTableWidget(0, 4, page)
        table.setHorizontalHeaderLabels(["msg_stem", "attachment_substr", "bolag_id", "bolag (info)"])
        for col in (0, 1, 3):
            table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for item in data:
            self._append_attachment_row(
                table, item.get("msg_stem", ""), item.get("attachment_substr", ""),
                int(item.get("bolag_id", 0)),
            )
        table.itemChanged.connect(lambda _it, t=table: self._refresh_company_info(t, id_col=2, info_col=3))
        v.addWidget(table, 1)
        v.addLayout(self._row_buttons(table, lambda: self._append_attachment_row(table, "", "", 0)))
        self.tabs.addTab(page, "Attachment")
        return table

    def _build_alias_tab(self, data: dict[str, list[str]]) -> QTableWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "<b>Aliases</b> — alternativa namn/fraser som ger full poäng om de förekommer "
            "i filnamn, ämne, bilaga, avsändare eller body. En rad per (bolag, fras). "
            "Användbart när bolagets riktiga namn aldrig nämns i mailen "
            "(t.ex. <code>GF Sich</code> för Goldfunk Sicherheitstechnik)."
        ))
        table = QTableWidget(0, 3, page)
        table.setHorizontalHeaderLabels(["bolag_id", "alias", "bolag (info)"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for bolag_id, phrases in sorted(data.items(), key=lambda kv: int(kv[0])):
            for phrase in phrases:
                self._append_alias_row(table, int(bolag_id), phrase)
        table.itemChanged.connect(lambda _it, t=table: self._refresh_company_info(t, id_col=0, info_col=2))
        v.addWidget(table, 1)
        v.addLayout(self._row_buttons(table, lambda: self._append_alias_row(table, 0, "")))
        self.tabs.addTab(page, "Aliases")
        return table

    def _build_excluded_tab(self, data: list) -> QTableWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "<b>Excluded</b> — bolag som GUI:t gömmer i tabellen som default. "
            "Påverkar <i>inte</i> matchning eller pipeline; endast visuellt. "
            "Tipp: högerklicka på en rad i tabellen för att toggla."
        ))
        table = QTableWidget(0, 2, page)
        table.setHorizontalHeaderLabels(["bolag_id", "bolag (info)"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for bid in sorted({int(i) for i in data if str(i).strip().lstrip("-").isdigit()}):
            self._append_excluded_row(table, bid)
        table.itemChanged.connect(lambda _it, t=table: self._refresh_company_info(t, id_col=0, info_col=1))
        v.addWidget(table, 1)
        v.addLayout(self._row_buttons(table, lambda: self._append_excluded_row(table, 0)))
        self.tabs.addTab(page, "Excluded")
        return table

    def _append_excluded_row(self, table: QTableWidget, bolag_id: int) -> None:
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(str(bolag_id) if bolag_id else ""))
        info = QTableWidgetItem(self._company_info(bolag_id))
        info.setFlags(info.flags() & ~Qt.ItemIsEditable)
        table.setItem(r, 1, info)

    def _append_alias_row(self, table: QTableWidget, bolag_id: int, phrase: str) -> None:
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(str(bolag_id) if bolag_id else ""))
        table.setItem(r, 1, QTableWidgetItem(phrase))
        info = QTableWidgetItem(self._company_info(bolag_id))
        info.setFlags(info.flags() & ~Qt.ItemIsEditable)
        table.setItem(r, 2, info)

    def _build_country_tab(self, data: dict[str, str]) -> QTableWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(QLabel(
            "<b>Country overrides</b> — för bolag vars Market-kolumn (C) i Dotterbolagslistan "
            "inte stämmer (t.ex. interna bolag som ska routas till annat land)."
        ))
        table = QTableWidget(0, 3, page)
        table.setHorizontalHeaderLabels(["bolag_id", "country", "bolag (info)"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for bolag_id, country in sorted(data.items(), key=lambda kv: int(kv[0])):
            self._append_country_row(table, int(bolag_id), country)
        table.itemChanged.connect(lambda _it, t=table: self._refresh_company_info(t, id_col=0, info_col=2))
        v.addWidget(table, 1)
        v.addLayout(self._row_buttons(table, lambda: self._append_country_row(table, 0, "Sweden")))
        self.tabs.addTab(page, "Country")
        return table

    def _row_buttons(self, table: QTableWidget, add_callback) -> QHBoxLayout:
        h = QHBoxLayout()
        add = QPushButton("Lägg till rad")
        add.clicked.connect(add_callback)
        rm = QPushButton("Ta bort markerad rad")
        rm.clicked.connect(lambda: self._remove_selected(table))
        h.addWidget(add)
        h.addWidget(rm)
        h.addStretch(1)
        return h

    def _remove_selected(self, table: QTableWidget) -> None:
        rows = sorted({i.row() for i in table.selectedItems()}, reverse=True)
        for r in rows:
            table.removeRow(r)

    def _append_subject_row(self, table: QTableWidget, stem: str, bolag_id: int) -> None:
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(stem))
        table.setItem(r, 1, QTableWidgetItem(str(bolag_id) if bolag_id else ""))
        info = QTableWidgetItem(self._company_info(bolag_id))
        info.setFlags(info.flags() & ~Qt.ItemIsEditable)
        table.setItem(r, 2, info)

    def _append_attachment_row(self, table: QTableWidget, stem: str, substr: str, bolag_id: int) -> None:
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(stem))
        table.setItem(r, 1, QTableWidgetItem(substr))
        table.setItem(r, 2, QTableWidgetItem(str(bolag_id) if bolag_id else ""))
        info = QTableWidgetItem(self._company_info(bolag_id))
        info.setFlags(info.flags() & ~Qt.ItemIsEditable)
        table.setItem(r, 3, info)

    def _append_country_row(self, table: QTableWidget, bolag_id: int, country: str) -> None:
        r = table.rowCount()
        table.insertRow(r)
        table.setItem(r, 0, QTableWidgetItem(str(bolag_id) if bolag_id else ""))
        table.setItem(r, 1, QTableWidgetItem(country))
        info = QTableWidgetItem(self._company_info(bolag_id))
        info.setFlags(info.flags() & ~Qt.ItemIsEditable)
        table.setItem(r, 2, info)

    def _company_info(self, bolag_id: int) -> str:
        if not bolag_id:
            return ""
        meta = self._companies.get(int(bolag_id))
        if not meta:
            return "❌ saknas i Dotterbolagslistan"
        return f"{meta.get('name', '')} ({meta.get('country', '')})"

    def _refresh_company_info(self, table: QTableWidget, id_col: int, info_col: int) -> None:
        for r in range(table.rowCount()):
            id_item = table.item(r, id_col)
            info_item = table.item(r, info_col)
            if not id_item or not info_item:
                continue
            text = id_item.text().strip()
            try:
                bolag_id = int(text) if text else 0
            except ValueError:
                bolag_id = 0
            new_info = self._company_info(bolag_id)
            if info_item.text() != new_info:
                info_item.setText(new_info)

    def _collect_subject(self) -> tuple[dict[str, int], list[str]]:
        out: dict[str, int] = {}
        errors: list[str] = []
        for r in range(self.subject_table.rowCount()):
            stem = (self.subject_table.item(r, 0).text() if self.subject_table.item(r, 0) else "").strip()
            id_text = (self.subject_table.item(r, 1).text() if self.subject_table.item(r, 1) else "").strip()
            if not stem and not id_text:
                continue
            if not stem or not id_text:
                errors.append(f"Subject rad {r + 1}: båda fälten måste fyllas.")
                continue
            try:
                bid = int(id_text)
            except ValueError:
                errors.append(f"Subject rad {r + 1}: bolag_id ej heltal: '{id_text}'")
                continue
            if bid not in self._valid_ids:
                errors.append(f"Subject rad {r + 1}: bolag_id {bid} finns inte i Dotterbolagslistan.")
                continue
            out[stem] = bid
        return out, errors

    def _collect_attachment(self) -> tuple[list[dict], list[str]]:
        out: list[dict] = []
        errors: list[str] = []
        for r in range(self.attachment_table.rowCount()):
            stem = (self.attachment_table.item(r, 0).text() if self.attachment_table.item(r, 0) else "").strip()
            substr = (self.attachment_table.item(r, 1).text() if self.attachment_table.item(r, 1) else "").strip()
            id_text = (self.attachment_table.item(r, 2).text() if self.attachment_table.item(r, 2) else "").strip()
            if not stem and not substr and not id_text:
                continue
            if not stem or not substr or not id_text:
                errors.append(f"Attachment rad {r + 1}: alla fält måste fyllas.")
                continue
            try:
                bid = int(id_text)
            except ValueError:
                errors.append(f"Attachment rad {r + 1}: bolag_id ej heltal: '{id_text}'")
                continue
            if bid not in self._valid_ids:
                errors.append(f"Attachment rad {r + 1}: bolag_id {bid} finns inte i Dotterbolagslistan.")
                continue
            out.append({"msg_stem": stem, "attachment_substr": substr.lower(), "bolag_id": bid})
        return out, errors

    def _collect_country(self) -> tuple[dict[str, str], list[str]]:
        out: dict[str, str] = {}
        errors: list[str] = []
        for r in range(self.country_table.rowCount()):
            id_text = (self.country_table.item(r, 0).text() if self.country_table.item(r, 0) else "").strip()
            country = (self.country_table.item(r, 1).text() if self.country_table.item(r, 1) else "").strip()
            if not id_text and not country:
                continue
            if not id_text or not country:
                errors.append(f"Country rad {r + 1}: båda fälten måste fyllas.")
                continue
            try:
                bid = int(id_text)
            except ValueError:
                errors.append(f"Country rad {r + 1}: bolag_id ej heltal: '{id_text}'")
                continue
            if bid not in self._valid_ids:
                errors.append(f"Country rad {r + 1}: bolag_id {bid} finns inte i Dotterbolagslistan.")
                continue
            out[str(bid)] = country
        return out, errors

    def _collect_aliases(self) -> tuple[dict[str, list[str]], list[str]]:
        out: dict[str, list[str]] = {}
        errors: list[str] = []
        for r in range(self.alias_table.rowCount()):
            id_text = (self.alias_table.item(r, 0).text() if self.alias_table.item(r, 0) else "").strip()
            phrase = (self.alias_table.item(r, 1).text() if self.alias_table.item(r, 1) else "").strip()
            if not id_text and not phrase:
                continue
            if not id_text or not phrase:
                errors.append(f"Aliases rad {r + 1}: båda fälten måste fyllas.")
                continue
            try:
                bid = int(id_text)
            except ValueError:
                errors.append(f"Aliases rad {r + 1}: bolag_id ej heltal: '{id_text}'")
                continue
            if bid not in self._valid_ids:
                errors.append(f"Aliases rad {r + 1}: bolag_id {bid} finns inte i Dotterbolagslistan.")
                continue
            out.setdefault(str(bid), []).append(phrase)
        return out, errors

    def _collect_excluded(self) -> tuple[list[int], list[str]]:
        out: set[int] = set()
        errors: list[str] = []
        for r in range(self.excluded_table.rowCount()):
            id_text = (self.excluded_table.item(r, 0).text() if self.excluded_table.item(r, 0) else "").strip()
            if not id_text:
                continue
            try:
                bid = int(id_text)
            except ValueError:
                errors.append(f"Excluded rad {r + 1}: bolag_id ej heltal: '{id_text}'")
                continue
            if bid not in self._valid_ids:
                errors.append(f"Excluded rad {r + 1}: bolag_id {bid} finns inte i Dotterbolagslistan.")
                continue
            out.add(bid)
        return sorted(out), errors

    def _on_save(self) -> None:
        subject, e1 = self._collect_subject()
        attachment, e2 = self._collect_attachment()
        country, e3 = self._collect_country()
        aliases, e4 = self._collect_aliases()
        excluded, e5 = self._collect_excluded()
        errors = e1 + e2 + e3 + e4 + e5
        if errors:
            QMessageBox.warning(self, "Valideringsfel", "\n".join(errors))
            return
        existing = load_overrides()
        existing.update({
            "subject_overrides": subject,
            "attachment_overrides": attachment,
            "country_overrides": country,
            "aliases": aliases,
            "excluded": excluded,
        })
        try:
            _atomic_write_json(OVERRIDES_PATH, existing)
        except OSError as e:
            QMessageBox.critical(self, "Skrivfel", f"Kunde inte spara: {e}")
            return
        alias_count = sum(len(v) for v in aliases.values())
        QMessageBox.information(
            self, "Sparat",
            f"Sparat {len(subject)} subject, {len(attachment)} attachment, "
            f"{len(country)} country, {alias_count} aliases, {len(excluded)} excluded.",
        )
        self.accept()
