"""PySide6 dialog for direct SQL Server connections; saves settings but never passwords."""
from __future__ import annotations
import json
import os
from pathlib import Path
import pyodbc
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout,
)
from connection import ConnectionSettings, SQLServerConnection, list_odbc_drivers

APP_FOLDER = "SQLServerMonitor"
SETTINGS_FILE = "settings.json"

def settings_file_path() -> Path:
    appdata = os.environ.get("APPDATA")
    folder = Path(appdata) / APP_FOLDER if appdata else Path.home() / f".{APP_FOLDER}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / SETTINGS_FILE

def load_saved_settings() -> ConnectionSettings:
    try:
        data = json.loads(settings_file_path().read_text(encoding="utf-8"))
        allowed = {"server", "database", "driver", "authentication", "username",
                   "trust_server_certificate", "encrypt", "timeout_seconds"}
        return ConnectionSettings(**{k: v for k, v in data.items() if k in allowed})
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ConnectionSettings()

def save_settings(settings: ConnectionSettings) -> None:
    data = {
        "server": settings.server, "database": settings.database,
        "driver": settings.driver, "authentication": settings.authentication,
        "username": settings.username,
        "trust_server_certificate": settings.trust_server_certificate,
        "encrypt": settings.encrypt, "timeout_seconds": settings.timeout_seconds,
    }
    settings_file_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

class ConnectionDialog(QDialog):
    def __init__(self, parent=None, initial_settings=None):
        super().__init__(parent)
        self.setWindowTitle("SQL Server Connection")
        self.setMinimumWidth(510)
        self.connection_settings = None
        initial_settings = initial_settings or load_saved_settings()

        layout = QVBoxLayout(self)
        intro = QLabel("Connect directly to SQL Server. Settings are saved for your Windows user; passwords are never saved.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        conn_group = QGroupBox("Connection")
        conn_form = QFormLayout(conn_group)
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText(r"e.g. localhost\MSSQLSERVER01 or server01,1433")
        conn_form.addRow("Server / instance:", self.server_edit)
        self.database_edit = QLineEdit("master")
        conn_form.addRow("Database:", self.database_edit)
        self.driver_combo = QComboBox()
        self.driver_combo.setEditable(True)
        conn_form.addRow("ODBC driver:", self.driver_combo)
        layout.addWidget(conn_group)

        auth_group = QGroupBox("Authentication")
        auth_form = QFormLayout(auth_group)
        self.auth_combo = QComboBox()
        self.auth_combo.addItem("Windows Authentication", "windows")
        self.auth_combo.addItem("SQL Server Authentication", "sql")
        auth_form.addRow("Authentication:", self.auth_combo)
        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("SQL Server login")
        auth_form.addRow("Username:", self.username_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Enter password (not saved)")
        auth_form.addRow("Password:", self.password_edit)
        layout.addWidget(auth_group)

        options_group = QGroupBox("Connection options")
        options_form = QFormLayout(options_group)
        self.encrypt_combo = QComboBox()
        self.encrypt_combo.addItem("Yes (recommended)", "yes")
        self.encrypt_combo.addItem("No", "no")
        self.encrypt_combo.addItem("Strict (if supported)", "strict")
        options_form.addRow("Encrypt:", self.encrypt_combo)
        self.trust_certificate_check = QCheckBox("Trust server certificate (skip certificate validation)")
        self.trust_certificate_check.setChecked(True)
        options_form.addRow("", self.trust_certificate_check)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 120)
        self.timeout_spin.setValue(5)
        self.timeout_spin.setSuffix(" seconds")
        options_form.addRow("Login timeout:", self.timeout_spin)
        layout.addWidget(options_group)

        self.status_label = QLabel("Connection has not been tested.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test_connection)
        buttons.addWidget(self.test_button)
        buttons.addStretch(1)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        self.button_box.accepted.connect(self._accept_settings)
        self.button_box.rejected.connect(self.reject)
        buttons.addWidget(self.button_box)
        layout.addLayout(buttons)

        self.auth_combo.currentIndexChanged.connect(self._update_enabled_controls)
        self._load_drivers(initial_settings.driver)
        self._apply_settings(initial_settings)
        self._update_enabled_controls()

    def _load_drivers(self, preferred=""):
        self.driver_combo.clear()
        self.driver_combo.addItems(list_odbc_drivers())
        if preferred and self.driver_combo.findText(preferred) < 0:
            self.driver_combo.addItem(preferred)
        if preferred:
            self.driver_combo.setCurrentText(preferred)

    def _apply_settings(self, settings):
        self.server_edit.setText(settings.server)
        self.database_edit.setText(settings.database or "master")
        if settings.driver:
            self.driver_combo.setCurrentText(settings.driver)
        self.auth_combo.setCurrentIndex(max(0, self.auth_combo.findData(settings.authentication)))
        self.username_edit.setText(settings.username)
        self.password_edit.clear()
        self.encrypt_combo.setCurrentIndex(max(0, self.encrypt_combo.findData(settings.encrypt)))
        self.trust_certificate_check.setChecked(settings.trust_server_certificate)
        self.timeout_spin.setValue(max(1, min(120, settings.timeout_seconds)))

    def _update_enabled_controls(self, *_):
        is_sql = self.auth_combo.currentData() == "sql"
        self.username_edit.setEnabled(is_sql)
        self.password_edit.setEnabled(is_sql)

    def _collect_settings(self):
        return ConnectionSettings(
            server=self.server_edit.text().strip(),
            database=self.database_edit.text().strip() or "master",
            driver=self.driver_combo.currentText().strip(),
            authentication=self.auth_combo.currentData(),
            username=self.username_edit.text().strip(),
            password=self.password_edit.text(),
            trust_server_certificate=self.trust_certificate_check.isChecked(),
            encrypt=self.encrypt_combo.currentData(),
            timeout_seconds=self.timeout_spin.value(),
        )

    def _test_connection(self):
        self.test_button.setEnabled(False)
        self.status_label.setText("Testing connection...")
        try:
            success, message = SQLServerConnection(self._collect_settings()).test_connection()
            self.status_label.setText(("Success: " if success else "Failed: ") + message)
            if success:
                QMessageBox.information(self, "Connection test", message)
            else:
                QMessageBox.warning(self, "Connection test failed", message)
        except (pyodbc.Error, ValueError) as exc:
            self.status_label.setText(f"Failed: {exc}")
            QMessageBox.warning(self, "Connection test failed", str(exc))
        except Exception as exc:
            self.status_label.setText(f"Failed: {exc}")
            QMessageBox.warning(self, "Connection test failed", str(exc))
        finally:
            self.test_button.setEnabled(True)

    def _accept_settings(self):
        settings = self._collect_settings()
        if not settings.server:
            QMessageBox.warning(self, "Missing information", "Enter a SQL Server name.")
            return
        if settings.authentication == "sql" and (not settings.username or not settings.password):
            QMessageBox.warning(self, "Missing information", "Enter both username and password for SQL Server Authentication.")
            return
        try:
            save_settings(settings)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save settings", f"Settings could not be saved:\n{exc}")
        self.connection_settings = settings
        self.accept()
