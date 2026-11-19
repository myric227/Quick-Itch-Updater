from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
import os
from pathlib import Path
from queue import Empty, SimpleQueue

from keyring.errors import KeyringError
from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .appearance import CheckCellDelegate, STATUS_COLORS, THEMES, apply_theme
from .files import EXTRACT_ZIPS, KEEP_ARCHIVES, FileSafetyError, validate_destination
from .itch import ItchError


LOGGER = logging.getLogger(__name__)
LIBRARY_SORTS = ("pack", "creator", "tracking", "status", "last_checked", "latest_update")
ATTENTION_STATUSES = {"Ready to download", "Update available", "Possible change", "Files missing locally", "Check failed"}


def date_label(value):
    if value:
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%b %d, %Y %H:%M")
        except ValueError:
            return value
    return "—"


def sort_date(value):
    if value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def library_status(row):
    if not row.available:
        return "Unavailable on itch.io"
    return (row.status or "Not checked") if row.tracked else "Not tracked"


def sorted_library(rows, column, descending):
    key = (
        lambda row: row.asset.title.casefold(),
        lambda row: row.asset.creator.casefold(),
        lambda row: row.tracked,
        lambda row: library_status(row).casefold(),
        lambda row: sort_date(row.last_checked),
        lambda row: sort_date(row.last_update),
    )[column]
    values, missing = [], []
    for row in sorted(rows, key=lambda item: (item.asset.title.casefold(), item.asset.id)):
        value = key(row)
        if value is None:
            missing.append(row)
        else:
            values.append((value, row))
    return [row for _, row in sorted(values, key=lambda item: item[0], reverse=descending)] + missing


def open_folder(path, parent):
    try:
        os.startfile(path)
    except OSError:
        QMessageBox.warning(parent, "Folder unavailable", f"Windows could not open {path}.")


def backup_notice(paths):
    return "".join(f"\nKept a previous download at {path}" for path in paths)


def pack_page_url(value):
    url = QUrl(value)
    if url.isValid() and url.scheme() in {"http", "https"} and url.host() and not url.userInfo():
        if url.host() == "itch.io" or url.host().endswith(".itch.io"):
            url.setScheme("https")
        return url
    return None


def storage_picker(current, *, allow_default=False):
    picker = QComboBox()
    if allow_default:
        picker.addItem("Use default", None)
    picker.addItem("Extract ZIPs and keep the files", EXTRACT_ZIPS)
    picker.addItem("Keep the original download only", KEEP_ARCHIVES)
    picker.setCurrentIndex(picker.findData(current))
    return picker


class Window(QMainWindow):
    def __init__(self, store, updater):
        super().__init__()
        self.store = store
        self.updater = updater
        self.theme = store.setting("theme") or "system"
        self.is_dark = apply_theme(QApplication.instance(), self.theme)
        self.account = int(store.setting("account") or 0)
        self.pending = {}
        self.failed_downloads = {}
        self.rows = []
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.progress_queue = SimpleQueue()
        self.poll_timer = QTimer(self, interval=100)
        self.poll_timer.timeout.connect(self.poll_job)
        self.setWindowTitle(f"Quick Itch Updater {__version__}")
        self.setMinimumSize(950, 520)
        self.resize(1130, 640)

        self.body = QWidget()
        self.setCentralWidget(self.body)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("Quick Itch Updater", objectName="title")
        header.addWidget(title)
        header.addStretch()
        self.settings = QPushButton("Settings")
        self.settings.clicked.connect(self.show_settings)
        header.addWidget(self.settings)
        layout.addLayout(header)

        self.summary = QLabel(objectName="subtitle")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        tools = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Search by pack, creator, or itch.io URL…", clearButtonEnabled=True)
        self.search.textChanged.connect(self.render)
        tools.addWidget(self.search, 1)
        self.tracked = QCheckBox("Tracked only")
        self.tracked.setChecked(store.setting("tracked_only") == "1")
        self.tracked.toggled.connect(self.set_tracked_filter)
        tools.addWidget(self.tracked)
        self.attention = QCheckBox("Needs attention")
        self.attention.setToolTip("Show packs with files to download or checks that failed")
        self.attention.setChecked(store.setting("needs_attention") == "1")
        self.attention.toggled.connect(self.set_attention_filter)
        tools.addWidget(self.attention)
        self.refresh = QPushButton("Refresh Library", clicked=self.refresh_library)
        tools.addWidget(self.refresh)
        self.check = QPushButton("Check for Updates", clicked=self.check_updates)
        tools.addWidget(self.check)
        self.download = QPushButton("Download Updates", objectName="primary", clicked=self.choose_download)
        tools.addWidget(self.download)
        layout.addLayout(tools)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Asset pack", "Creator", "Tracking", "Status", "Last checked", "Latest update"])
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setItemDelegateForColumn(2, CheckCellDelegate(self.table))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        table_header = self.table.horizontalHeader()
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionsClickable(True)
        table_header.setSortIndicatorShown(True)
        sort_name, _, sort_direction = (store.setting("library_sort") or "pack:asc").partition(":")
        sort_column = LIBRARY_SORTS.index(sort_name) if sort_name in LIBRARY_SORTS else 0
        sort_order = Qt.SortOrder.DescendingOrder if sort_direction == "desc" else Qt.SortOrder.AscendingOrder
        table_header.setSortIndicator(sort_column, sort_order)
        table_header.sortIndicatorChanged.connect(self.set_library_sort)
        for column, width in enumerate((130, 80, 150, 144, 144), start=1):
            self.table.setColumnWidth(column, width)
        self.table.itemChanged.connect(self.change_tracking)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.table.itemDoubleClicked.connect(self.open_item_page)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.location = QLabel(objectName="location", wordWrap=True)
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.location, 1)
        self.page = QPushButton("Open Pack Page", clicked=self.open_pack_page)
        self.page.setToolTip("Open the selected pack in your default browser")
        self.open_downloads = QPushButton("Open Download Folder", clicked=self.open_download_folder)
        self.files = QPushButton("Manage Files", clicked=self.manage_files)
        self.folder = QPushButton("Pack Settings", clicked=self.pack_settings)
        self.history = QPushButton("Download History", clicked=self.show_history)
        actions.addWidget(self.page)
        actions.addWidget(self.open_downloads)
        actions.addWidget(self.files)
        actions.addWidget(self.folder)
        actions.addWidget(self.history)
        layout.addLayout(actions)

        self.progress = QProgressBar(textVisible=False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = QLabel("Connect your itch.io account in Settings to import your library.",
                             objectName="status", wordWrap=True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        self.retry = QPushButton("Retry Failed Downloads", clicked=self.retry_failed_downloads)
        status_row.addWidget(self.retry)
        layout.addLayout(status_row)
        self.reload()
        QApplication.styleHints().colorSchemeChanged.connect(self.system_theme_changed)
        if self.account:
            QTimer.singleShot(0, self.check_updates)

    def reload(self):
        self.rows = self.store.library(self.account) if self.account else []
        tracked_ids = {row.asset.id for row in self.rows if row.tracked and row.available}
        self.failed_downloads = {key: upload for key, upload in self.failed_downloads.items() if key[0] in tracked_ids}
        self.render()

    def set_tracked_filter(self, enabled):
        self.store.set_setting("tracked_only", "1" if enabled else "0")
        self.render()

    def set_attention_filter(self, enabled):
        self.store.set_setting("needs_attention", "1" if enabled else "0")
        self.render()

    def set_library_sort(self, column, order):
        direction = "desc" if order == Qt.SortOrder.DescendingOrder else "asc"
        self.store.set_setting("library_sort", f"{LIBRARY_SORTS[column]}:{direction}")
        self.render()

    def system_theme_changed(self):
        if self.theme == "system":
            self.refresh_theme()

    def refresh_theme(self):
        self.is_dark = apply_theme(QApplication.instance(), self.theme)
        self.render()

    def selected_id(self):
        item = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected_row(self):
        asset_id = self.selected_id()
        return next((row for row in self.rows if row.asset.id == asset_id), None)

    def render(self, *_):
        search = self.search.text().casefold().strip().rstrip("/")
        selected_id = self.selected_id()
        selected_column = max(0, self.table.currentColumn())
        displayed = [r for r in self.rows if (not self.tracked.isChecked() or r.tracked)
                     and (not self.attention.isChecked() or library_status(r) in ATTENTION_STATUSES) and any(
            search in value.casefold() for value in (r.asset.title, r.asset.creator, r.asset.url)
        )]
        header = self.table.horizontalHeader()
        displayed = sorted_library(displayed, header.sortIndicatorSection(),
                                   header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder)
        tracked = sum(r.tracked and r.available for r in self.rows)
        summary = (f"{len(displayed)} of {len(self.rows)} projects  ·  {tracked} tracked"
                   if self.account else "No itch.io account connected")
        self.summary.setText(summary)
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(len(displayed))
        self.table.clearSelection()
        colors = STATUS_COLORS[self.is_dark]
        for index, row in enumerate(displayed):
            status = library_status(row)
            values = (row.asset.title, row.asset.creator, "On" if row.tracked else "Off", status,
                      date_label(row.last_checked), date_label(row.last_update))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0 and pack_page_url(row.asset.url) is not None:
                    item.setToolTip(f"Double-click to open the pack page\n{row.asset.url}")
                else:
                    item.setToolTip(value)
                item.setData(Qt.ItemDataRole.UserRole, row.asset.id)
                if column == 2:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if row.tracked else Qt.CheckState.Unchecked)
                if column == 3 and status in colors:
                    item.setForeground(QColor(colors[status]))
                self.table.setItem(index, column, item)
            if row.asset.id == selected_id:
                self.table.setCurrentCell(index, selected_column)
        self.table.blockSignals(False)
        self.update_actions()

    def update_actions(self):
        row = self.selected_row()
        busy = self.future is not None
        self.settings.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self.refresh.setEnabled(not busy and bool(self.account))
        self.check.setEnabled(not busy and bool(self.account))
        self.download.setEnabled(not busy and any(self.pending.values()))
        self.page.setEnabled(not busy and row is not None and pack_page_url(row.asset.url) is not None)
        self.open_downloads.setEnabled(not busy and row is not None)
        self.files.setEnabled(not busy and row is not None)
        self.folder.setEnabled(not busy and row is not None)
        self.history.setEnabled(not busy and row is not None)
        self.retry.setVisible(bool(self.failed_downloads))
        self.retry.setEnabled(not busy and bool(self.failed_downloads))
        self.location.setText(str(self.updater.folder(row)) if row else "Select a pack to see its download folder.")

    def open_item_page(self, item):
        if item.column() == 0:
            self.open_pack_page()

    def open_pack_page(self):
        row = self.selected_row()
        url = pack_page_url(row.asset.url) if row else None
        if url is not None and not QDesktopServices.openUrl(url):
            self.status.setText(f"Windows could not open the browser. Pack page: {url.toDisplayString()}")

    def open_download_folder(self):
        row = self.selected_row()
        if row:
            path = self.updater.folder(row)
            if path.is_dir():
                open_folder(path, self)
            else:
                self.status.setText("This pack’s download folder does not exist yet.")

    def change_tracking(self, item):
        if item.column() != 2 or self.future is not None:
            return
        asset_id = item.data(Qt.ItemDataRole.UserRole)
        enabled = item.checkState() == Qt.CheckState.Checked
        self.store.track(self.account, asset_id, enabled)
        self.pending.pop(asset_id, None)
        self.reload()
        self.status.setText("Tracking enabled. Check for updates when ready." if enabled else "Tracking disabled.")

    def show_settings(self):
        dialog = QDialog(self, windowTitle="Settings")
        dialog.setMinimumWidth(500)
        layout = QFormLayout(dialog)
        account_name = self.store.setting(f"name:{self.account}") if self.account else None
        account = QLabel(account_name or ("Connected" if self.account else "Not connected"))
        account.setTextFormat(Qt.TextFormat.PlainText)
        layout.addRow("itch.io account", account)
        theme = QComboBox()
        for label, value in THEMES:
            theme.addItem(label, value)
        theme.setCurrentIndex(max(0, theme.findData(self.theme)))
        layout.addRow("Theme", theme)
        key = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText="Paste a personal itch.io API key")
        layout.addRow("API key", key)
        visible = QCheckBox("Show key")
        visible.toggled.connect(lambda show: key.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password))
        layout.addRow("", visible)
        default_root = str(Path.home() / "GameAssets")
        root = QLineEdit(self.store.setting("download_root") or default_root, readOnly=True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: root.setText(
            QFileDialog.getExistingDirectory(dialog, "Default download folder", root.text()) or root.text()))
        root_row = QHBoxLayout()
        root_row.addWidget(root, 1)
        root_row.addWidget(browse)
        layout.addRow("Default download folder", root_row)
        mode = storage_picker(self.store.setting("storage_mode") or EXTRACT_ZIPS)
        layout.addRow("Save future downloads", mode)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            validate_destination(root.text())
        except (FileSafetyError, OSError) as error:
            QMessageBox.warning(self, "Invalid download location", str(error) if isinstance(error, FileSafetyError)
                                else "Windows could not verify the selected folder.")
            return
        self.store.set_setting("download_root", root.text())
        self.store.set_setting("storage_mode", mode.currentData())
        if theme.currentData() != self.theme:
            self.theme = theme.currentData()
            self.store.set_setting("theme", self.theme)
            self.refresh_theme()
        else:
            self.update_actions()
        api_key = key.text().strip()
        key.clear()
        if api_key:
            self.run_job("Connecting and importing library…", self.updater.connect, (api_key,), self.connected)
        else:
            self.status.setText("Settings saved.")

    def connected(self, result):
        if self.account != result[0]:
            self.failed_downloads.clear()
        self.account, count = result
        self.pending.clear()
        self.reload()
        self.status.setText(f"Imported {count} projects. Enable tracking for the packs you want to monitor.")
        if any(row.tracked and row.available for row in self.rows):
            self.check_updates()

    def refresh_library(self):
        self.run_job("Refreshing library…", self.updater.refresh_library, (self.account,), self.library_refreshed)

    def library_refreshed(self, count):
        self.pending.clear()
        self.reload()
        self.status.setText(f"Refreshed {count} projects. Check for updates to refresh tracked pack status.")

    def check_updates(self):
        if not any(row.tracked and row.available for row in self.rows):
            self.status.setText("Enable tracking for a pack to check it for updates.")
            return
        self.pending.clear()
        self.run_job("Checking tracked packs…", self.updater.check, (self.account,), self.checked)

    def checked(self, result):
        self.pending = result.pending
        for key, upload in list(self.failed_downloads.items()):
            if key[0] in self.pending and all(item.revision != upload.revision for item in self.pending[key[0]]):
                del self.failed_downloads[key]
        self.reload()
        count = sum(len(changes) for changes in self.pending.values())
        if result.errors:
            title, message = result.errors[0]
            summary = f"{len(result.errors)} checks failed. {title} — {message}"
        else:
            summary = f"Checked {len(self.pending)} tracked packs. {count} files to review."
        self.status.setText(summary + backup_notice(result.retained_backups))

    def choose_download(self):
        dialog = QDialog(self, windowTitle="Download Updates")
        dialog.resize(810, 390)
        layout = QVBoxLayout(dialog)
        instructions = QLabel("Check the files you want to download. Restore hidden files anytime in Manage Files.",
                              wordWrap=True)
        layout.addWidget(instructions)
        choices = QListWidget()
        choices.setItemDelegate(CheckCellDelegate(choices))
        choices.setAlternatingRowColors(True)
        layout.addWidget(choices)
        controls = QHBoxLayout()
        select_all = QPushButton("Check All")
        hide = QPushButton("Hide Checked Files")
        controls.addWidget(select_all)
        controls.addWidget(hide)
        controls.addStretch()
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.addButton("Download", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def selected():
            return [choices.item(i).data(Qt.ItemDataRole.UserRole) for i in range(choices.count())
                    if choices.item(i).checkState() == Qt.CheckState.Checked]

        def update_buttons():
            count = len(selected())
            confirm.setText(f"Download {count}" if count else "Download")
            confirm.setEnabled(count > 0)
            hide.setEnabled(count > 0)
            select_all.setEnabled(choices.count() > count)

        def populate():
            choices.blockSignals(True)
            choices.clear()
            for row in self.rows:
                for upload, reason in self.pending.get(row.asset.id, {}).items():
                    size = f"{upload.size:,} bytes" if upload.size else "size unknown"
                    item = QListWidgetItem(f"{row.asset.title}  ·  {upload.label}  ·  {reason}  ·  {size}")
                    item.setToolTip(item.text())
                    item.setData(Qt.ItemDataRole.UserRole, (row.asset.id, upload))
                    item.setCheckState(Qt.CheckState.Unchecked)
                    choices.addItem(item)
            choices.blockSignals(False)
            update_buttons()

        def check_all():
            choices.blockSignals(True)
            for index in range(choices.count()):
                choices.item(index).setCheckState(Qt.CheckState.Checked)
            choices.blockSignals(False)
            update_buttons()

        def hide_checked():
            groups = {}
            for asset, upload in selected():
                groups.setdefault(asset, {})[upload.id] = False
            count = sum(len(group) for group in groups.values())
            for asset, visibility in groups.items():
                self.pending[asset] = self.change_upload_visibility(asset, visibility)
            self.reload()
            populate()
            instructions.setText(f"Hidden files: {count}. To restore them, select the pack and click Manage Files.")

        choices.itemChanged.connect(update_buttons)
        select_all.clicked.connect(check_all)
        hide.clicked.connect(hide_checked)
        populate()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_downloads(selected())

    def manage_files(self):
        row = self.selected_row()
        asset = row.asset.id
        uploads = self.store.uploads(self.account, asset)
        if not uploads:
            text = ("Enable tracking and check for updates to load this pack’s file list."
                    if uploads is None else "Itch.io returned no supported files for this pack.")
            QMessageBox.information(self, "Manage Files", text)
            return
        hidden = self.store.hidden_uploads(self.account, asset)
        dialog = QDialog(self, windowTitle=f"Manage Files — {row.asset.title}")
        dialog.resize(650, 340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Keep a file checked to include it in updates and downloads. "
                                "New files appear for review.", wordWrap=True))
        files = QListWidget()
        files.setItemDelegate(CheckCellDelegate(files))
        files.setAlternatingRowColors(True)
        for upload in uploads:
            item = QListWidgetItem(upload.label)
            item.setToolTip(upload.label)
            item.setCheckState(Qt.CheckState.Unchecked if upload.id in hidden else Qt.CheckState.Checked)
            files.addItem(item)
        layout.addWidget(files)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            visibility = {upload.id: files.item(index).checkState() == Qt.CheckState.Checked
                          for index, upload in enumerate(uploads)}
            pending = self.change_upload_visibility(asset, visibility)
            if row.tracked and row.available:
                self.pending[asset] = pending
            self.reload()
            self.status.setText(f"File preferences saved for {row.asset.title}.")

    def change_upload_visibility(self, asset, visibility):
        pending = self.updater.change_upload_visibility(self.account, asset, visibility)
        for upload, visible in visibility.items():
            if not visible:
                self.failed_downloads.pop((asset, upload), None)
        return pending

    def start_downloads(self, selections):
        if selections:
            self.run_job(f"Preparing {len(selections)} files…", self.updater.download_many,
                         (self.account, selections, self.report_progress),
                         lambda result: self.downloaded(result, selections))

    def retry_failed_downloads(self):
        self.start_downloads([(key[0], upload) for key, upload in self.failed_downloads.items()])

    def report_progress(self, number, count, filename, received, total):
        self.progress_queue.put((number, count, filename, received, total))

    def downloaded(self, result, selections):
        for asset, upload in selections:
            self.failed_downloads.pop((asset, upload.id), None)
        for asset, upload in result.retryable:
            self.failed_downloads[(asset, upload.id)] = upload
        self.pending.update(result.pending)
        self.reload()
        summary = f"Saved {result.saved} · Already saved {result.duplicates} · Could not download {len(result.errors)}"
        if result.preserved:
            summary += f"\nKept {result.preserved} existing folders because they contain local changes."
        if result.cleanup_pending:
            summary += "\nA temporary backup is still on disk. The app will retry cleanup on the next update check."
        summary += backup_notice(result.retained_backups)
        self.status.setText(summary)
        if result.errors:
            details = QMessageBox(QMessageBox.Icon.Warning, "Download results", summary, parent=self)
            details.setDetailedText("\n".join(f"{filename} — {error}" for filename, error in result.errors))
            details.exec()

    def pack_settings(self):
        row = self.selected_row()
        dialog = QDialog(self, windowTitle=f"Pack Settings — {row.asset.title}")
        dialog.setMinimumWidth(480)
        layout = QFormLayout(dialog)
        path = QLineEdit(row.destination or "", readOnly=True, placeholderText="Use default download folder")
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: path.setText(QFileDialog.getExistingDirectory(
            dialog, "Pack download folder", str(self.updater.folder(row))) or path.text()))
        reset = QPushButton("Use Default", clicked=path.clear)
        folder_row = QHBoxLayout()
        folder_row.addWidget(path, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(reset)
        layout.addRow("Download folder", folder_row)
        mode = storage_picker(row.storage_mode, allow_default=True)
        layout.addRow("Save future downloads", mode)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                if path.text():
                    validate_destination(path.text())
            except (FileSafetyError, OSError) as error:
                QMessageBox.warning(self, "Invalid download location", str(error) if isinstance(error, FileSafetyError)
                                    else "Windows could not verify the selected folder.")
                return
            self.store.set_pack_settings(self.account, row.asset.id, path.text() or None, mode.currentData())
            self.reload()

    def show_history(self):
        row = self.selected_row()
        versions = self.store.versions(self.account, row.asset.id)
        paths = {}
        for version in versions:
            paths.setdefault(version.path, version)
        dialog = QDialog(self, windowTitle=f"Download History — {row.asset.title}")
        dialog.resize(620, 300)
        layout = QVBoxLayout(dialog)
        files = QListWidget()
        if not paths:
            layout.addWidget(QLabel("No saved downloads for this pack."))
        for index, version in enumerate(paths.values()):
            path = version.path
            label = "Current  ·  " if version.current else ""
            filename = f"{version.manifest.filename} (extracted)" if version.manifest else path.name
            files.addItem(f"{label}{date_label(version.saved_at)}  ·  {filename}")
            files.item(index).setToolTip(str(path))
        files.setCurrentRow(0)
        layout.addWidget(files)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        folder = buttons.addButton("Open Version Folder", QDialogButtonBox.ButtonRole.ActionRole)
        folder.setEnabled(bool(paths))
        def open_selected():
            version = list(paths.values())[files.currentRow()]
            open_folder(version.path if version.manifest else version.path.parent, dialog)

        folder.clicked.connect(open_selected)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def run_job(self, text, operation, arguments, done):
        if self.future is not None:
            return
        self.status.setText(text)
        self.done = done
        self.future = self.executor.submit(operation, *arguments)
        self.update_actions()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.poll_timer.start()

    def poll_job(self):
        latest = None
        while True:
            try:
                latest = self.progress_queue.get_nowait()
            except Empty:
                break
        if latest is not None:
            number, count, filename, received, total = latest
            if total:
                self.progress.setRange(0, 100)
                self.progress.setValue(int(received * 100 / total))
            else:
                self.progress.setRange(0, 0)
            amount = f"{received:,} bytes" + (f" of {total:,}" if total else "")
            self.status.setText(f"File {number} of {count} · {filename} · {amount}")
        if not self.future.done():
            return
        self.poll_timer.stop()
        self.progress.hide()
        future, self.future = self.future, None
        try:
            self.done(future.result())
        except (ItchError, FileSafetyError) as error:
            self.status.setText(str(error))
        except KeyringError:
            self.status.setText("Windows Credential Manager could not access the saved key.")
        except Exception as error:
            LOGGER.warning("Operation failed (%s)", type(error).__name__)
            self.status.setText("Could not process the request. Check the destination or retry the operation.")
        finally:
            self.update_actions()

    def closeEvent(self, event):
        if self.future is not None:
            self.status.setText("An operation is in progress. Close the app after it finishes.")
            event.ignore()
        else:
            self.executor.shutdown(wait=False)
            event.accept()
