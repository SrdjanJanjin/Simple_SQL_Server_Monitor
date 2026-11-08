"""Application entry point for SQLServerMonitor."""
from __future__ import annotations

import sys
from PySide6.QtWidgets import QApplication

from main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Simple SQL Server Monitor")
    app.setOrganizationName("SQLServerMonitor")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
