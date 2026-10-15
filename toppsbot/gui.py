"""Main window: product list, settings, Start/Stop and live log."""
from __future__ import annotations

import copy
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QStyle,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__, config
from .config import Product
from .engine import Engine

try:
    import winsound
except ImportError:  # not on Windows
    winsound = None

COL_URL, COL_QTY, COL_MAX, COL_STATUS = range(4)
GOOD_WORDS = ("in stock", "order placed", "test ok")
BAD_WORDS = ("error", "failed", "gave up", "over max")


class Bridge(QObject):
    """Carries engine events (background thread) to the GUI thread."""
    event = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = config.load()
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.engine = Engine(self.bridge.event.emit)

        self.setWindowTitle(f"Topps Bot {__version__}")
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Topps Bot")
        self.tray.show()
        self.resize(1000, 680)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(self._build_products())
        layout.addWidget(self._build_settings())
        layout.addLayout(self._build_controls())
        layout.addWidget(self._build_log(), stretch=1)
        self.setCentralWidget(root)

        for product in self.settings.products:
            self._add_row(product)
        self._set_running(False)
        self.log("Ready. 1) Open Chrome / Log in  2) Add product URLs  3) Start")

    # ---------- layout ----------

    def _build_products(self) -> QGroupBox:
        box = QGroupBox("Products to monitor")
        layout = QVBoxLayout(box)

        add_row = QHBoxLayout()
        self.url_input = QLineEdit(placeholderText="Paste a topps.com product URL and press Enter")
        self.url_input.returnPressed.connect(self.add_product)
        self.qty_input = self._qty_spin(1)
        self.max_input = self._max_spin(0)
        self.add_btn = QPushButton("Add product")
        self.add_btn.clicked.connect(self.add_product)
        add_row.addWidget(self.url_input, stretch=1)
        add_row.addWidget(QLabel("Qty"))
        add_row.addWidget(self.qty_input)
        add_row.addWidget(QLabel("Max price"))
        add_row.addWidget(self.max_input)
        add_row.addWidget(self.add_btn)
        layout.addLayout(add_row)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Product URL", "Qty", "Max price", "Status"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_URL, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_QTY, 70)
        self.table.setColumnWidth(COL_MAX, 110)
        layout.addWidget(self.table)

        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self.remove_selected)
        layout.addWidget(self.remove_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        return box

    def _build_settings(self) -> QGroupBox:
        box = QGroupBox("Settings")
        layout = QHBoxLayout(box)
        s = self.settings

        self.interval_min = QSpinBox(minimum=5, maximum=600, value=s.interval_min, suffix=" s")
        self.interval_max = QSpinBox(minimum=5, maximum=600, value=s.interval_max, suffix=" s")
        self.test_mode = QCheckBox("Test mode (stop before Place Order)", checked=s.test_mode)
        self.clear_cart = QCheckBox("Empty cart before buying", checked=s.clear_cart)
        self.sound = QCheckBox("Sound alerts", checked=s.sound)
        for widget in (self.interval_min, self.interval_max):
            widget.valueChanged.connect(self.save_settings)
        for widget in (self.test_mode, self.clear_cart, self.sound):
            widget.toggled.connect(self.save_settings)

        layout.addWidget(QLabel("Check every"))
        layout.addWidget(self.interval_min)
        layout.addWidget(QLabel("to"))
        layout.addWidget(self.interval_max)
        layout.addSpacing(20)
        layout.addWidget(self.test_mode)
        layout.addWidget(self.clear_cart)
        layout.addWidget(self.sound)
        layout.addStretch()
        return box

    def _build_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.login_btn = QPushButton("Open Chrome / Log in")
        self.login_btn.clicked.connect(self.open_login)
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.engine.stop)
        for btn, color in ((self.start_btn, "#1e8e3e"), (self.stop_btn, "#c5221f")):
            btn.setMinimumHeight(40)
            btn.setMinimumWidth(140)
            btn.setStyleSheet(
                f"QPushButton {{ background: {color}; color: white; font-weight: bold; border-radius: 4px; }}"
                "QPushButton:disabled { background: #9aa0a6; }"
            )
        self.login_btn.setMinimumHeight(40)
        self.state_label = QLabel()
        self.state_label.setFont(QFont(self.font().family(), 11, QFont.Weight.Bold))
        row.addWidget(self.login_btn)
        row.addStretch()
        row.addWidget(self.state_label)
        row.addStretch()
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        return row

    def _build_log(self) -> QGroupBox:
        box = QGroupBox("Activity log")
        layout = QVBoxLayout(box)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_view)
        return box

    @staticmethod
    def _qty_spin(value: int) -> QSpinBox:
        return QSpinBox(minimum=1, maximum=99, value=value)

    @staticmethod
    def _max_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(minimum=0, maximum=100000, decimals=2, value=value, prefix="$")
        spin.setSpecialValueText("No limit")
        return spin

    # ---------- products ----------

    def _add_row(self, product: Product) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        url_item = QTableWidgetItem(product.url)
        url_item.setData(Qt.ItemDataRole.UserRole, product.id)
        url_item.setToolTip(product.url)
        self.table.setItem(row, COL_URL, url_item)
        qty = self._qty_spin(product.quantity)
        max_price = self._max_spin(product.max_price)
        qty.valueChanged.connect(lambda v, p=product: self._update_product(p, quantity=v))
        max_price.valueChanged.connect(lambda v, p=product: self._update_product(p, max_price=v))
        self.table.setCellWidget(row, COL_QTY, qty)
        self.table.setCellWidget(row, COL_MAX, max_price)
        self.table.setItem(row, COL_STATUS, QTableWidgetItem("Idle"))

    def _update_product(self, product: Product, **changes) -> None:
        for key, value in changes.items():
            setattr(product, key, value)
        self.save_settings()

    def _row_of(self, product_id: str) -> int:
        for row in range(self.table.rowCount()):
            if self.table.item(row, COL_URL).data(Qt.ItemDataRole.UserRole) == product_id:
                return row
        return -1

    def add_product(self) -> None:
        url = self.url_input.text().strip()
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            QMessageBox.warning(self, "Invalid URL", "Paste a full product URL, e.g. https://www.topps.com/...")
            return
        if any(p.url == url for p in self.settings.products):
            QMessageBox.information(self, "Already added", "That product is already in the list.")
            return
        product = Product(url=url, quantity=self.qty_input.value(), max_price=self.max_input.value())
        self.settings.products.append(product)
        self._add_row(product)
        self.save_settings()
        self.url_input.clear()

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            product_id = self.table.item(row, COL_URL).data(Qt.ItemDataRole.UserRole)
            self.settings.products = [p for p in self.settings.products if p.id != product_id]
            self.table.removeRow(row)
        self.save_settings()

    # ---------- actions ----------

    def save_settings(self, *_) -> None:
        s = self.settings
        s.interval_min = self.interval_min.value()
        s.interval_max = max(self.interval_min.value(), self.interval_max.value())
        s.test_mode = self.test_mode.isChecked()
        s.clear_cart = self.clear_cart.isChecked()
        s.sound = self.sound.isChecked()
        self._update_state_label()
        try:
            config.save(s)
        except OSError as e:
            self.log(f"Could not save settings: {e}", "error")

    def open_login(self) -> None:
        origin = config.DEFAULT_ORIGIN
        if self.settings.products:
            parts = urlparse(self.settings.products[0].url)
            origin = f"{parts.scheme}://{parts.netloc}"
        self.engine.open_login(origin)

    def start(self) -> None:
        if not self.settings.products:
            QMessageBox.information(self, "No products", "Add at least one product URL first.")
            return
        if not self.test_mode.isChecked():
            answer = QMessageBox.question(
                self, "Live mode",
                "Test mode is OFF - the bot will place real orders and charge your saved card.\n\nContinue?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.save_settings()
        self._set_running(True)
        self.engine.start(copy.deepcopy(self.settings))

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        for widget in (self.url_input, self.qty_input, self.max_input, self.add_btn, self.remove_btn,
                       self.interval_min, self.interval_max, self.test_mode, self.clear_cart):
            widget.setEnabled(not running)
        for row in range(self.table.rowCount()):
            for col in (COL_QTY, COL_MAX):
                self.table.cellWidget(row, col).setEnabled(not running)
        self._update_state_label()

    def _update_state_label(self) -> None:
        if getattr(self, "running", False):
            text, color = ("RUNNING - TEST MODE", "#e37400") if self.test_mode.isChecked() else ("RUNNING - LIVE", "#1e8e3e")
        else:
            text, color = "STOPPED", "#5f6368"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(f"color: {color};")

    # ---------- engine events ----------

    def on_event(self, event: dict) -> None:
        kind = event["type"]
        if kind == "log":
            self.log(event["msg"], event["level"])
        elif kind == "status":
            self._set_status(event["id"], event["text"])
        elif kind == "running":
            self._set_running(event["value"])
        elif kind == "alert":
            self.tray.showMessage(event["title"], event["msg"], QSystemTrayIcon.MessageIcon.Warning, 10000)
            QApplication.alert(self)
            if self.settings.sound and winsound:
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)

    def _set_status(self, product_id: str, text: str) -> None:
        row = self._row_of(product_id)
        if row < 0:
            return
        item = QTableWidgetItem(f"{datetime.now():%H:%M:%S}  {text}")
        lowered = text.lower()
        if any(word in lowered for word in BAD_WORDS):
            item.setForeground(QColor("#c5221f"))
        elif any(word in lowered for word in GOOD_WORDS):
            item.setForeground(QColor("#1e8e3e"))
        self.table.setItem(row, COL_STATUS, item)

    def log(self, msg: str, level: str = "info") -> None:
        prefix = {"warning": "! ", "error": "ERROR: "}.get(level, "")
        self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] {prefix}{msg}")

    def closeEvent(self, event) -> None:
        if self.running and QMessageBox.question(
            self, "Quit", "The bot is running. Stop it and quit?"
        ) != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.save_settings()
        self.engine.shutdown()
        self.tray.hide()
        event.accept()


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("toppsbot")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


def main() -> None:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("Topps Bot")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
