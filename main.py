"""Application entry point for SQLServerMonitor."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        app_dir = Path(sys._MEIPASS)
    else:
        app_dir = Path(__file__).resolve().parent
    app.setWindowIcon(QIcon(str(app_dir / "Simple_SQL_Server_Monitor.ico")))
    app.setApplicationName("Simple SQL Server Monitor")
    app.setOrganizationName("SQLServerMonitor")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
