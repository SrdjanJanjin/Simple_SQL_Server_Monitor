"""Main window for SQLServerMonitor.

Requires connection.py, connection_dialog.py, PySide6 and pyodbc in the same
project directory. All SQL executed by this module is read-only.
"""
from __future__ import annotations

import pyodbc
import re
import sys
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timedelta
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QPainter, QFont, QFontMetrics, QPen, QTextCursor, QTextCharFormat, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QMainWindow,
    QFileDialog,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QScrollArea,
    QFrame,
    QSizePolicy,
    QComboBox,
    QRadioButton,
    QLineEdit,
    QPushButton,
    QButtonGroup,
    QDoubleSpinBox,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QPlainTextEdit,
    QTextEdit,
    QSplitter,
    QCheckBox,
    QDialog,
    QListWidget,
    QListWidgetItem,
)

from connection import ConnectionSettings, SQLServerConnection
from connection_dialog import ConnectionDialog, load_saved_settings


DATABASE_FILES_QUERY = r"""
SELECT
    d.name AS DatabaseName,
    mf.name AS LogicalFileName,
    mf.type_desc AS FileType,
    CAST(mf.size * 8.0 / 1024 AS DECIMAL(18, 2)) AS SizeMB,
    CAST(mf.size * 8.0 / 1024 / 1024 AS DECIMAL(18, 3)) AS SizeGB,
    CASE
        WHEN mf.max_size = -1 THEN 'UNLIMITED'
        WHEN mf.max_size = 0 THEN 'NO GROWTH'
        ELSE CAST(
            CAST(mf.max_size * 8.0 / 1024 AS DECIMAL(18, 2))
            AS VARCHAR(30)
        ) + ' MB'
    END AS MaxSize,
    CASE
        WHEN mf.growth = 0 THEN 'DISABLED'
        WHEN mf.is_percent_growth = 1
            THEN CAST(mf.growth AS VARCHAR(10)) + '%'
        ELSE CAST(
            CAST(mf.growth * 8.0 / 1024 AS DECIMAL(18, 2))
            AS VARCHAR(30)
        ) + ' MB'
    END AS AutoGrowth,
    mf.physical_name AS PhysicalFilePath
FROM sys.master_files AS mf
INNER JOIN sys.databases AS d
    ON d.database_id = mf.database_id
ORDER BY d.name, mf.type_desc, mf.name;
""".strip()


class SortableTableWidgetItem(QTableWidgetItem):
    """Sort by a typed UserRole value when available, otherwise sort text."""

    def __lt__(self, other):
        left_value = self.data(Qt.ItemDataRole.UserRole)
        right_value = other.data(Qt.ItemDataRole.UserRole)
        if left_value is not None and right_value is not None:
            if isinstance(left_value, (int, float, Decimal)) and isinstance(
                right_value, (int, float, Decimal)
            ):
                return float(left_value) < float(right_value)
            try:
                return left_value < right_value
            except (TypeError, ValueError):
                pass
        return self.text().casefold() < other.text().casefold()


class SegmentedBar(QWidget):
    """Simple horizontal stacked bar for database space categories."""

    def __init__(self, segments, parent=None):
        super().__init__(parent)
        self.segments = [(str(label), max(0.0, float(value)), QColor(color))
                         for label, value, color in segments]
        self.setMinimumHeight(30)
        self.setMaximumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        total = sum(value for _, value, _ in self.segments)
        tooltip = []
        for label, value, _ in self.segments:
            pct = (value / total * 100.0) if total else 0.0
            tooltip.append(f"{label}: {value:,.2f} MB ({pct:.1f}%)")
        self.setToolTip("\n".join(tooltip) if tooltip else "No space data")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(0, 2, 0, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E8EAED"))
        painter.drawRoundedRect(bounds, 5, 5)
        total = sum(value for _, value, _ in self.segments)
        if total <= 0 or bounds.width() <= 0:
            painter.end()
            return
        x = bounds.left()
        used_width = 0
        for index, (label, value, color) in enumerate(self.segments):
            if value <= 0:
                continue
            width = (bounds.width() - used_width) if index == len(self.segments) - 1 else round(bounds.width() * value / total)
            width = max(0, min(width, bounds.right() - x + 1))
            if width <= 0:
                continue
            segment_rect = bounds.adjusted(0, 0, 0, 0)
            segment_rect.setLeft(x)
            segment_rect.setWidth(width)
            painter.setBrush(color)
            painter.drawRect(segment_rect)
            if width >= 76:
                pct = value / total * 100.0
                text = f"{label} {pct:.1f}%"
                painter.setPen(QColor("#FFFFFF") if color.lightness() < 145 else QColor("#202124"))
                painter.drawText(segment_rect.adjusted(5, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
                painter.setPen(Qt.PenStyle.NoPen)
            x += width
            used_width += width
        painter.end()


class ProportionalDatabaseBar(QWidget):
    """Proportional bar split into used/free data and transaction-log space."""

    DATA_USED = "#2878B5"
    DATA_FREE = "#8CC8EE"
    LOG_USED = "#7256A8"
    LOG_FREE = "#C4B5E8"

    def __init__(self, data_mb, data_used_mb, log_mb, log_used_mb, max_total_mb, parent=None):
        super().__init__(parent)
        self.data_mb = max(0.0, float(data_mb or 0.0))
        self.data_used_mb = min(self.data_mb, max(0.0, float(data_used_mb or 0.0)))
        self.data_free_mb = max(0.0, self.data_mb - self.data_used_mb)
        self.log_mb = max(0.0, float(log_mb or 0.0))
        self.log_used_mb = min(self.log_mb, max(0.0, float(log_used_mb or 0.0)))
        self.log_free_mb = max(0.0, self.log_mb - self.log_used_mb)
        self.max_total_mb = max(0.0, float(max_total_mb or 0.0))
        self.setMinimumHeight(24)
        self.setMaximumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        total = self.data_mb + self.log_mb
        self.setToolTip(
            f"Database space used: {self.data_used_mb:,.2f} MB\n"
            f"Database space free: {self.data_free_mb:,.2f} MB\n"
            f"Database files total: {self.data_mb:,.2f} MB\n"
            f"Transaction log used: {self.log_used_mb:,.2f} MB\n"
            f"Transaction log free: {self.log_free_mb:,.2f} MB\n"
            f"Transaction log total: {self.log_mb:,.2f} MB\n"
            f"Total allocated: {total:,.2f} MB"
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(0, 2, 0, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E8EAED"))
        painter.drawRoundedRect(bounds, 4, 4)
        if self.max_total_mb <= 0 or bounds.width() <= 0:
            painter.end()
            return

        scale = bounds.width() / self.max_total_mb
        segments = [
            (self.data_used_mb, self.DATA_USED),
            (self.data_free_mb, self.DATA_FREE),
            (self.log_used_mb, self.LOG_USED),
            (self.log_free_mb, self.LOG_FREE),
        ]
        x = bounds.left()
        for value, color in segments:
            width = max(0, round(value * scale))
            width = min(width, bounds.right() - x + 1)
            if width > 0:
                painter.setBrush(QColor(color))
                painter.drawRect(x, bounds.top(), width, bounds.height())
                x += width
        painter.end()


class JobTimelineWidget(QWidget):
    """Paint SQL Agent job runs as horizontal bars on a time axis."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.runs = []
        self.window_start = None
        self.window_end = None
        self.setMinimumWidth(850)
        self.setMinimumHeight(220)

    def set_data(self, runs, window_start, window_end):
        self.runs = list(runs)
        self.window_start = window_start
        self.window_end = window_end
        self.setMinimumHeight(max(220, 58 + len(self.runs) * 30))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        if self.window_start is None or self.window_end is None:
            painter.drawText(20, 35, "Set the time range and click Refresh.")
            return
        left, right = 245, max(260, self.width() - 24)
        top, row_h = 48, 30
        total_seconds = max(1.0, (self.window_end - self.window_start).total_seconds())
        width = right - left
        painter.setPen(self.palette().text().color())
        painter.drawText(left, 22, self.window_start.strftime("%Y-%m-%d %H:%M:%S"))
        painter.drawText(max(left, right - 155), 22, self.window_end.strftime("%Y-%m-%d %H:%M:%S"))
        from datetime import timedelta
        tick = self.window_start.replace(minute=0, second=0, microsecond=0)
        if tick < self.window_start:
            tick += timedelta(hours=1)
        while tick < self.window_end:
            x = left + (tick - self.window_start).total_seconds() / total_seconds * width
            painter.setPen(QColor(210, 210, 210))
            painter.drawLine(int(x), top - 8, int(x), top + len(self.runs) * row_h)
            # Keep hourly grid lines, but omit crowded hourly text labels.
            tick += timedelta(hours=1)
        for i, run in enumerate(self.runs):
            name, start, end, status = run
            y = top + i * row_h
            painter.setPen(QColor(225, 225, 225))
            painter.drawLine(0, y + row_h - 1, self.width(), y + row_h - 1)
            painter.setPen(self.palette().text().color())
            label = name if len(name) <= 34 else name[:31] + "..."
            painter.drawText(8, y + 19, label)
            clipped_start = max(start, self.window_start)
            clipped_end = min(end, self.window_end)
            x1 = left + (clipped_start - self.window_start).total_seconds() / total_seconds * width
            x2 = left + (clipped_end - self.window_start).total_seconds() / total_seconds * width
            if x2 <= x1:
                x2 = x1 + 2
            color = QColor(76, 150, 90) if status == 1 else QColor(195, 75, 65)
            if status == 3:
                color = QColor(210, 160, 55)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(int(x1), y + 4, max(3, int(x2 - x1)), 21, 4, 4)
            caption = f"{start:%H:%M:%S}–{end:%H:%M:%S}"
            painter.setPen(QColor(255, 255, 255) if x2 - x1 > 120 else self.palette().text().color())
            painter.drawText(int(x1 + 5) if x2 - x1 > 120 else int(x2 + 5), y + 19, caption)
        if not self.runs:
            painter.setPen(self.palette().text().color())
            painter.drawText(left, top + 24, "No job runs found in this time range.")


class StepsTimelineWidget(QWidget):
    """Paint job runs with their individual SQL Agent steps on a shared time axis."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs = []
        self.window_start = None
        self.window_end = None
        self.setMinimumWidth(950)
        self.setMinimumHeight(220)

    def set_data(self, jobs, window_start, window_end):
        self.jobs = list(jobs)
        self.window_start = window_start
        self.window_end = window_end
        row_count = sum(1 + len(job.get("steps", [])) for job in self.jobs)
        self.setMinimumHeight(max(220, 58 + row_count * 25))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        if self.window_start is None or self.window_end is None:
            painter.drawText(20, 35, "Set the time range and click Refresh.")
            return
        left, right = 320, max(340, self.width() - 24)
        top, row_h = 48, 25
        total_seconds = max(1.0, (self.window_end - self.window_start).total_seconds())
        width = right - left
        # Keep the hourly grid lines, but omit the crowded timestamp labels.
        from datetime import timedelta
        tick = self.window_start.replace(minute=0, second=0, microsecond=0)
        if tick < self.window_start:
            tick += timedelta(hours=1)
        row_count = sum(1 + len(job.get("steps", [])) for job in self.jobs)
        while tick < self.window_end:
            x = left + (tick - self.window_start).total_seconds() / total_seconds * width
            painter.setPen(QColor(210, 210, 210))
            painter.drawLine(int(x), top - 8, int(x), top + row_count * row_h)
            tick += timedelta(hours=1)

        row = 0
        for job in self.jobs:
            y = top + row * row_h
            painter.setPen(QColor(200, 200, 200))
            painter.drawLine(0, y + row_h - 1, self.width(), y + row_h - 1)
            painter.setPen(self.palette().text().color())
            label = str(job["name"])
            painter.setFont(QFont(painter.font().family(), painter.font().pointSize(), QFont.Weight.Bold))
            painter.drawText(8, y + 17, label[:43] + ("..." if len(label) > 43 else ""))
            painter.setFont(QFont(painter.font().family(), painter.font().pointSize(), QFont.Weight.Normal))
            self._draw_bar(painter, job["start"], job["end"], job["status"], y, row_h, left, width, total_seconds, True)
            row += 1
            for step in job.get("steps", []):
                y = top + row * row_h
                painter.setPen(QColor(230, 230, 230))
                painter.drawLine(0, y + row_h - 1, self.width(), y + row_h - 1)
                painter.setPen(self.palette().text().color())
                label = f"    {step['step_id']}. {step['name']}"
                painter.drawText(8, y + 17, label[:47] + ("..." if len(label) > 47 else ""))
                self._draw_bar(painter, step["start"], step["end"], step["status"], y, row_h, left, width, total_seconds, False)
                row += 1
        if not self.jobs:
            painter.setPen(self.palette().text().color())
            painter.drawText(left, top + 24, "No job runs found in this time range.")

    def _draw_bar(self, painter, start, end, status, y, row_h, left, width, total_seconds, is_job):
        clipped_start = max(start, self.window_start)
        clipped_end = min(end, self.window_end)
        x1 = left + (clipped_start - self.window_start).total_seconds() / total_seconds * width
        x2 = left + (clipped_end - self.window_start).total_seconds() / total_seconds * width
        if x2 <= x1:
            x2 = x1 + 2
        color = QColor(76, 150, 90) if status == 1 else QColor(195, 75, 65)
        if status == 3:
            color = QColor(210, 160, 55)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color if is_job else color.lighter(115))
        painter.drawRoundedRect(int(x1), y + 3, max(3, int(x2 - x1)), row_h - 6, 3, 3)
        caption = f"{start:%H:%M:%S}–{end:%H:%M:%S}"
        painter.setPen(QColor(255, 255, 255) if x2 - x1 > 125 else self.palette().text().color())
        painter.drawText(int(x1 + 4) if x2 - x1 > 125 else int(x2 + 4), y + 17, caption)



class CpuHistoryChart(QWidget):
    """Lightweight CPU history chart drawn with QPainter (no extra dependencies)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(230)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_points(self, points):
        self.points = list(points)
        self.update()

    @staticmethod
    def _draw_hour_markers(painter, points, left, top, plot_w, plot_h, height, text_color):
        """Draw vertical guides at full-hour boundaries across a time series."""
        if len(points) < 2:
            return
        first_time, last_time = points[0][0], points[-1][0]
        if not hasattr(first_time, "replace") or last_time <= first_time:
            return
        hour = first_time.replace(minute=0, second=0, microsecond=0)
        if hour <= first_time:
            from datetime import timedelta
            hour += timedelta(hours=1)
        from datetime import timedelta
        painter.save()
        pen = QPen(QColor("#9AA0A6"))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(1)
        painter.setPen(pen)
        while hour < last_time:
            fraction = (hour - first_time).total_seconds() / (last_time - first_time).total_seconds()
            x = int(left + fraction * plot_w)
            painter.drawLine(x, top, x, top + plot_h)
            painter.setPen(text_color)
            painter.drawText(x + 3, max(12, top - 7), hour.strftime("%H:%M"))
            painter.setPen(pen)
            hour += timedelta(hours=1)
        painter.restore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        width, height = self.width(), self.height()
        left, right, top, bottom = 58, 18, 30, 38
        plot_w, plot_h = max(1, width - left - right), max(1, height - top - bottom)
        text_color = self.palette().text().color()
        grid_color = QColor("#D9DEE5")
        painter.setFont(QFont(self.font().family(), 8))
        painter.setPen(text_color)
        for value in (0, 20, 40, 60, 80, 100):
            y = top + plot_h - (value / 100.0) * plot_h
            painter.setPen(grid_color)
            painter.drawLine(left, int(y), width - right, int(y))
            painter.setPen(text_color)
            painter.drawText(18, int(y + 4), str(value))
        painter.setPen(grid_color)
        painter.drawRect(left, top, plot_w, plot_h)
        painter.setPen(QColor("#858B94"))
        painter.drawLine(left, top + plot_h, left + plot_w, top + plot_h)
        self._draw_hour_markers(painter, self.points, left, top, plot_w, plot_h, height, text_color)

        if not self.points:
            painter.setPen(text_color)
            painter.drawText(left + 10, top + 28, "Click Refresh to load CPU history.")
            return

        # Use the most recent 360 samples to show a longer history.
        points = self.points[-360:]
        series = [
            ("SQL Server", 1, QColor(40, 115, 210)),
            ("Other processes", 2, QColor(225, 135, 40)),
            ("Idle", 3, QColor(70, 160, 95)),
        ]
        n = len(points)
        for label, idx, color in series:
            painter.setPen(QPen(color, 2))
            painter.setBrush(color)
            previous = None
            for i, point in enumerate(points):
                x = left + (i / max(1, n - 1)) * plot_w
                y = top + plot_h - (max(0, min(100, float(point[idx]))) / 100.0) * plot_h
                current = (int(x), int(y))
                if previous is not None:
                    painter.drawLine(previous[0], previous[1], current[0], current[1])
                if n <= 30:
                    painter.drawEllipse(current[0] - 2, current[1] - 2, 4, 4)
                previous = current

        # Time labels for the first and last sample.
        painter.setPen(text_color)
        first_time, last_time = points[0][0], points[-1][0]
        painter.drawText(left, height - 12, first_time.strftime("%H:%M:%S"))
        last_label = last_time.strftime("%H:%M:%S")
        painter.drawText(width - right - painter.fontMetrics().horizontalAdvance(last_label),
                         height - 12, last_label)

        legend_y = height - 12
        legend_x = left + 110
        for label, _, color in series:
            painter.setPen(color)
            painter.drawLine(legend_x, legend_y - 4, legend_x + 18, legend_y - 4)
            painter.setPen(text_color)
            painter.drawText(legend_x + 23, legend_y, label)
            legend_x += 130 if label != "Other processes" else 155


class MemoryHistoryChart(QWidget):
    """Time-series chart of physical memory used, retained for up to four hours."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_points(self, points):
        self.points = list(points)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        width, height = self.width(), self.height()
        left, right, top, bottom = 68, 18, 30, 38
        plot_w, plot_h = max(1, width - left - right), max(1, height - top - bottom)
        text_color = self.palette().text().color()
        grid_color = QColor("#D9DEE5")
        painter.setFont(QFont(self.font().family(), 8))
        if not self.points:
            painter.setPen(text_color)
            painter.drawText(left + 8, top + 25, "Waiting for memory samples...")
            return

        max_gb = max((float(point[1]) for point in self.points), default=1.0)
        total_gb = max((float(point[2]) for point in self.points), default=1.0)
        scale_gb = max(1.0, total_gb, max_gb)
        for fraction in (0, .25, .5, .75, 1):
            value = scale_gb * fraction
            y = top + plot_h - fraction * plot_h
            painter.setPen(grid_color)
            painter.drawLine(left, int(y), width - right, int(y))
            painter.setPen(text_color)
            painter.drawText(8, int(y + 4), f"{value:.0f} GB")
        painter.setPen(grid_color)
        painter.drawRect(left, top, plot_w, plot_h)
        painter.setPen(QColor("#858B94"))
        painter.drawLine(left, top + plot_h, left + plot_w, top + plot_h)
        CpuHistoryChart._draw_hour_markers(
            painter, self.points, left, top, plot_w, plot_h, height, text_color
        )

        points = self.points
        painter.setPen(QColor("#2878B5"))
        painter.setBrush(QColor("#2878B5"))
        previous = None
        n = len(points)
        for i, point in enumerate(points):
            x = left + (i / max(1, n - 1)) * plot_w
            y = top + plot_h - min(1.0, max(0.0, float(point[1]) / scale_gb)) * plot_h
            current = (int(x), int(y))
            if previous is not None:
                painter.drawLine(previous[0], previous[1], current[0], current[1])
            if n <= 30:
                painter.drawEllipse(current[0] - 2, current[1] - 2, 4, 4)
            previous = current

        painter.setPen(text_color)
        first_time, last_time = points[0][0], points[-1][0]
        painter.drawText(left, height - 12, first_time.strftime("%H:%M:%S"))
        last_label = last_time.strftime("%H:%M:%S")
        painter.drawText(width - right - painter.fontMetrics().horizontalAdvance(last_label),
                         height - 12, last_label)



class ConnectionHistoryChart(QWidget):
    """History of active SQL Server connections, sampled by the monitor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(155)
        self.setMaximumHeight(175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_points(self, points):
        self.points = list(points)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        width, height = self.width(), self.height()
        left, right, top, bottom = 58, 14, 30, 34
        plot_w, plot_h = max(1, width-left-right), max(1, height-top-bottom)
        text_color = self.palette().text().color()
        grid_color = QColor("#D9DEE5")
        painter.setFont(QFont(self.font().family(), 8))
        if not self.points:
            painter.setPen(text_color)
            painter.drawText(left + 8, top + 24, "Waiting for connection samples...")
            return

        max_count = max((int(point[1]) for point in self.points), default=0)
        scale = max(5, int(((max_count + 4) // 5) * 5))
        for fraction in (0, .25, .5, .75, 1):
            value = scale * fraction
            y = top + plot_h - fraction * plot_h
            painter.setPen(grid_color)
            painter.drawLine(left, int(y), width-right, int(y))
            painter.setPen(text_color)
            painter.drawText(5, int(y+4), f"{value:.0f}")
        painter.setPen(grid_color)
        painter.drawRect(left, top, plot_w, plot_h)
        painter.setPen(QColor("#858B94"))
        painter.drawLine(left, top + plot_h, left + plot_w, top + plot_h)
        CpuHistoryChart._draw_hour_markers(
            painter, self.points, left, top, plot_w, plot_h, height, text_color
        )

        color = QColor("#2878B5")
        painter.setPen(QPen(color, 2))
        previous = None
        n = len(self.points)
        for i, point in enumerate(self.points):
            x = left + (i / max(1, n-1)) * plot_w
            y = top + plot_h - min(1.0, max(0.0, int(point[1]) / scale)) * plot_h
            current = (int(x), int(y))
            if previous is not None:
                painter.drawLine(previous[0], previous[1], current[0], current[1])
            previous = current

        painter.setPen(text_color)
        first_time, last_time = self.points[0][0], self.points[-1][0]
        painter.drawText(left, height-10, first_time.strftime("%H:%M:%S"))
        last_label = last_time.strftime("%H:%M:%S")
        painter.drawText(width-right-painter.fontMetrics().horizontalAdvance(last_label),
                         height-10, last_label)


class DiskUsageHistoryChart(QWidget):
    """Percentage-used history for each SQL Server database-file volume."""

    PALETTE = [
        "#2878B5", "#D97706", "#2E8B57", "#8B5CF6", "#C2416C",
        "#0891B2", "#A16207", "#64748B", "#DB2777", "#4D7C0F",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.series_colors = {}
        self.setMinimumHeight(155)
        self.setMaximumHeight(175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_points(self, points):
        self.points = list(points)
        names = sorted({name for point in self.points for name in point[1]})
        for name in names:
            if name not in self.series_colors:
                color = self.PALETTE[len(self.series_colors) % len(self.PALETTE)]
                self.series_colors[name] = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        width, height = self.width(), self.height()
        left, right, top, bottom = 58, 14, 30, 48
        plot_w, plot_h = max(1, width-left-right), max(1, height-top-bottom)
        text_color = self.palette().text().color()
        grid_color = QColor("#D9DEE5")
        painter.setFont(QFont(self.font().family(), 8))
        if not self.points:
            painter.setPen(text_color)
            painter.drawText(left + 8, top + 24, "Waiting for disk usage samples...")
            return

        for fraction in (0, .25, .5, .75, 1):
            value = fraction * 100
            y = top + plot_h - fraction * plot_h
            painter.setPen(grid_color)
            painter.drawLine(left, int(y), width-right, int(y))
            painter.setPen(text_color)
            painter.drawText(12, int(y+4), f"{value:.0f}%")
        painter.setPen(grid_color)
        painter.drawRect(left, top, plot_w, plot_h)
        painter.setPen(QColor("#858B94"))
        painter.drawLine(left, top + plot_h, left + plot_w, top + plot_h)
        CpuHistoryChart._draw_hour_markers(
            painter, self.points, left, top, plot_w, plot_h, height, text_color
        )

        for name, color in self.series_colors.items():
            previous = None
            valid = [(i, point[1][name]) for i, point in enumerate(self.points)
                     if name in point[1]]
            for i, value in valid:
                x = left + (i / max(1, len(self.points)-1)) * plot_w
                y = top + plot_h - min(100.0, max(0.0, float(value))) / 100.0 * plot_h
                current = (int(x), int(y))
                if previous is not None:
                    painter.setPen(QPen(color, 2))
                    painter.drawLine(previous[0], previous[1], current[0], current[1])
                previous = current

        painter.setFont(QFont(self.font().family(), 8))
        painter.setPen(text_color)
        first_time, last_time = self.points[0][0], self.points[-1][0]
        painter.drawText(left, height-38, first_time.strftime("%H:%M:%S"))
        last_label = last_time.strftime("%H:%M:%S")
        painter.drawText(width-right-painter.fontMetrics().horizontalAdvance(last_label),
                         height-38, last_label)

        # Compact two-row legend so each volume keeps its own color.
        legend_y = height - 5
        legend_x = left + 92
        max_x = width - right
        row = 0
        for name, color in self.series_colors.items():
            label_width = painter.fontMetrics().horizontalAdvance(name) + 25
            if legend_x + label_width > max_x and legend_x > left:
                row += 1
                legend_x = left + 92
            if row > 1:
                break
            y = legend_y - (1-row) * 15
            painter.setPen(QPen(color, 2))
            painter.drawLine(legend_x, y-4, legend_x+12, y-4)
            painter.setPen(text_color)
            painter.drawText(legend_x+16, y, name)
            legend_x += label_width


class DatabaseHistoryChart(QWidget):
    """Four-series history chart for data/log used and free space of one database."""

    SERIES = [
        ("Data used", 1, QColor("#2878B5")),
        ("Data free", 2, QColor("#8CC8EE")),
        ("Log used", 3, QColor("#D97706")),
        ("Log free", 4, QColor("#F5C27A")),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.setMinimumHeight(112)
        self.setMaximumHeight(132)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_points(self, points):
        self.points = list(points)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        width, height = self.width(), self.height()
        left, right, top, bottom = 62, 18, 28, 36
        plot_w, plot_h = max(1, width - left - right), max(1, height - top - bottom)
        text_color = self.palette().text().color()
        grid_color = QColor("#D9DEE5")
        painter.setFont(QFont(self.font().family(), 8))
        if not self.points:
            painter.setPen(text_color)
            painter.drawText(left + 8, top + 24, "Waiting for database space samples...")
            return

        # The overview's "Total MB" is data file allocation + log file allocation.
        # The history tuple stores those same four used/free components in GB.
        # Sum them rather than relying on a separate value that could be missing
        # in older samples; reserve 2x the currently allocated database size.
        latest = self.points[-1]
        allocated_gb = sum(max(0.0, float(latest[i])) for i in range(1, 5))
        scale_gb = max(0.1, allocated_gb * 2.0)
        for fraction in (0, .25, .5, .75, 1):
            value = scale_gb * fraction
            y = top + plot_h - fraction * plot_h
            painter.setPen(grid_color)
            painter.drawLine(left, int(y), width - right, int(y))
            painter.setPen(text_color)
            painter.drawText(2, int(y + 4), f"{value:.1f} GB")
        painter.setPen(grid_color)
        painter.drawRect(left, top, plot_w, plot_h)
        painter.setPen(QColor("#858B94"))
        painter.drawLine(left, top + plot_h, left + plot_w, top + plot_h)
        CpuHistoryChart._draw_hour_markers(
            painter, self.points, left, top, plot_w, plot_h, height, text_color
        )

        n = len(self.points)
        for label, value_index, color in self.SERIES:
            painter.setPen(QPen(color, 2))
            painter.setBrush(color)
            previous = None
            for i, point in enumerate(self.points):
                x = left + (i / max(1, n - 1)) * plot_w
                y = top + plot_h - min(1.0, max(0.0, float(point[value_index]) / scale_gb)) * plot_h
                current = (int(x), int(y))
                if previous is not None:
                    painter.drawLine(previous[0], previous[1], current[0], current[1])
                previous = current

        painter.setPen(text_color)
        first_time, last_time = self.points[0][0], self.points[-1][0]
        painter.drawText(left, height - 12, first_time.strftime("%H:%M:%S"))
        last_label = last_time.strftime("%H:%M:%S")
        painter.drawText(width - right - painter.fontMetrics().horizontalAdvance(last_label),
                         height - 12, last_label)
        legend_y = height - 12
        legend_x = left + 105
        for label, _, color in self.SERIES:
            painter.setPen(color)
            painter.drawLine(legend_x, legend_y - 4, legend_x + 12, legend_y - 4)
            painter.setPen(text_color)
            painter.drawText(legend_x + 15, legend_y, label)
            legend_x += 83


class MemoryOverviewChart(QWidget):
    """Compact snapshot chart for physical memory and SQL Server process memory."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.total_mb = 0.0
        self.available_mb = 0.0
        self.sql_process_mb = 0.0
        self.state = "Click Refresh to load memory information."
        self.setMinimumHeight(150)
        self.setMaximumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, total_mb, available_mb, sql_process_mb, state=""):
        self.total_mb = max(0.0, float(total_mb or 0.0))
        self.available_mb = max(0.0, min(self.total_mb, float(available_mb or 0.0)))
        self.sql_process_mb = max(0.0, float(sql_process_mb or 0.0))
        self.state = state or "Memory snapshot"
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        text_color = self.palette().text().color()
        grid_color = self.palette().mid().color()
        left, right = 142, 14
        bar_w = max(1, self.width() - left - right)
        rows = [
            ("Physical memory used", max(0.0, self.total_mb - self.available_mb), self.total_mb, QColor("#2878B5")),
            ("SQL Server process", self.sql_process_mb, self.total_mb, QColor("#7256A8")),
            ("Physical memory free", self.available_mb, self.total_mb, QColor("#8CC8EE")),
        ]
        painter.setFont(QFont(self.font().family(), 8))
        if self.total_mb <= 0:
            painter.setPen(text_color)
            painter.drawText(8, 30, self.state)
            return
        for index, (label, value, total, color) in enumerate(rows):
            y = 24 + index * 39
            painter.setPen(text_color)
            painter.drawText(5, y + 14, label)
            painter.setPen(grid_color)
            painter.setBrush(self.palette().alternateBase())
            painter.drawRoundedRect(left, y, bar_w, 20, 4, 4)
            pct = min(1.0, value / total) if total else 0.0
            fill_w = int(bar_w * pct)
            if fill_w > 0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(left, y, fill_w, 20, 4, 4)
            painter.setPen(text_color)
            painter.drawText(left + 5, y + 14, f"{value:,.0f} MB ({pct * 100:.1f}%)")
        painter.setPen(text_color)
        painter.drawText(5, self.height() - 7, self.state)



class HomeScreen(QWidget):
    """Display the welcome dashboard image, scaled without distortion."""

    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self._pixmap = QPixmap(str(image_path))
        self.setMinimumSize(320, 180)
        self.setStyleSheet("background-color: #151a1f;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#151a1f"))
        if not self._pixmap.isNull():
            target = self.rect().adjusted(8, 8, -8, -8)
            scaled = self._pixmap.scaled(
                target.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QColor("#E8EAED"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Simple SQL Server Monitor",
            )
        painter.end()



class MainWindow(QMainWindow):
    """Application main window with menus and a results table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Simple SQL Server Monitor")
        self.resize(1180, 650)
        self.setMinimumSize(760, 420)

        self._db: SQLServerConnection | None = None
        self._settings: ConnectionSettings = load_saved_settings()
        self._jobs_step_search = ""

        self.table = QTableWidget(self)
        self.table.setColumnCount(0)
        self.table.setRowCount(0)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)

        # Table plus summary totals at the bottom.
        central_widget = QWidget(self)
        central_layout = QVBoxLayout(central_widget)
        central_layout.setContentsMargins(8, 8, 8, 8)

        # Welcome screen shown when the application starts.
        app_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        self.home_screen = HomeScreen(
            app_dir / "welcome_dashboard.png",
            central_widget,
        )
        central_layout.addWidget(self.home_screen, 1)

        central_layout.addWidget(self.table, 1)
        self.table.hide()

        totals_layout = QHBoxLayout()
        totals_layout.addStretch(1)
        self.total_gb_label = QLabel("Total GB: 0.000", central_widget)
        self.total_mb_label = QLabel("Total MB: 0.00", central_widget)
        for label in (self.total_gb_label, self.total_mb_label):
            label.setMinimumWidth(150)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            label.setStyleSheet("font-weight: bold; padding: 4px 8px;")
            totals_layout.addWidget(label)
        central_layout.addLayout(totals_layout)
        self.total_gb_label.hide()
        self.total_mb_label.hide()

        # Scrollable visual view used by Database files > Space usage.
        self.space_scroll = QScrollArea(self)
        self.space_scroll.setWidgetResizable(True)
        self.space_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.space_content = QWidget(self.space_scroll)
        self.space_layout = QVBoxLayout(self.space_content)
        self.space_layout.setContentsMargins(4, 4, 4, 4)
        self.space_layout.setSpacing(10)
        self.space_scroll.setWidget(self.space_content)
        self.space_scroll.hide()

        # Programmability view: stored procedures/functions list and SQL definition.
        self.programmability_panel = QWidget(central_widget)
        prog_layout = QVBoxLayout(self.programmability_panel)
        prog_layout.setContentsMargins(4, 4, 4, 4)
        prog_controls = QHBoxLayout()
        prog_controls.addWidget(QLabel("Database:"))
        self.programmability_database_combo = QComboBox(self.programmability_panel)
        self.programmability_database_combo.setMinimumWidth(180)
        prog_controls.addWidget(self.programmability_database_combo)
        self.programmability_refresh_button = QPushButton("Refresh", self.programmability_panel)
        self.programmability_refresh_button.clicked.connect(self.refresh_programmability_objects)
        prog_controls.addWidget(self.programmability_refresh_button)
        self.programmability_procs_check = QCheckBox("Stored Procedures", self.programmability_panel)
        self.programmability_procs_check.setChecked(True)
        self.programmability_funcs_check = QCheckBox("Functions", self.programmability_panel)
        self.programmability_funcs_check.setChecked(True)
        self.programmability_views_check = QCheckBox("Views", self.programmability_panel)
        self.programmability_views_check.setChecked(True)
        self.programmability_triggers_check = QCheckBox("Triggers", self.programmability_panel)
        self.programmability_triggers_check.setChecked(True)
        self.programmability_triggers_check.stateChanged.connect(self._apply_programmability_filters)
        self.programmability_procs_check.stateChanged.connect(self._apply_programmability_filters)
        self.programmability_funcs_check.stateChanged.connect(self._apply_programmability_filters)
        self.programmability_views_check.stateChanged.connect(self._apply_programmability_filters)
        prog_controls.addWidget(self.programmability_procs_check)
        prog_controls.addWidget(self.programmability_funcs_check)
        prog_controls.addWidget(self.programmability_views_check)
        prog_controls.addWidget(self.programmability_triggers_check)
        prog_controls.addWidget(QLabel("Search in code:"))
        self.programmability_search_edit = QLineEdit(self.programmability_panel)
        self.programmability_search_edit.setPlaceholderText("e.g. SalesTable")
        self.programmability_search_edit.setMinimumWidth(300)
        self.programmability_search_edit.setMaximumWidth(380)
        self.programmability_search_edit.returnPressed.connect(self._apply_programmability_filters)
        prog_controls.addWidget(self.programmability_search_edit)
        self.programmability_search_button = QPushButton("Search", self.programmability_panel)
        self.programmability_search_button.clicked.connect(self._apply_programmability_filters)
        prog_controls.addWidget(self.programmability_search_button)
        prog_controls.addStretch(1)
        prog_layout.addLayout(prog_controls)
        self.programmability_count_label = QLabel("0 programmable object(s)", self.programmability_panel)
        self.programmability_count_label.setStyleSheet("font-weight: bold; padding: 4px 8px;")
        prog_layout.addWidget(self.programmability_count_label)
        prog_splitter = QSplitter(Qt.Orientation.Horizontal, self.programmability_panel)
        self.programmability_list = QTableWidget(prog_splitter)
        self.programmability_list.setColumnCount(5)
        self.programmability_list.setHorizontalHeaderLabels(["Type", "Schema", "Name", "Created", "Last Modified"])
        self.programmability_list.setAlternatingRowColors(True)
        self.programmability_list.setSortingEnabled(True)
        self.programmability_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.programmability_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.programmability_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.programmability_list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.programmability_list.horizontalHeader().setStretchLastSection(True)
        self.programmability_list.itemSelectionChanged.connect(self._on_programmability_selection_changed)
        self.programmability_sql_text = QPlainTextEdit(prog_splitter)
        self.programmability_sql_text.setReadOnly(True)
        self.programmability_sql_text.setPlaceholderText("Select a stored procedure, function, view, or trigger to view its SQL definition.")
        self.programmability_sql_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        prog_splitter.addWidget(self.programmability_list)
        prog_splitter.addWidget(self.programmability_sql_text)
        prog_splitter.setStretchFactor(0, 3)
        prog_splitter.setStretchFactor(1, 2)
        prog_layout.addWidget(prog_splitter, 1)
        self.programmability_panel.hide()
        central_layout.addWidget(self.programmability_panel, 1)

        # View Structure: recursively show the tables and nested views referenced by a view.
        self.view_structure_panel = QWidget(central_widget)
        view_structure_layout = QVBoxLayout(self.view_structure_panel)
        view_structure_layout.setContentsMargins(4, 4, 4, 4)
        view_structure_controls = QHBoxLayout()
        view_structure_controls.addWidget(QLabel("Database:"))
        self.view_structure_database_combo = QComboBox(self.view_structure_panel)
        self.view_structure_database_combo.setMinimumWidth(180)
        self.view_structure_database_combo.currentTextChanged.connect(self._load_view_structure_names)
        view_structure_controls.addWidget(self.view_structure_database_combo)
        view_structure_controls.addWidget(QLabel("Search views:"))
        self.view_structure_search = QLineEdit(self.view_structure_panel)
        self.view_structure_search.setPlaceholderText("Filter by view name or schema")
        self.view_structure_search.setClearButtonEnabled(True)
        self.view_structure_search.textChanged.connect(self._filter_view_structure_names)
        view_structure_controls.addWidget(self.view_structure_search, 1)
        self.view_structure_refresh_button = QPushButton("Refresh", self.view_structure_panel)
        self.view_structure_refresh_button.clicked.connect(self._refresh_view_structure)
        view_structure_controls.addWidget(self.view_structure_refresh_button)
        view_structure_layout.addLayout(view_structure_controls)
        view_structure_splitter = QSplitter(Qt.Orientation.Horizontal, self.view_structure_panel)
        self.view_structure_list = QTableWidget(view_structure_splitter)
        self.view_structure_list.setColumnCount(2)
        self.view_structure_list.setHorizontalHeaderLabels(["Schema", "View"])
        self.view_structure_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view_structure_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view_structure_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view_structure_list.setAlternatingRowColors(True)
        self.view_structure_list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.view_structure_list.horizontalHeader().setStretchLastSection(True)
        self.view_structure_list.verticalHeader().setVisible(False)
        self.view_structure_list.itemSelectionChanged.connect(self._show_selected_view_structure)
        self.view_structure_tree = QTreeWidget(view_structure_splitter)
        self.view_structure_tree.setHeaderLabels(["View dependency structure"])
        self.view_structure_tree.setAlternatingRowColors(True)
        self.view_structure_tree.setUniformRowHeights(True)
        view_structure_splitter.addWidget(self.view_structure_list)
        view_structure_splitter.addWidget(self.view_structure_tree)
        view_structure_splitter.setStretchFactor(0, 1)
        view_structure_splitter.setStretchFactor(1, 2)
        view_structure_layout.addWidget(view_structure_splitter, 1)
        self.view_structure_status = QLabel("Select a database and a view.", self.view_structure_panel)
        view_structure_layout.addWidget(self.view_structure_status)
        self.view_structure_panel.hide()
        central_layout.addWidget(self.view_structure_panel, 1)

        self.central_widget = central_widget
        self.central_layout = central_layout
        central_layout.addWidget(self.space_scroll, 1)

        # Table-size view with database/filter controls and results.
        self.tables_panel = QWidget(central_widget)
        tables_layout = QVBoxLayout(self.tables_panel)
        tables_layout.setContentsMargins(4, 4, 4, 4)
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Database:"))
        self.tables_database_combo = QComboBox(self.tables_panel)
        self.tables_database_combo.setMinimumWidth(180)
        controls_layout.addWidget(self.tables_database_combo)

        self.tables_limit_group = QButtonGroup(self.tables_panel)
        self.tables_top50_radio = QRadioButton("Top 50", self.tables_panel)
        self.tables_all_radio = QRadioButton("All", self.tables_panel)
        self.tables_top50_radio.setChecked(True)
        self.tables_limit_group.addButton(self.tables_top50_radio)
        self.tables_limit_group.addButton(self.tables_all_radio)
        controls_layout.addWidget(self.tables_top50_radio)
        controls_layout.addWidget(self.tables_all_radio)

        controls_layout.addWidget(QLabel("Contains text:"))
        self.tables_contains_edit = QLineEdit(self.tables_panel)
        self.tables_contains_edit.setPlaceholderText("Optional table-name filter")
        self.tables_contains_edit.setClearButtonEnabled(True)
        controls_layout.addWidget(self.tables_contains_edit, 1)

        controls_layout.addWidget(QLabel("Tables larger than MB:"))
        self.tables_min_size_spin = QDoubleSpinBox(self.tables_panel)
        self.tables_min_size_spin.setRange(0.0, 999999999.99)
        self.tables_min_size_spin.setDecimals(2)
        self.tables_min_size_spin.setSingleStep(100.0)
        self.tables_min_size_spin.setSpecialValueText("No filter")
        self.tables_min_size_spin.setToolTip(
            "Show only tables whose reserved size is greater than this value. Set to 0 to disable."
        )
        self.tables_min_size_spin.setMinimumWidth(115)
        controls_layout.addWidget(self.tables_min_size_spin)

        self.tables_refresh_button = QPushButton("Refresh", self.tables_panel)
        self.tables_refresh_button.clicked.connect(self.refresh_tables_size)
        controls_layout.addWidget(self.tables_refresh_button)
        tables_layout.addLayout(controls_layout)

        self.tables_result = QTableWidget(self.tables_panel)
        self.tables_result.setAlternatingRowColors(True)
        self.tables_result.setSortingEnabled(True)
        self.tables_result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tables_result.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tables_result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tables_result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tables_result.horizontalHeader().setStretchLastSection(False)
        self.tables_result.verticalHeader().setVisible(False)
        self.tables_result.itemSelectionChanged.connect(self._on_table_list_selection_changed)

        # Top area: compact table list on the left and selected-table columns on the right.
        tables_top_splitter = QSplitter(Qt.Orientation.Horizontal, self.tables_panel)
        tables_top_splitter.addWidget(self.tables_result)

        columns_panel = QWidget(tables_top_splitter)
        columns_layout = QVBoxLayout(columns_panel)
        columns_layout.setContentsMargins(2, 0, 0, 0)
        self.table_columns_label = QLabel("Columns and data types", columns_panel)
        self.table_columns_label.setStyleSheet("font-weight: bold; padding: 4px 6px;")
        columns_layout.addWidget(self.table_columns_label)
        self.table_columns_filter = QLineEdit(columns_panel)
        self.table_columns_filter.setPlaceholderText("Filter columns by name...")
        self.table_columns_filter.setClearButtonEnabled(True)
        self.table_columns_filter.textChanged.connect(self._filter_table_columns)
        columns_layout.addWidget(self.table_columns_filter)
        self.table_columns_result = QTableWidget(columns_panel)
        self.table_columns_result.setColumnCount(2)
        self.table_columns_result.setHorizontalHeaderLabels(["Column", "Data type"])
        self.table_columns_result.setAlternatingRowColors(True)
        self.table_columns_result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_columns_result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_columns_result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_columns_result.horizontalHeader().setStretchLastSection(True)
        self.table_columns_result.verticalHeader().setVisible(False)
        columns_layout.addWidget(self.table_columns_result, 1)
        tables_top_splitter.addWidget(columns_panel)
        tables_top_splitter.setStretchFactor(0, 3)
        tables_top_splitter.setStretchFactor(1, 2)
        tables_top_splitter.setSizes([700, 430])

        tables_splitter = QSplitter(Qt.Orientation.Vertical, self.tables_panel)
        tables_splitter.addWidget(tables_top_splitter)

        preview_panel = QWidget(tables_splitter)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 2, 0, 0)
        self.table_preview_label = QLabel("Select a table above to preview its first 10 rows.", preview_panel)
        self.table_preview_label.setStyleSheet("font-weight: bold; padding: 4px 6px;")
        preview_layout.addWidget(self.table_preview_label)
        self.table_preview_result = QTableWidget(preview_panel)
        self.table_preview_result.setAlternatingRowColors(True)
        self.table_preview_result.setSortingEnabled(False)
        self.table_preview_result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_preview_result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_preview_result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_preview_result.horizontalHeader().setStretchLastSection(True)
        self.table_preview_result.verticalHeader().setVisible(False)
        preview_layout.addWidget(self.table_preview_result, 1)
        tables_splitter.addWidget(preview_panel)
        tables_splitter.setStretchFactor(0, 3)
        tables_splitter.setStretchFactor(1, 2)
        tables_layout.addWidget(tables_splitter, 1)

        self.tables_total_mb_label = QLabel("Total size: 0.00 MB", self.tables_panel)
        self.tables_total_mb_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tables_total_mb_label.setStyleSheet("font-weight: bold; padding: 4px 8px;")
        tables_layout.addWidget(self.tables_total_mb_label)
        self.tables_panel.hide()
        central_layout.addWidget(self.tables_panel, 1)

        # Tables-with-triggers view: table hierarchy on the left, SQL definition on the right.
        self.triggers_panel = QWidget(central_widget)
        triggers_layout = QVBoxLayout(self.triggers_panel)
        triggers_layout.setContentsMargins(4, 4, 4, 4)
        trigger_controls = QHBoxLayout()
        trigger_controls.addWidget(QLabel("Database:"))
        self.triggers_database_combo = QComboBox(self.triggers_panel)
        self.triggers_database_combo.setMinimumWidth(180)
        trigger_controls.addWidget(self.triggers_database_combo)
        self.triggers_refresh_button = QPushButton("Refresh", self.triggers_panel)
        self.triggers_refresh_button.clicked.connect(self.refresh_tables_with_triggers)
        trigger_controls.addWidget(self.triggers_refresh_button)
        trigger_controls.addStretch(1)
        triggers_layout.addLayout(trigger_controls)
        triggers_splitter = QSplitter(Qt.Orientation.Horizontal, self.triggers_panel)
        self.triggers_tree = QTreeWidget(triggers_splitter)
        self.triggers_tree.setHeaderLabels(["Tables and triggers"])
        self.triggers_tree.setMinimumWidth(280)
        self.triggers_tree.currentItemChanged.connect(self._on_trigger_tree_selection_changed)
        self.trigger_sql_text = QPlainTextEdit(triggers_splitter)
        self.trigger_sql_text.setReadOnly(True)
        self.trigger_sql_text.setPlaceholderText("Select a trigger to view its SQL definition.")
        self.trigger_sql_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        triggers_splitter.addWidget(self.triggers_tree)
        triggers_splitter.addWidget(self.trigger_sql_text)
        triggers_splitter.setStretchFactor(0, 1)
        triggers_splitter.setStretchFactor(1, 2)
        triggers_layout.addWidget(triggers_splitter, 1)
        self.triggers_panel.hide()
        central_layout.addWidget(self.triggers_panel, 1)

        # Tables-with-indexes view: table/index hierarchy on the left, index properties on the right.
        self.indexes_panel = QWidget(central_widget)
        indexes_layout = QVBoxLayout(self.indexes_panel)
        indexes_layout.setContentsMargins(4, 4, 4, 4)
        index_controls = QHBoxLayout()
        index_controls.addWidget(QLabel("Database:"))
        self.indexes_database_combo = QComboBox(self.indexes_panel)
        self.indexes_database_combo.setMinimumWidth(180)
        index_controls.addWidget(self.indexes_database_combo)
        self.indexes_refresh_button = QPushButton("Refresh", self.indexes_panel)
        self.indexes_refresh_button.clicked.connect(self.refresh_tables_with_indexes)
        index_controls.addWidget(self.indexes_refresh_button)
        index_controls.addWidget(QLabel("Table contains:"))
        self.indexes_table_filter = QLineEdit(self.indexes_panel)
        self.indexes_table_filter.setPlaceholderText("e.g. Sales")
        self.indexes_table_filter.setMaximumWidth(180)
        self.indexes_table_filter.textChanged.connect(self._apply_index_filters)
        index_controls.addWidget(self.indexes_table_filter)
        index_controls.addWidget(QLabel("Index larger than MB:"))
        self.indexes_size_filter = QDoubleSpinBox(self.indexes_panel)
        self.indexes_size_filter.setRange(0, 1000000000)
        self.indexes_size_filter.setDecimals(2)
        self.indexes_size_filter.setSingleStep(1.0)
        self.indexes_size_filter.setSuffix(" MB")
        self.indexes_size_filter.setToolTip("Show only indexes larger than this size; 0 disables the size filter.")
        self.indexes_size_filter.setMaximumWidth(130)
        self.indexes_size_filter.valueChanged.connect(self._apply_index_filters)
        index_controls.addWidget(self.indexes_size_filter)
        index_controls.addStretch(1)
        indexes_layout.addLayout(index_controls)
        self.indexes_total_label = QLabel("Total index size: 0.00 MB")
        self.indexes_total_label.setStyleSheet("font-weight: bold; padding: 4px 8px;")
        indexes_layout.addWidget(self.indexes_total_label)
        indexes_splitter = QSplitter(Qt.Orientation.Horizontal, self.indexes_panel)
        self.indexes_tree = QTreeWidget(indexes_splitter)
        self.indexes_tree.setHeaderLabels(["Tables and indexes"])
        self.indexes_tree.setMinimumWidth(280)
        self.indexes_tree.currentItemChanged.connect(self._on_index_tree_selection_changed)
        self.indexes_list = QTableWidget(indexes_splitter)
        self.indexes_list.setColumnCount(5)
        self.indexes_list.setHorizontalHeaderLabels(["Schema", "Table", "Index", "Type", "Size MB"])
        self.indexes_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.indexes_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.indexes_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.indexes_list.setSortingEnabled(True)
        self.indexes_list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.indexes_list.horizontalHeader().setStretchLastSection(True)
        self.indexes_list.setMinimumWidth(420)
        self.indexes_list.itemSelectionChanged.connect(self._on_index_list_selection_changed)
        self.index_details_text = QPlainTextEdit(indexes_splitter)
        self.index_details_text.setReadOnly(True)
        self.index_details_text.setPlaceholderText("Select an index to view its properties.")
        self.index_details_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        indexes_splitter.addWidget(self.indexes_tree)
        indexes_splitter.addWidget(self.indexes_list)
        indexes_splitter.addWidget(self.index_details_text)
        indexes_splitter.setStretchFactor(0, 1)
        indexes_splitter.setStretchFactor(1, 2)
        indexes_splitter.setStretchFactor(2, 2)
        indexes_layout.addWidget(indexes_splitter, 1)
        self.indexes_panel.hide()
        self.programmability_panel.hide()
        central_layout.addWidget(self.indexes_panel, 1)

        # Server overview shown after a successful connection.
        self.server_info_panel = QWidget(central_widget)
        server_info_layout = QVBoxLayout(self.server_info_panel)
        server_info_layout.setContentsMargins(18, 18, 18, 18)
        server_info_title = QLabel("SQL Server information", self.server_info_panel)
        server_info_title.setStyleSheet("font-size: 18px; font-weight: bold; padding: 4px;")
        server_info_layout.addWidget(server_info_title)
        self.server_info_table = QTableWidget(self.server_info_panel)
        self.server_info_table.setColumnCount(2)
        self.server_info_table.setHorizontalHeaderLabels(["Property", "Value"])
        self.server_info_table.setAlternatingRowColors(True)
        self.server_info_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.server_info_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.server_info_table.verticalHeader().setVisible(False)
        self.server_info_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.server_info_table.horizontalHeader().setStretchLastSection(True)
        server_info_layout.addWidget(self.server_info_table)
        self.server_info_panel.hide()
        central_layout.addWidget(self.server_info_panel, 1)

        # SQL Server Agent jobs view: refresh, jobs/schedules, details and steps.
        self.jobs_panel = QWidget(central_widget)
        jobs_layout = QVBoxLayout(self.jobs_panel)
        jobs_layout.setContentsMargins(4, 4, 4, 4)
        jobs_controls = QHBoxLayout()
        self.jobs_controls = jobs_controls
        self.jobs_refresh_button = QPushButton("Refresh", self.jobs_panel)
        self.jobs_refresh_button.clicked.connect(self.refresh_jobs_list)
        jobs_controls.addWidget(self.jobs_refresh_button)
        self.jobs_step_search_edit = QLineEdit(self.jobs_panel)
        self.jobs_step_search_edit.setPlaceholderText("Search text in job steps (table, procedure, SQL text)...")
        self.jobs_step_search_edit.setClearButtonEnabled(True)
        self.jobs_step_search_edit.returnPressed.connect(self.search_job_steps)
        jobs_controls.addWidget(self.jobs_step_search_edit, 1)
        self.jobs_step_search_button = QPushButton("Search steps", self.jobs_panel)
        self.jobs_step_search_button.clicked.connect(self.search_job_steps)
        jobs_controls.addWidget(self.jobs_step_search_button)
        self.jobs_step_search_clear_button = QPushButton("Clear search", self.jobs_panel)
        self.jobs_step_search_clear_button.clicked.connect(self.clear_job_step_search)
        jobs_controls.addWidget(self.jobs_step_search_clear_button)
        jobs_layout.addLayout(jobs_controls)

        # Shared controls for failed/completed job history views.
        self.job_history_controls = QWidget(self.jobs_panel)
        history_controls_layout = QHBoxLayout(self.job_history_controls)
        history_controls_layout.setContentsMargins(2, 2, 2, 2)
        history_controls_layout.addWidget(QLabel("Last:", self.job_history_controls))
        self.job_history_hours_spin = QSpinBox(self.job_history_controls)
        self.job_history_hours_spin.setRange(1, 720)
        self.job_history_hours_spin.setValue(24)
        self.job_history_hours_spin.setSuffix(" h")
        history_controls_layout.addWidget(self.job_history_hours_spin)
        self.job_history_refresh_button = QPushButton("Refresh", self.job_history_controls)
        self.job_history_refresh_button.clicked.connect(self._refresh_current_job_history)
        history_controls_layout.addWidget(self.job_history_refresh_button)
        history_controls_layout.addStretch(1)
        self.job_history_controls.hide()
        jobs_layout.addWidget(self.job_history_controls)

        self.job_history_content_splitter = QSplitter(Qt.Orientation.Horizontal, self.jobs_panel)
        self.job_history_list_panel = QWidget(self.job_history_content_splitter)
        history_list_layout = QVBoxLayout(self.job_history_list_panel)
        history_list_layout.setContentsMargins(2, 2, 6, 2)
        history_list_layout.setSpacing(4)
        history_list_layout.addWidget(QLabel("Jobs to display:", self.job_history_list_panel))
        history_list_buttons = QHBoxLayout()
        self.job_history_select_all_button = QPushButton("All", self.job_history_list_panel)
        self.job_history_select_none_button = QPushButton("None", self.job_history_list_panel)
        self.job_history_select_all_button.clicked.connect(lambda: self._set_job_history_checks(True))
        self.job_history_select_none_button.clicked.connect(lambda: self._set_job_history_checks(False))
        history_list_buttons.addWidget(self.job_history_select_all_button)
        history_list_buttons.addWidget(self.job_history_select_none_button)
        history_list_layout.addLayout(history_list_buttons)
        self.job_history_job_list = QListWidget(self.job_history_list_panel)
        self.job_history_job_list.setMinimumWidth(180)
        self.job_history_job_list.setMaximumWidth(300)
        self.job_history_job_list.itemChanged.connect(self._on_job_history_item_changed)
        history_list_layout.addWidget(self.job_history_job_list, 1)

        jobs_splitter = QSplitter(Qt.Orientation.Vertical, self.jobs_panel)
        self.jobs_splitter = jobs_splitter
        self.jobs_result = QTableWidget(jobs_splitter)
        self.jobs_result.setAlternatingRowColors(True)
        self.jobs_result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_result.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs_result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_result.setSortingEnabled(True)
        self.jobs_result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.jobs_result.horizontalHeader().setStretchLastSection(True)
        self.jobs_result.verticalHeader().setVisible(False)
        self.jobs_result.itemSelectionChanged.connect(self._on_job_selection_changed)
        jobs_splitter.addWidget(self.jobs_result)

        jobs_bottom_splitter = QSplitter(Qt.Orientation.Horizontal, jobs_splitter)
        jobs_details_panel = QWidget(jobs_bottom_splitter)
        jobs_details_layout = QVBoxLayout(jobs_details_panel)
        jobs_details_layout.setContentsMargins(2, 2, 2, 2)
        jobs_details_title = QLabel("Job / Schedule details", jobs_details_panel)
        self.jobs_details_title = jobs_details_title
        jobs_details_title.setStyleSheet("font-weight: bold; padding: 4px;")
        jobs_details_layout.addWidget(jobs_details_title)
        self.jobs_details_text = QPlainTextEdit(jobs_details_panel)
        self.jobs_details_text.setReadOnly(True)
        self.jobs_details_text.setPlaceholderText("Select a job or schedule above to view details.")
        jobs_details_layout.addWidget(self.jobs_details_text, 1)

        jobs_steps_panel = QWidget(jobs_bottom_splitter)
        self.jobs_steps_panel = jobs_steps_panel
        jobs_steps_layout = QVBoxLayout(jobs_steps_panel)
        jobs_steps_layout.setContentsMargins(2, 2, 2, 2)
        jobs_steps_title = QLabel("Job steps", jobs_steps_panel)
        jobs_steps_title.setStyleSheet("font-weight: bold; padding: 4px;")
        jobs_steps_layout.addWidget(jobs_steps_title)
        self.jobs_steps_result = QTableWidget(jobs_steps_panel)
        self.jobs_steps_result.setColumnCount(4)
        self.jobs_steps_result.setHorizontalHeaderLabels(["Step", "Step Name", "Subsystem", "Command"])
        self.jobs_steps_result.setAlternatingRowColors(True)
        self.jobs_steps_result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_steps_result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_steps_result.setWordWrap(False)
        self.jobs_steps_result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.jobs_steps_result.horizontalHeader().setStretchLastSection(True)
        self.jobs_steps_result.verticalHeader().setVisible(False)
        jobs_steps_layout.addWidget(self.jobs_steps_result, 1)
        jobs_bottom_splitter.addWidget(jobs_details_panel)
        jobs_bottom_splitter.addWidget(jobs_steps_panel)
        jobs_bottom_splitter.setStretchFactor(0, 1)
        jobs_bottom_splitter.setStretchFactor(1, 2)
        jobs_splitter.addWidget(jobs_bottom_splitter)
        self.jobs_bottom_splitter = jobs_bottom_splitter
        jobs_splitter.setStretchFactor(0, 3)
        jobs_splitter.setStretchFactor(1, 2)
        self.job_history_content_splitter.addWidget(self.job_history_list_panel)
        self.job_history_content_splitter.addWidget(jobs_splitter)
        self.job_history_content_splitter.setStretchFactor(0, 0)
        self.job_history_content_splitter.setStretchFactor(1, 1)
        self.job_history_content_splitter.setSizes([220, 1050])
        jobs_layout.addWidget(self.job_history_content_splitter, 1)
        self.job_history_list_panel.hide()
        self.jobs_panel.hide()
        central_layout.addWidget(self.jobs_panel, 1)

        self.timeline_panel = QWidget(central_widget)
        timeline_layout = QVBoxLayout(self.timeline_panel)
        timeline_layout.setContentsMargins(4, 4, 4, 4)
        timeline_controls = QHBoxLayout()
        timeline_controls.addWidget(QLabel("Last:", self.timeline_panel))
        self.timeline_hours_spin = QSpinBox(self.timeline_panel)
        self.timeline_hours_spin.setRange(1, 720)
        self.timeline_hours_spin.setValue(24)
        self.timeline_hours_spin.setSuffix(" h")
        timeline_controls.addWidget(self.timeline_hours_spin)
        self.timeline_refresh_button = QPushButton("Refresh", self.timeline_panel)
        self.timeline_refresh_button.clicked.connect(self.refresh_jobs_timeline)
        timeline_controls.addWidget(self.timeline_refresh_button)
        timeline_controls.addStretch(1)
        timeline_layout.addLayout(timeline_controls)
        self.timeline_status_label = QLabel("Green = succeeded, red = failed, yellow = cancelled.", self.timeline_panel)
        timeline_layout.addWidget(self.timeline_status_label)
        timeline_content_splitter = QSplitter(Qt.Orientation.Horizontal, self.timeline_panel)
        self.timeline_job_list_panel = QWidget(timeline_content_splitter)
        timeline_job_list_layout = QVBoxLayout(self.timeline_job_list_panel)
        timeline_job_list_layout.setContentsMargins(2, 2, 6, 2)
        timeline_job_list_layout.setSpacing(4)
        timeline_job_list_layout.addWidget(QLabel("Jobs to display:", self.timeline_job_list_panel))
        timeline_job_list_buttons = QHBoxLayout()
        self.timeline_select_all_button = QPushButton("All", self.timeline_job_list_panel)
        self.timeline_select_none_button = QPushButton("None", self.timeline_job_list_panel)
        self.timeline_select_all_button.clicked.connect(lambda: self._set_timeline_job_checks(True))
        self.timeline_select_none_button.clicked.connect(lambda: self._set_timeline_job_checks(False))
        timeline_job_list_buttons.addWidget(self.timeline_select_all_button)
        timeline_job_list_buttons.addWidget(self.timeline_select_none_button)
        timeline_job_list_layout.addLayout(timeline_job_list_buttons)
        self.timeline_job_list = QListWidget(self.timeline_job_list_panel)
        self.timeline_job_list.setMinimumWidth(190)
        self.timeline_job_list.setMaximumWidth(310)
        self.timeline_job_list.itemChanged.connect(self._on_timeline_job_item_changed)
        timeline_job_list_layout.addWidget(self.timeline_job_list, 1)
        timeline_content_splitter.addWidget(self.timeline_job_list_panel)

        self.timeline_scroll = QScrollArea(self.timeline_panel)
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.timeline_chart = JobTimelineWidget()
        self.timeline_scroll.setWidget(self.timeline_chart)
        timeline_content_splitter.addWidget(self.timeline_scroll)
        timeline_content_splitter.setStretchFactor(0, 0)
        timeline_content_splitter.setStretchFactor(1, 1)
        timeline_content_splitter.setSizes([230, 900])
        timeline_layout.addWidget(timeline_content_splitter, 1)
        self.timeline_panel.hide()
        central_layout.addWidget(self.timeline_panel, 1)

        self.steps_timeline_panel = QWidget(central_widget)
        steps_layout = QVBoxLayout(self.steps_timeline_panel)
        steps_layout.setContentsMargins(4, 4, 4, 4)
        steps_controls = QHBoxLayout()
        steps_controls.addWidget(QLabel("Last:", self.steps_timeline_panel))
        self.steps_timeline_hours_spin = QSpinBox(self.steps_timeline_panel)
        self.steps_timeline_hours_spin.setRange(1, 720)
        self.steps_timeline_hours_spin.setValue(24)
        self.steps_timeline_hours_spin.setSuffix(" h")
        steps_controls.addWidget(self.steps_timeline_hours_spin)
        self.steps_timeline_refresh_button = QPushButton("Refresh", self.steps_timeline_panel)
        self.steps_timeline_refresh_button.clicked.connect(self.refresh_steps_timeline)
        steps_controls.addWidget(self.steps_timeline_refresh_button)
        steps_controls.addStretch(1)
        steps_layout.addLayout(steps_controls)
        self.steps_timeline_status_label = QLabel("Each job is followed by its recorded steps. Green = succeeded, red = failed, yellow = cancelled.", self.steps_timeline_panel)
        steps_layout.addWidget(self.steps_timeline_status_label)
        steps_content_splitter = QSplitter(Qt.Orientation.Horizontal, self.steps_timeline_panel)
        self.steps_timeline_job_list_panel = QWidget(steps_content_splitter)
        steps_job_list_layout = QVBoxLayout(self.steps_timeline_job_list_panel)
        steps_job_list_layout.setContentsMargins(2, 2, 6, 2)
        steps_job_list_layout.setSpacing(4)
        steps_job_list_layout.addWidget(QLabel("Jobs to display:", self.steps_timeline_job_list_panel))
        steps_job_list_buttons = QHBoxLayout()
        self.steps_timeline_select_all_button = QPushButton("All", self.steps_timeline_job_list_panel)
        self.steps_timeline_select_none_button = QPushButton("None", self.steps_timeline_job_list_panel)
        self.steps_timeline_select_all_button.clicked.connect(lambda: self._set_steps_timeline_job_checks(True))
        self.steps_timeline_select_none_button.clicked.connect(lambda: self._set_steps_timeline_job_checks(False))
        steps_job_list_buttons.addWidget(self.steps_timeline_select_all_button)
        steps_job_list_buttons.addWidget(self.steps_timeline_select_none_button)
        steps_job_list_layout.addLayout(steps_job_list_buttons)
        self.steps_timeline_job_list = QListWidget(self.steps_timeline_job_list_panel)
        self.steps_timeline_job_list.setMinimumWidth(180)
        self.steps_timeline_job_list.setMaximumWidth(300)
        self.steps_timeline_job_list.itemChanged.connect(self._on_steps_timeline_job_item_changed)
        steps_job_list_layout.addWidget(self.steps_timeline_job_list, 1)
        steps_content_splitter.addWidget(self.steps_timeline_job_list_panel)
        self.steps_timeline_scroll = QScrollArea(self.steps_timeline_panel)
        self.steps_timeline_scroll.setWidgetResizable(True)
        self.steps_timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.steps_timeline_chart = StepsTimelineWidget()
        self.steps_timeline_scroll.setWidget(self.steps_timeline_chart)
        steps_content_splitter.addWidget(self.steps_timeline_scroll)
        steps_content_splitter.setStretchFactor(0, 0)
        steps_content_splitter.setStretchFactor(1, 1)
        steps_content_splitter.setSizes([230, 900])
        steps_layout.addWidget(steps_content_splitter, 1)
        self.steps_timeline_panel.hide()
        central_layout.addWidget(self.steps_timeline_panel, 1)

        # Active SQL Server requests / sessions.
        self.processes_panel = QWidget(central_widget)
        processes_layout = QVBoxLayout(self.processes_panel)
        processes_layout.setContentsMargins(4, 4, 4, 4)
        processes_controls = QHBoxLayout()
        self.processes_refresh_button = QPushButton("Refresh", self.processes_panel)
        self.processes_refresh_button.clicked.connect(self.refresh_active_processes)
        processes_controls.addWidget(self.processes_refresh_button)
        self.processes_count_label = QLabel("0 active process(es)", self.processes_panel)
        processes_controls.addWidget(self.processes_count_label)
        processes_controls.addStretch(1)
        processes_layout.addLayout(processes_controls)
        self.processes_splitter = QSplitter(Qt.Orientation.Vertical, self.processes_panel)
        self.processes_table = QTableWidget(self.processes_splitter)
        self.processes_table.setColumnCount(9)
        self.processes_table.setHorizontalHeaderLabels([
            "Session ID", "Login", "Database", "Application", "Wait Time (ms)",
            "Wait Type", "Blocked By", "Head Blocker", "Memory Use (MB)"
        ])
        self.processes_table.setAlternatingRowColors(True)
        self.processes_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.processes_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.processes_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Allow users to sort by clicking any column header.
        self.processes_table.setSortingEnabled(True)
        self.processes_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.processes_table.horizontalHeader().setStretchLastSection(True)
        self.processes_table.verticalHeader().setVisible(False)
        self.processes_table.itemSelectionChanged.connect(self._show_selected_process_query)
        self.processes_splitter.addWidget(self.processes_table)
        self.processes_query = QPlainTextEdit(self.processes_splitter)
        self.processes_query.setReadOnly(True)
        self.processes_query.setPlaceholderText("Select an active process to view its SQL text.")
        self.processes_splitter.addWidget(self.processes_query)
        self.processes_splitter.setStretchFactor(0, 3)
        self.processes_splitter.setStretchFactor(1, 2)
        self.processes_splitter.setSizes([360, 220])
        processes_layout.addWidget(self.processes_splitter, 1)
        self.processes_panel.hide()
        central_layout.addWidget(self.processes_panel, 1)

        # CPU monitoring view.
        self.cpu_panel = QWidget(central_widget)
        cpu_layout = QVBoxLayout(self.cpu_panel)
        cpu_layout.setContentsMargins(4, 4, 4, 4)
        cpu_controls = QHBoxLayout()
        self.cpu_refresh_button = QPushButton("Refresh", self.cpu_panel)
        self.cpu_refresh_button.clicked.connect(self.refresh_cpu_usage)
        cpu_controls.addWidget(self.cpu_refresh_button)
        self.cpu_last_updated_label = QLabel("Not loaded", self.cpu_panel)
        cpu_controls.addWidget(self.cpu_last_updated_label)
        cpu_controls.addStretch(1)
        cpu_layout.addLayout(cpu_controls)

        cpu_summary_layout = QHBoxLayout()
        self.cpu_sql_label = QLabel("SQL Server CPU: —", self.cpu_panel)
        self.cpu_other_label = QLabel("Other Processes: —", self.cpu_panel)
        self.cpu_idle_label = QLabel("CPU Idle: —", self.cpu_panel)
        for label in (self.cpu_sql_label, self.cpu_other_label, self.cpu_idle_label):
            label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 8px;")
            label.setFrameShape(QFrame.Shape.StyledPanel)
            cpu_summary_layout.addWidget(label, 1)
        cpu_layout.addLayout(cpu_summary_layout)

        cpu_layout.addWidget(QLabel("Top 20 queries by total CPU time", self.cpu_panel))
        self.cpu_queries_table = QTableWidget(self.cpu_panel)
        self.cpu_queries_table.setColumnCount(7)
        self.cpu_queries_table.setHorizontalHeaderLabels([
            "Rank", "Database", "Total CPU (ms)", "Average CPU (ms)",
            "Executions", "Last Execution", "SQL Statement"
        ])
        self.cpu_queries_table.setAlternatingRowColors(True)
        self.cpu_queries_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cpu_queries_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.cpu_queries_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cpu_queries_table.setSortingEnabled(True)
        self.cpu_queries_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.cpu_queries_table.horizontalHeader().setStretchLastSection(True)
        self.cpu_queries_table.verticalHeader().setVisible(False)
        self.cpu_queries_table.setMaximumHeight(250)
        cpu_layout.addWidget(self.cpu_queries_table)

        cpu_layout.addWidget(QLabel("CPU utilization over time (recent SQL Server samples)", self.cpu_panel))
        self.cpu_chart = CpuHistoryChart(self.cpu_panel)
        cpu_layout.addWidget(self.cpu_chart, 1)
        self.cpu_panel.hide()
        central_layout.addWidget(self.cpu_panel, 1)
        self._cpu_history = []

        # Compact server health dashboard, with a scrollable vertical stack of summaries.
        self.server_health_panel = QWidget(central_widget)
        health_outer_layout = QVBoxLayout(self.server_health_panel)
        health_outer_layout.setContentsMargins(4, 4, 4, 4)
        health_controls = QHBoxLayout()
        health_title = QLabel("Server Health Overview", self.server_health_panel)
        health_title.setStyleSheet("font-size: 18px; font-weight: bold; padding: 3px;")
        health_controls.addWidget(health_title)
        health_controls.addStretch(1)
        self.server_health_updated_label = QLabel("Not loaded", self.server_health_panel)
        health_controls.addWidget(self.server_health_updated_label)
        self.server_health_refresh_button = QPushButton("Refresh", self.server_health_panel)
        self.server_health_refresh_button.clicked.connect(self.refresh_server_health_overview)
        health_controls.addWidget(self.server_health_refresh_button)
        health_outer_layout.addLayout(health_controls)

        self.server_health_scroll = QScrollArea(self.server_health_panel)
        self.server_health_scroll.setWidgetResizable(True)
        self.server_health_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.server_health_content = QWidget(self.server_health_scroll)
        health_content_layout = QVBoxLayout(self.server_health_content)
        health_content_layout.setContentsMargins(4, 2, 4, 4)
        health_content_layout.setSpacing(7)

        # Quick server indicators shown above the CPU and memory charts.
        health_kpi_row = QHBoxLayout()
        self.server_health_connections_label = QLabel("SQL Server connections: —")
        self.server_health_connections_label.setStyleSheet("font-weight: bold; padding: 7px;")
        self.server_health_uptime_label = QLabel("SQL Server uptime: —")
        self.server_health_uptime_label.setStyleSheet("font-weight: bold; padding: 7px;")
        self.server_health_deadlocks_label = QLabel("Deadlocks: —")
        self.server_health_deadlocks_label.setStyleSheet("font-weight: bold; padding: 7px;")
        for kpi in (self.server_health_connections_label,
                    self.server_health_uptime_label,
                    self.server_health_deadlocks_label):
            kpi.setFrameShape(QFrame.Shape.StyledPanel)
            health_kpi_row.addWidget(kpi, 1)
        health_content_layout.addLayout(health_kpi_row)

        backup_box = QFrame(self.server_health_content)
        backup_box.setFrameShape(QFrame.Shape.StyledPanel)
        backup_layout = QVBoxLayout(backup_box)
        backup_layout.setContentsMargins(6, 4, 6, 4)
        backup_layout.setSpacing(3)
        backup_heading = QLabel("Database Backups — latest successful backup", backup_box)
        backup_heading.setStyleSheet("font-weight: bold;")
        backup_layout.addWidget(backup_heading)
        self.server_health_backup_table = QTableWidget(backup_box)
        self.server_health_backup_table.setColumnCount(4)
        self.server_health_backup_table.setHorizontalHeaderLabels(
            ["Database", "Last Full", "Last Differential", "Last Log"]
        )
        self.server_health_backup_table.setAlternatingRowColors(True)
        self.server_health_backup_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.server_health_backup_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.server_health_backup_table.setSortingEnabled(True)
        self.server_health_backup_table.verticalHeader().setVisible(False)
        self.server_health_backup_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.server_health_backup_table.horizontalHeader().setStretchLastSection(True)
        self.server_health_backup_table.setMinimumHeight(85)
        self.server_health_backup_table.setMaximumHeight(145)
        backup_layout.addWidget(self.server_health_backup_table)
        health_content_layout.addWidget(backup_box)

        charts_row = QHBoxLayout()
        cpu_box = QFrame(self.server_health_content)
        cpu_box.setFrameShape(QFrame.Shape.StyledPanel)
        cpu_box_layout = QVBoxLayout(cpu_box)
        cpu_box_layout.setContentsMargins(6, 4, 6, 4)
        cpu_box_layout.addWidget(QLabel("CPU utilization", cpu_box))
        self.server_health_cpu_chart = CpuHistoryChart(cpu_box)
        self.server_health_cpu_chart.setMinimumHeight(150)
        self.server_health_cpu_chart.setMaximumHeight(180)
        cpu_box_layout.addWidget(self.server_health_cpu_chart)
        self.server_health_cpu_summary = QLabel("SQL Server: — | Other: — | Idle: —", cpu_box)
        cpu_box_layout.addWidget(self.server_health_cpu_summary)
        memory_box = QFrame(self.server_health_content)
        memory_box.setFrameShape(QFrame.Shape.StyledPanel)
        memory_box_layout = QVBoxLayout(memory_box)
        memory_box_layout.setContentsMargins(6, 4, 6, 4)
        memory_box_layout.addWidget(QLabel("Memory usage", memory_box))
        self.server_health_total_memory_label = QLabel("Total physical memory: — GB", memory_box)
        self.server_health_total_memory_label.setStyleSheet("font-weight: bold; padding: 1px 3px;")
        memory_box_layout.addWidget(self.server_health_total_memory_label)
        self.server_health_memory_chart = MemoryOverviewChart(memory_box)
        memory_box_layout.addWidget(self.server_health_memory_chart)
        charts_row.addWidget(cpu_box, 1)
        charts_row.addWidget(memory_box, 1)
        health_content_layout.addLayout(charts_row)

        def make_health_table(title, headers, parent, max_height):
            box = QFrame(parent)
            box.setFrameShape(QFrame.Shape.StyledPanel)
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(6, 4, 6, 4)
            box_layout.setSpacing(3)
            heading = QLabel(title, box)
            heading.setStyleSheet("font-weight: bold;")
            box_layout.addWidget(heading)
            table = QTableWidget(box)
            table.setColumnCount(len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setSortingEnabled(True)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            table.horizontalHeader().setStretchLastSection(True)
            table.setMaximumHeight(max_height)
            table.setMinimumHeight(min(65, max_height))
            box_layout.addWidget(table)
            return box, table

        disk_box, self.server_health_disk_table = make_health_table(
            "Disk space", ["Drive / mount point", "File system", "Total GB", "Free GB", "Free %"],
            self.server_health_content, 112)
        health_content_layout.addWidget(disk_box)
        db_box, self.server_health_database_table = make_health_table(
            "Database space compared", ["Database", "Data used / free", "Log used / free", "Total MB"],
            self.server_health_content, 10000)
        health_content_layout.addWidget(db_box)
        blocked_box, self.server_health_blocked_table = make_health_table(
            "Blocked processes", ["Session ID", "Database", "Blocked by", "Wait (ms)", "Wait type", "SQL statement"],
            self.server_health_content, 130)
        health_content_layout.addWidget(blocked_box)
        failed_box, self.server_health_failed_jobs_table = make_health_table(
            "Failed jobs — last 24 hours", ["Job", "End time", "Duration", "Error"],
            self.server_health_content, 130)
        health_content_layout.addWidget(failed_box)
        health_content_layout.addStretch(1)
        self.server_health_scroll.setWidget(self.server_health_content)
        health_outer_layout.addWidget(self.server_health_scroll, 1)
        self.server_health_panel.hide()
        central_layout.addWidget(self.server_health_panel, 1)

        # Dedicated lightweight Server Health Monitor (CPU utilization only for now).
        self.server_health_monitor_panel = QWidget(central_widget)
        monitor_layout = QVBoxLayout(self.server_health_monitor_panel)
        monitor_layout.setContentsMargins(8, 8, 8, 8)
        monitor_controls = QHBoxLayout()
        monitor_title = QLabel("Server Health Monitor", self.server_health_monitor_panel)
        monitor_title.setStyleSheet("font-size: 18px; font-weight: bold; padding: 3px;")
        monitor_controls.addWidget(monitor_title)
        monitor_controls.addSpacing(16)
        monitor_controls.addWidget(QLabel("Refresh interval (sec):", self.server_health_monitor_panel))
        self.server_health_monitor_interval = QSpinBox(self.server_health_monitor_panel)
        self.server_health_monitor_interval.setRange(1, 3600)
        self.server_health_monitor_interval.setValue(5)
        self.server_health_monitor_interval.setSuffix(" sec")
        self.server_health_monitor_interval.setToolTip("Automatic refresh interval in seconds")
        monitor_controls.addWidget(self.server_health_monitor_interval)
        monitor_controls.addWidget(QLabel("Keep history (hours):", self.server_health_monitor_panel))
        self.server_health_monitor_retention = QSpinBox(self.server_health_monitor_panel)
        self.server_health_monitor_retention.setRange(1, 720)
        self.server_health_monitor_retention.setValue(4)
        self.server_health_monitor_retention.setSuffix(" h")
        self.server_health_monitor_retention.setToolTip(
            "How many hours of monitoring history to keep in application memory (1–720 hours)"
        )
        monitor_controls.addWidget(self.server_health_monitor_retention)
        self.server_health_monitor_pause_button = QPushButton("Pause refresh", self.server_health_monitor_panel)
        self.server_health_monitor_pause_button.setCheckable(True)
        self.server_health_monitor_pause_button.toggled.connect(self._toggle_server_health_monitor_pause)
        monitor_controls.addWidget(self.server_health_monitor_pause_button)
        monitor_controls.addStretch(1)
        self.server_health_monitor_updated_label = QLabel("Not loaded", self.server_health_monitor_panel)
        monitor_controls.addWidget(self.server_health_monitor_updated_label)
        self.server_health_monitor_refresh_button = QPushButton("Refresh", self.server_health_monitor_panel)
        self.server_health_monitor_refresh_button.clicked.connect(self.refresh_server_health_monitor)
        monitor_controls.addWidget(self.server_health_monitor_refresh_button)
        monitor_layout.addLayout(monitor_controls)

        self.server_health_monitor_scroll = QScrollArea(self.server_health_monitor_panel)
        self.server_health_monitor_scroll.setWidgetResizable(True)
        self.server_health_monitor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.server_health_monitor_content = QWidget(self.server_health_monitor_scroll)
        monitor_content_layout = QVBoxLayout(self.server_health_monitor_content)
        monitor_content_layout.setContentsMargins(2, 2, 2, 2)
        monitor_content_layout.setSpacing(6)

        # Compact CPU and memory charts side by side to leave room for database histories.
        monitor_top_charts = QHBoxLayout()
        cpu_monitor_box = QFrame(self.server_health_monitor_content)
        cpu_monitor_box.setFrameShape(QFrame.Shape.StyledPanel)
        cpu_monitor_layout = QVBoxLayout(cpu_monitor_box)
        cpu_monitor_layout.setContentsMargins(5, 4, 5, 4)
        cpu_monitor_layout.addWidget(QLabel("CPU utilization", cpu_monitor_box))
        self.server_health_monitor_cpu_summary = QLabel(
            "SQL Server: — | Other: — | Idle: —", cpu_monitor_box
        )
        self.server_health_monitor_cpu_summary.setStyleSheet("font-weight: bold; padding: 2px;")
        cpu_monitor_layout.addWidget(self.server_health_monitor_cpu_summary)
        self.server_health_monitor_cpu_chart = CpuHistoryChart(cpu_monitor_box)
        self.server_health_monitor_cpu_chart.setMinimumHeight(155)
        self.server_health_monitor_cpu_chart.setMaximumHeight(175)
        cpu_monitor_layout.addWidget(self.server_health_monitor_cpu_chart)

        memory_monitor_box = QFrame(self.server_health_monitor_content)
        memory_monitor_box.setFrameShape(QFrame.Shape.StyledPanel)
        memory_monitor_layout = QVBoxLayout(memory_monitor_box)
        memory_monitor_layout.setContentsMargins(5, 4, 5, 4)
        memory_monitor_layout.addWidget(QLabel("Memory used (physical)", memory_monitor_box))
        self.server_health_monitor_memory_summary = QLabel(
            "Used: — GB | Total: — GB", memory_monitor_box
        )
        self.server_health_monitor_memory_summary.setStyleSheet("font-weight: bold; padding: 2px;")
        memory_monitor_layout.addWidget(self.server_health_monitor_memory_summary)
        self.server_health_monitor_memory_chart = MemoryHistoryChart(memory_monitor_box)
        self.server_health_monitor_memory_chart.setMinimumHeight(155)
        self.server_health_monitor_memory_chart.setMaximumHeight(175)
        memory_monitor_layout.addWidget(self.server_health_monitor_memory_chart)
        monitor_top_charts.addWidget(cpu_monitor_box, 1)
        monitor_top_charts.addWidget(memory_monitor_box, 1)
        monitor_content_layout.addLayout(monitor_top_charts)

        # Connection count and disk utilization histories directly follow memory.
        monitor_resource_charts = QHBoxLayout()
        connection_monitor_box = QFrame(self.server_health_monitor_content)
        connection_monitor_box.setFrameShape(QFrame.Shape.StyledPanel)
        connection_monitor_layout = QVBoxLayout(connection_monitor_box)
        connection_monitor_layout.setContentsMargins(5, 4, 5, 4)
        connection_monitor_layout.addWidget(QLabel("SQL Server connections", connection_monitor_box))
        self.server_health_monitor_connection_summary = QLabel(
            "Active connections: —", connection_monitor_box
        )
        self.server_health_monitor_connection_summary.setStyleSheet("font-weight: bold; padding: 2px;")
        connection_monitor_layout.addWidget(self.server_health_monitor_connection_summary)
        self.server_health_monitor_connection_chart = ConnectionHistoryChart(connection_monitor_box)
        connection_monitor_layout.addWidget(self.server_health_monitor_connection_chart)

        disk_monitor_box = QFrame(self.server_health_monitor_content)
        disk_monitor_box.setFrameShape(QFrame.Shape.StyledPanel)
        disk_monitor_layout = QVBoxLayout(disk_monitor_box)
        disk_monitor_layout.setContentsMargins(5, 4, 5, 4)
        disk_monitor_layout.addWidget(QLabel("Disk space used (%)", disk_monitor_box))
        self.server_health_monitor_disk_summary = QLabel(
            "Volumes containing SQL Server files", disk_monitor_box
        )
        self.server_health_monitor_disk_summary.setStyleSheet("font-weight: bold; padding: 2px;")
        disk_monitor_layout.addWidget(self.server_health_monitor_disk_summary)
        self.server_health_monitor_disk_chart = DiskUsageHistoryChart(disk_monitor_box)
        disk_monitor_layout.addWidget(self.server_health_monitor_disk_chart)
        monitor_resource_charts.addWidget(connection_monitor_box, 1)
        monitor_resource_charts.addWidget(disk_monitor_box, 1)
        monitor_content_layout.addLayout(monitor_resource_charts)

        self.server_health_monitor_database_history_heading = QLabel(
            "Database space history — last 4 hours", self.server_health_monitor_content
        )
        self.server_health_monitor_database_history_heading.setStyleSheet(
            "font-size: 14px; font-weight: bold; padding: 4px;"
        )
        monitor_content_layout.addWidget(self.server_health_monitor_database_history_heading)
        self.server_health_monitor_database_charts_layout = QGridLayout()
        self.server_health_monitor_database_charts_layout.setContentsMargins(2, 2, 2, 2)
        self.server_health_monitor_database_charts_layout.setHorizontalSpacing(8)
        self.server_health_monitor_database_charts_layout.setVerticalSpacing(5)
        self.server_health_monitor_database_charts_layout.setColumnStretch(0, 1)
        self.server_health_monitor_database_charts_layout.setColumnStretch(1, 1)
        monitor_content_layout.addLayout(self.server_health_monitor_database_charts_layout)
        monitor_content_layout.addStretch(1)
        self.server_health_monitor_scroll.setWidget(self.server_health_monitor_content)
        monitor_layout.addWidget(self.server_health_monitor_scroll, 1)

        self.server_health_monitor_timer = QTimer(self)
        self.server_health_monitor_timer.setInterval(5000)
        self.server_health_monitor_timer.timeout.connect(self.refresh_server_health_monitor)
        self.server_health_monitor_interval.valueChanged.connect(
            self._set_server_health_monitor_interval
        )
        self.server_health_monitor_retention.valueChanged.connect(
            self._set_server_health_monitor_retention
        )
        self._server_health_monitor_memory_points = []
        self._server_health_monitor_connection_points = []
        self._server_health_monitor_disk_points = []
        self._server_health_monitor_database_points = {}
        self._server_health_monitor_database_charts = {}
        self._server_health_monitor_paused = False
        self.server_health_monitor_panel.hide()
        central_layout.addWidget(self.server_health_monitor_panel, 1)

        self.setCentralWidget(central_widget)

        # Keep selected table rows readable across all table views.
        # Explicitly set both background and foreground: otherwise some
        # platform themes use a dark selection color while retaining black text.
        self.setStyleSheet(self.styleSheet() + """
            QTableWidget::item:selected,
            QTableView::item:selected {
                background-color: #D6EAF8;
                color: #17324D;
            }
            QTableWidget::item:selected:active,
            QTableView::item:selected:active {
                background-color: #C7E2F5;
                color: #17324D;
            }
            QTableWidget::item:selected:!active,
            QTableView::item:selected:!active {
                background-color: #E2EDF5;
                color: #17324D;
            }
        """)

        self._set_view_mode("table")
        self._space_usage_results = []
        self._tables_size_rows = []
        self._timeline_all_runs = []
        self._timeline_job_checks_initialized = False
        self._timeline_updating_job_list = False
        self._steps_timeline_all_jobs = []
        self._steps_timeline_updating_job_list = False
        self._job_history_all_rows = []
        self._job_history_headers = []
        self._job_history_updating_list = False

        self._create_menus()
        self.menuBar().triggered.connect(self._hide_home_screen)
        # Give the main menu bar a subtly different grey so it is visually distinct.
        self.menuBar().setStyleSheet("""
            QMenuBar { background-color: #e3e3e3; border-bottom: 1px solid #c8c8c8; }
            QMenuBar::item { background: transparent; padding: 5px 9px; }
            QMenuBar::item:selected { background-color: #d0d0d0; }
            QMenuBar::item:pressed { background-color: #c5c5c5; }
            QMenu { background-color: #f4f4f4; color: #202020; }
            QMenu::item { color: #202020; padding: 5px 24px 5px 20px; }
            QMenu::item:selected { color: #0066cc; background-color: #e0e0e0; }
            QMenu::item:disabled { color: #888888; }
        """)
        self.statusBar().showMessage("Not connected")

    def _hide_home_screen(self, action=None):
        """Hide the welcome image after the user selects a menu command."""
        if hasattr(self, "home_screen"):
            self.home_screen.hide()

    def _set_view_mode(self, mode):
        """Set the active view and update whether Excel export is available."""
        self._view_mode = mode
        self._update_export_action()

    def _update_export_action(self):
        """Enable Excel export only in views with an implemented exporter."""
        action = getattr(self, "excel_action", None)
        if action is None:
            return
        exportable_modes = {"table", "space", "tables", "indexes", "programmability"}
        action.setEnabled(getattr(self, "_view_mode", None) in exportable_modes)

    def _create_menus(self):
        menu_file = self.menuBar().addMenu("&File")
        connect_action = QAction("Connect to SQL server", self)
        connect_action.setStatusTip("Connect to a SQL Server or switch to another server")
        connect_action.triggered.connect(self.connect_to_sql_server)
        menu_file.addAction(connect_action)

        menu_file.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.setStatusTip("Exit Simple SQL Server Monitor")
        exit_action.triggered.connect(self.close)
        menu_file.addAction(exit_action)

        menu_server = self.menuBar().addMenu("&Server")
        basic_info_action = QAction("Basic Information", self)
        basic_info_action.setStatusTip("Show basic information about the connected SQL Server")
        basic_info_action.triggered.connect(self.show_server_information)
        menu_server.addAction(basic_info_action)
        server_health_action = QAction("Server Health Overview", self)
        server_health_action.setStatusTip("Show CPU, memory, disk, database space, blockers, and failed jobs in one dashboard")
        server_health_action.triggered.connect(self.show_server_health_overview)
        menu_server.addAction(server_health_action)
        disk_space_action = QAction("Disk space", self)
        disk_space_action.setStatusTip(
            "Show total and free disk space for volumes containing SQL Server database files"
        )
        disk_space_action.triggered.connect(self.show_disk_space)
        menu_server.addAction(disk_space_action)

        menu_monitor = self.menuBar().addMenu("&Monitor")
        server_health_monitor_action = QAction("Server Health Monitor", self)
        server_health_monitor_action.setStatusTip("Monitor SQL Server CPU utilization in real time")
        server_health_monitor_action.triggered.connect(self.show_server_health_monitor)
        menu_monitor.addAction(server_health_monitor_action)

        menu_database_files = self.menuBar().addMenu("&Database files")
        database_size_action = QAction("Database files size", self)
        database_size_action.setStatusTip(
            "Show data and transaction log file sizes for all databases"
        )
        database_size_action.triggered.connect(self.show_database_file_sizes)
        menu_database_files.addAction(database_size_action)
        space_usage_action = QAction("Space usage", self)
        space_usage_action.setStatusTip("Show used and free space for each database")
        space_usage_action.triggered.connect(self.show_space_usage)
        menu_database_files.addAction(space_usage_action)
        database_space_compared_action = QAction("Database space compared", self)
        database_space_compared_action.setStatusTip(
            "Compare allocated database data space and transaction log sizes across databases"
        )
        database_space_compared_action.triggered.connect(self.show_database_space_compared)
        menu_database_files.addAction(database_space_compared_action)

        menu_tables = self.menuBar().addMenu("&Tables")
        tables_size_action = QAction("Table Explorer", self)
        tables_size_action.setStatusTip("Explore database tables, columns, data types, and preview the first 10 rows")
        tables_size_action.triggered.connect(self.show_tables_size)
        menu_tables.addAction(tables_size_action)
        tables_triggers_action = QAction("Tables with Triggers", self)
        tables_triggers_action.setStatusTip("List tables with triggers and view trigger SQL definitions")
        tables_triggers_action.triggered.connect(self.show_tables_with_triggers)
        menu_tables.addAction(tables_triggers_action)
        tables_indexes_action = QAction("Tables With Indexes", self)
        tables_indexes_action.setStatusTip("List tables and indexes and show index properties")
        tables_indexes_action.triggered.connect(self.show_tables_with_indexes)
        menu_tables.addAction(tables_indexes_action)

        menu_programmability = self.menuBar().addMenu("&Programmability")
        programmability_action = QAction("List and Search", self)
        programmability_action.setStatusTip("Search stored procedures, functions, views, and triggers and view their SQL definitions")
        programmability_action.triggered.connect(self.show_programmability_objects)
        menu_programmability.addAction(programmability_action)
        view_structure_action = QAction("View Structure", self)
        view_structure_action.setStatusTip("Show tables and nested views referenced by a selected view")
        view_structure_action.triggered.connect(self.show_view_structure)
        menu_programmability.addAction(view_structure_action)

        menu_jobs = self.menuBar().addMenu("&Jobs")
        jobs_list_action = QAction("Jobs list", self)
        jobs_list_action.setStatusTip("List SQL Server Agent jobs, schedules, and their active status")
        jobs_list_action.triggered.connect(self.show_jobs_list)
        menu_jobs.addAction(jobs_list_action)
        failed_jobs_action = QAction("Failed jobs last 24h", self)
        failed_jobs_action.setStatusTip("Show SQL Server Agent jobs that failed in the last 24 hours")
        failed_jobs_action.triggered.connect(self.show_failed_jobs_last_24h)
        menu_jobs.addAction(failed_jobs_action)
        completed_jobs_action = QAction("Completed jobs last 24h", self)
        completed_jobs_action.setStatusTip("Show SQL Server Agent jobs that completed successfully in the last 24 hours")
        completed_jobs_action.triggered.connect(self.show_completed_jobs_last_24h)
        menu_jobs.addAction(completed_jobs_action)
        timeline_action = QAction("Timeline", self)
        timeline_action.setStatusTip("Show SQL Server Agent job execution timeline for the last N hours")
        timeline_action.triggered.connect(self.show_jobs_timeline)
        menu_jobs.addAction(timeline_action)
        steps_timeline_action = QAction("Steps timeline", self)
        steps_timeline_action.setStatusTip("Show SQL Server Agent job runs and individual step timelines for the last N hours")
        steps_timeline_action.triggered.connect(self.show_steps_timeline)
        menu_jobs.addAction(steps_timeline_action)

        menu_processes = self.menuBar().addMenu("&Processes")
        active_processes_action = QAction("Active Processes", self)
        active_processes_action.setStatusTip("Show active SQL Server requests, waits, blockers, memory use, and SQL text")
        active_processes_action.triggered.connect(self.show_active_processes)
        menu_processes.addAction(active_processes_action)
        cpu_usage_action = QAction("CPU Usage", self)
        cpu_usage_action.setStatusTip("Show current CPU utilization, top CPU-consuming queries, and CPU history")
        cpu_usage_action.triggered.connect(self.show_cpu_usage)
        menu_processes.addAction(cpu_usage_action)

        menu_export = self.menuBar().addMenu("&Export")
        self.excel_action = QAction("&Excel", self)
        self.excel_action.setStatusTip("Export the data shown in the current view to an Excel workbook")
        self.excel_action.triggered.connect(self.export_to_excel)
        menu_export.addAction(self.excel_action)
        self._update_export_action()

        menu_help = self.menuBar().addMenu("&Help")
        user_guide_action = QAction("&User Guide", self)
        user_guide_action.setStatusTip("Open the Simple SQL Server Monitor user guide")
        user_guide_action.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://simplesqlservermonitor.blogspot.com/"))
        )
        menu_help.addAction(user_guide_action)

        about_action = QAction("&About", self)
        about_action.setStatusTip("About Simple SQL Server Monitor")
        about_action.triggered.connect(self.show_about)
        menu_help.addAction(about_action)

    def connect_to_sql_server(self):
        """Open the connection dialog and switch servers after a successful connection."""
        dialog = ConnectionDialog(self, initial_settings=self._settings)
        if dialog.exec() != dialog.DialogCode.Accepted:
            self.statusBar().showMessage("Connection unchanged")
            return

        settings = dialog.connection_settings
        if settings is None:
            return

        new_db = SQLServerConnection(settings)
        try:
            new_db.connect()
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            new_db.disconnect()
            QMessageBox.critical(
                self,
                "SQL Server connection failed",
                "Could not connect to the selected SQL Server.\n\n" + str(exc),
            )
            self.statusBar().showMessage("Connection failed; previous connection retained")
            return

        # Only replace the active connection after the new connection succeeds.
        old_db = self._db
        self._db = new_db
        self._settings = settings
        if old_db is not None:
            old_db.disconnect()

        self.statusBar().showMessage(
            f"Connected to {settings.server} — database: {settings.database}"
        )
        self.show_server_information()

    def _ensure_connection(self) -> bool:
        if self._db is not None and self._db.is_connected():
            return True

        dialog = ConnectionDialog(self, initial_settings=self._settings)
        if dialog.exec() != dialog.DialogCode.Accepted:
            self.statusBar().showMessage("Connection cancelled")
            return False

        settings = dialog.connection_settings
        if settings is None:
            return False

        self._settings = settings
        db = SQLServerConnection(settings)
        try:
            db.connect()
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            db.disconnect()
            QMessageBox.critical(
                self,
                "SQL Server connection failed",
                "Could not connect to SQL Server.\n\n" + str(exc),
            )
            self.statusBar().showMessage("Connection failed")
            return False

        self._db = db
        self.statusBar().showMessage(
            f"Connected to {settings.server} — database: {settings.database}"
        )
        self.show_server_information()
        return True

    def _leave_server_health_monitor(self):
        """Hide the monitor and stop polling whenever another view is opened."""
        if hasattr(self, "server_health_monitor_timer"):
            self.server_health_monitor_timer.stop()
        if hasattr(self, "server_health_monitor_panel"):
            self.server_health_monitor_panel.hide()

    def _toggle_server_health_monitor_pause(self, paused):
        """Pause/resume automatic polling without clearing collected history."""
        self._server_health_monitor_paused = bool(paused)
        if paused:
            self.server_health_monitor_timer.stop()
            self.server_health_monitor_pause_button.setText("Resume refresh")
            self.server_health_monitor_updated_label.setText("Paused")
        else:
            self.server_health_monitor_pause_button.setText("Pause refresh")
            if self._view_mode == "server_health_monitor":
                self.server_health_monitor_timer.start()
                self.refresh_server_health_monitor()

    def _set_server_health_monitor_interval(self, seconds):
        """Apply the selected automatic refresh interval."""
        self.server_health_monitor_timer.setInterval(int(seconds) * 1000)

    def _set_server_health_monitor_retention(self, hours):
        """Trim all in-memory monitor histories to the selected retention window."""
        cutoff = datetime.now() - timedelta(hours=int(hours))
        self.server_health_monitor_database_history_heading.setText(
            f"Database space history — last {int(hours)} hour(s)"
        )

        self._server_health_monitor_memory_points = [
            point for point in self._server_health_monitor_memory_points if point[0] >= cutoff
        ]
        self.server_health_monitor_memory_chart.set_points(
            self._server_health_monitor_memory_points
        )

        self._server_health_monitor_connection_points = [
            point for point in self._server_health_monitor_connection_points if point[0] >= cutoff
        ]
        self.server_health_monitor_connection_chart.set_points(
            self._server_health_monitor_connection_points
        )

        self._server_health_monitor_disk_points = [
            point for point in self._server_health_monitor_disk_points if point[0] >= cutoff
        ]
        self.server_health_monitor_disk_chart.set_points(self._server_health_monitor_disk_points)

        for database_name, history in list(self._server_health_monitor_database_points.items()):
            trimmed = [point for point in history if point[0] >= cutoff]
            self._server_health_monitor_database_points[database_name] = trimmed
            chart = self._server_health_monitor_database_charts.get(database_name)
            if chart is not None:
                chart.set_points(trimmed)

    def show_server_health_monitor(self):
        """Open the standalone CPU utilization monitor."""
        if not self._ensure_connection():
            return
        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
            self.jobs_panel, self.timeline_panel, self.steps_timeline_panel,
            self.processes_panel, self.cpu_panel, self.server_health_panel,
        ):
            widget.hide()
        widget = getattr(self, "view_structure_panel", None)
        if widget is not None:
            widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self._set_view_mode("server_health_monitor")
        self.server_health_monitor_panel.show()
        if self.server_health_monitor_pause_button.isChecked():
            self.server_health_monitor_pause_button.blockSignals(True)
            self.server_health_monitor_pause_button.setChecked(False)
            self.server_health_monitor_pause_button.blockSignals(False)
            self._server_health_monitor_paused = False
            self.server_health_monitor_pause_button.setText("Pause refresh")
        self.server_health_monitor_timer.start()
        self.refresh_server_health_monitor()

    def _ensure_database_monitor_chart(self, database_name):
        """Create a compact history chart for a database when first discovered."""
        if database_name in self._server_health_monitor_database_charts:
            return self._server_health_monitor_database_charts[database_name]
        box = QFrame(self.server_health_monitor_content)
        box.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(5, 3, 5, 3)
        layout.setSpacing(2)
        title = QLabel(database_name, box)
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)
        chart = DatabaseHistoryChart(box)
        layout.addWidget(chart)
        chart_index = len(self._server_health_monitor_database_charts)
        row, column = divmod(chart_index, 2)
        self.server_health_monitor_database_charts_layout.addWidget(box, row, column)
        self._server_health_monitor_database_charts[database_name] = chart
        self._server_health_monitor_database_points.setdefault(database_name, [])
        return chart

    def refresh_server_health_monitor(self):
        """Refresh monitor histories, retaining the user-selected number of hours in RAM."""
        if self._view_mode != "server_health_monitor":
            self.server_health_monitor_timer.stop()
            return
        if not self._ensure_connection():
            self.server_health_monitor_timer.stop()
            return
        self.server_health_monitor_refresh_button.setEnabled(False)
        self.server_health_monitor_updated_label.setText("Refreshing...")
        cursor = self._db.get_connection().cursor()
        now = datetime.now()
        cutoff = now - timedelta(hours=self.server_health_monitor_retention.value())
        try:
            cursor.execute(r"""
SELECT TOP (360)
    DATEADD(ms, -1 * (si.ms_ticks - rb.[timestamp]), GETDATE()) AS SampleTime,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS SqlCpu,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS IdleCpu,
    100
      - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int')
      - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS OtherCpu
FROM
(
    SELECT [timestamp], CONVERT(xml, record) AS record
    FROM sys.dm_os_ring_buffers
    WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
      AND record LIKE '%<SystemHealth>%'
) AS rb
CROSS JOIN sys.dm_os_sys_info AS si
ORDER BY rb.[timestamp] DESC;
""")
            samples = cursor.fetchall()
            cpu_history = [
                (sample[0], int(sample[1] or 0), int(sample[3] or 0), int(sample[2] or 0))
                for sample in reversed(samples)
                if sample[1] is not None and sample[0] is not None and sample[0] >= cutoff
            ][-360:]
            self.server_health_monitor_cpu_chart.set_points(cpu_history)
            if cpu_history:
                latest = cpu_history[-1]
                self.server_health_monitor_cpu_summary.setText(
                    f"SQL Server: {latest[1]}%  |  Other: {latest[2]}%  |  Idle: {latest[3]}%"
                )
            else:
                self.server_health_monitor_cpu_summary.setText("CPU samples are not currently available.")

            # Physical memory: sampled on every refresh and retained only in application RAM.
            cursor.execute(r"""
SELECT total_physical_memory_kb / 1024.0, available_physical_memory_kb / 1024.0
FROM sys.dm_os_sys_memory;
""")
            memory_row = cursor.fetchone()
            if memory_row:
                total_mb = float(memory_row[0] or 0.0)
                available_mb = float(memory_row[1] or 0.0)
                used_gb = max(0.0, total_mb - available_mb) / 1024.0
                total_gb = total_mb / 1024.0
                self._server_health_monitor_memory_points.append((now, used_gb, total_gb))
                self._server_health_monitor_memory_points = [
                    p for p in self._server_health_monitor_memory_points if p[0] >= cutoff
                ]
                self.server_health_monitor_memory_chart.set_points(
                    self._server_health_monitor_memory_points
                )
                pct = used_gb / total_gb * 100.0 if total_gb else 0.0
                self.server_health_monitor_memory_summary.setText(
                    f"Physical memory used: {used_gb:.2f} GB ({pct:.1f}%) | "
                    f"Total: {total_gb:.2f} GB | Samples: {len(self._server_health_monitor_memory_points):,}"
                )

            # Active SQL Server connections: sample the connection DMV on every refresh.
            cursor.execute("SELECT COUNT_BIG(*) FROM sys.dm_exec_connections;")
            connection_row = cursor.fetchone()
            connection_count = int(connection_row[0] or 0) if connection_row else 0
            self._server_health_monitor_connection_points.append((now, connection_count))
            self._server_health_monitor_connection_points = [
                point for point in self._server_health_monitor_connection_points
                if point[0] >= cutoff
            ]
            self.server_health_monitor_connection_chart.set_points(
                self._server_health_monitor_connection_points
            )
            self.server_health_monitor_connection_summary.setText(
                f"Active connections: {connection_count:,} | "
                f"Samples: {len(self._server_health_monitor_connection_points):,}"
            )

            # Disk utilization for each distinct volume containing SQL Server files.
            cursor.execute(r"""
SELECT DISTINCT
    vs.volume_mount_point,
    vs.total_bytes,
    vs.available_bytes
FROM sys.master_files AS mf
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS vs
WHERE vs.volume_mount_point IS NOT NULL
ORDER BY vs.volume_mount_point;
""")
            disk_rows = cursor.fetchall()
            disk_usage = {}
            for disk_row in disk_rows:
                volume = str(disk_row[0]).rstrip("\\/")
                total_bytes = float(disk_row[1] or 0)
                available_bytes = float(disk_row[2] or 0)
                if total_bytes > 0:
                    used_pct = max(0.0, min(100.0,
                        (total_bytes - available_bytes) / total_bytes * 100.0))
                    disk_usage[volume] = used_pct
            self._server_health_monitor_disk_points.append((now, disk_usage))
            self._server_health_monitor_disk_points = [
                point for point in self._server_health_monitor_disk_points
                if point[0] >= cutoff
            ]
            self.server_health_monitor_disk_chart.set_points(
                self._server_health_monitor_disk_points
            )
            self.server_health_monitor_disk_summary.setText(
                f"Volumes monitored: {len(disk_usage)} | "
                + "  ".join(f"{name}: {value:.1f}%" for name, value in sorted(disk_usage.items()))
            )

            # Enumerate online user databases, then collect data/log used and free space in each.
            cursor.execute("""
SELECT name
FROM sys.databases
WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
ORDER BY name;
""")
            database_names = [str(row[0]) for row in cursor.fetchall()]
            active_names = set(database_names)
            for database_name in database_names:
                try:
                    quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
                    cursor.execute(f"USE {quoted_db};")
                    cursor.execute(r"""
SELECT
    COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN size END), 0) * 8.0 / 1024.0 AS DataTotalMB,
    COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN FILEPROPERTY(name, 'SpaceUsed') END), 0) * 8.0 / 1024.0 AS DataUsedMB,
    COALESCE(SUM(CASE WHEN type_desc = 'LOG' THEN size END), 0) * 8.0 / 1024.0 AS LogTotalMB,
    COALESCE(MAX(log_space.used_log_space_in_bytes), 0) / 1048576.0 AS LogUsedMB
FROM sys.database_files
CROSS JOIN sys.dm_db_log_space_usage AS log_space;
""")
                    space_row = cursor.fetchone()
                    data_total = max(0.0, float(space_row[0] or 0.0))
                    data_used = min(data_total, max(0.0, float(space_row[1] or 0.0)))
                    log_total = max(0.0, float(space_row[2] or 0.0))
                    log_used = min(log_total, max(0.0, float(space_row[3] or 0.0)))
                    point = (
                        now,
                        data_used / 1024.0,
                        max(0.0, data_total - data_used) / 1024.0,
                        log_used / 1024.0,
                        max(0.0, log_total - log_used) / 1024.0,
                        (data_total + log_total) / 1024.0,
                    )
                    history = self._server_health_monitor_database_points.setdefault(database_name, [])
                    history.append(point)
                    self._server_health_monitor_database_points[database_name] = [
                        p for p in history if p[0] >= cutoff
                    ]
                    chart = self._ensure_database_monitor_chart(database_name)
                    chart.set_points(self._server_health_monitor_database_points[database_name])
                except pyodbc.Error:
                    # Skip this database for the current sample, but continue monitoring others.
                    continue

            # Keep only the last four hours for databases that are currently offline/removed too.
            for database_name, history in list(self._server_health_monitor_database_points.items()):
                self._server_health_monitor_database_points[database_name] = [
                    p for p in history if p[0] >= cutoff
                ]
                if database_name in self._server_health_monitor_database_charts:
                    self._server_health_monitor_database_charts[database_name].set_points(
                        self._server_health_monitor_database_points[database_name]
                    )

            self.server_health_monitor_updated_label.setText(
                f"Updated: {now:%Y-%m-%d %H:%M:%S}"
                + (" | Paused" if self._server_health_monitor_paused else "")
            )
        except pyodbc.Error as exc:
            self.server_health_monitor_updated_label.setText("Refresh failed")
            self.server_health_monitor_cpu_summary.setToolTip(str(exc))
            self.server_health_monitor_memory_summary.setToolTip(str(exc))
        finally:
            cursor.close()
            self.server_health_monitor_refresh_button.setEnabled(True)

    def show_server_health_overview(self):
        """Open the compact server health dashboard and refresh its sections."""
        if hasattr(self, "server_health_monitor_timer"):
            self.server_health_monitor_timer.stop()
        if not self._ensure_connection():
            return
        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
            self.jobs_panel, self.timeline_panel, self.steps_timeline_panel,
            self.processes_panel, self.cpu_panel, self.server_health_monitor_panel,
        ):
            widget.hide()
        for name in ("view_structure_panel",):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self._leave_server_health_monitor()
        self._set_view_mode("server_health")
        self.server_health_panel.show()
        self.refresh_server_health_overview()

    def _set_health_table_message(self, table, message):
        table.setSortingEnabled(False)
        table.clearContents()
        table.setRowCount(1)
        item = QTableWidgetItem(message)
        item.setToolTip(message)
        table.setItem(0, 0, item)
        table.setSortingEnabled(True)

    def refresh_server_health_overview(self):
        """Refresh SQL Server health indicators, resource charts and operational summaries."""
        if not self._ensure_connection():
            return
        self.server_health_refresh_button.setEnabled(False)
        self.server_health_updated_label.setText("Refreshing...")
        connection = self._db.get_connection()
        cursor = connection.cursor()
        try:
            # Current user connections/sessions.
            try:
                cursor.execute("""
SELECT COUNT(*)
FROM sys.dm_exec_sessions
WHERE is_user_process = 1;
""")
                connection_count = int(cursor.fetchone()[0] or 0)
                self.server_health_connections_label.setText(
                    f"SQL Server connections: {connection_count:,}"
                )
            except pyodbc.Error as exc:
                self.server_health_connections_label.setText(
                    "SQL Server connections: unavailable"
                )
                self.server_health_connections_label.setToolTip(str(exc))

            # SQL Server instance uptime.
            try:
                cursor.execute("SELECT sqlserver_start_time FROM sys.dm_os_sys_info;")
                uptime_row = cursor.fetchone()
                if uptime_row and uptime_row[0]:
                    started = uptime_row[0]
                    elapsed = datetime.now() - started
                    total_seconds = max(0, int(elapsed.total_seconds()))
                    days, remainder = divmod(total_seconds, 86400)
                    hours, remainder = divmod(remainder, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    self.server_health_uptime_label.setText(
                        f"SQL Server uptime: {days}d {hours:02d}h {minutes:02d}m"
                    )
                    self.server_health_uptime_label.setToolTip(
                        f"Instance started: {started:%Y-%m-%d %H:%M:%S}"
                    )
                else:
                    self.server_health_uptime_label.setText("SQL Server uptime: unavailable")
            except pyodbc.Error as exc:
                self.server_health_uptime_label.setText("SQL Server uptime: unavailable")
                self.server_health_uptime_label.setToolTip(str(exc))

            # Deadlock reports retained in the system_health Extended Events ring buffer.
            try:
                cursor.execute(r"""
;WITH SystemHealthTarget AS
(
    SELECT CAST(t.target_data AS XML) AS TargetData
    FROM sys.dm_xe_session_targets AS t
    INNER JOIN sys.dm_xe_sessions AS s
        ON s.address = t.event_session_address
    WHERE s.name = N'system_health'
      AND t.target_name = N'ring_buffer'
)
SELECT
    COUNT(*) AS DeadlockCount,
    MAX(Evt.value('@timestamp', 'datetime2')) AS LastDeadlockUtc
FROM SystemHealthTarget
CROSS APPLY TargetData.nodes(
    '/RingBufferTarget/event[@name="xml_deadlock_report"]'
) AS X(Evt);
""")
                deadlock_row = cursor.fetchone()
                deadlock_count = int(deadlock_row[0] or 0) if deadlock_row else 0
                last_deadlock = deadlock_row[1] if deadlock_row else None
                self.server_health_deadlocks_label.setText(
                    f"Deadlocks (system_health): {deadlock_count:,}"
                )
                self.server_health_deadlocks_label.setToolTip(
                    f"Most recent retained deadlock (UTC): {last_deadlock}"
                    if last_deadlock else
                    "No deadlock reports currently retained in the system_health ring buffer."
                )
            except pyodbc.Error as exc:
                self.server_health_deadlocks_label.setText("Deadlocks: unavailable")
                self.server_health_deadlocks_label.setToolTip(str(exc))

            # Latest successful full, differential and transaction-log backups per database.
            try:
                cursor.execute(r"""
SELECT
    d.name AS DatabaseName,
    MAX(CASE WHEN b.type = 'D' THEN b.backup_finish_date END) AS LastFull,
    MAX(CASE WHEN b.type = 'I' THEN b.backup_finish_date END) AS LastDifferential,
    MAX(CASE WHEN b.type = 'L' THEN b.backup_finish_date END) AS LastLog
FROM sys.databases AS d
LEFT JOIN msdb.dbo.backupset AS b
    ON b.database_name = d.name
   AND b.backup_finish_date IS NOT NULL
WHERE d.database_id > 4
GROUP BY d.name
ORDER BY d.name;
""")
                backup_rows = cursor.fetchall()
                backup_table = self.server_health_backup_table
                backup_table.setSortingEnabled(False)
                backup_table.setRowCount(len(backup_rows))
                for ri, row in enumerate(backup_rows):
                    values = [row[0]] + [
                        value.strftime("%Y-%m-%d %H:%M") if value else "Never"
                        for value in row[1:4]
                    ]
                    for ci, value in enumerate(values):
                        cell = QTableWidgetItem(str(value))
                        if ci > 0 and value == "Never":
                            cell.setForeground(QColor("#D93025"))
                        backup_table.setItem(ri, ci, cell)
                backup_table.resizeColumnsToContents()
                backup_table.setSortingEnabled(True)
                desired_height = min(145, max(85, backup_table.horizontalHeader().height()
                                               + len(backup_rows) * backup_table.verticalHeader().defaultSectionSize()
                                               + 12))
                backup_table.setMinimumHeight(desired_height)
                backup_table.setMaximumHeight(desired_height)
            except pyodbc.Error as exc:
                self._set_health_table_message(
                    self.server_health_backup_table,
                    "Backup data unavailable: " + str(exc)[:160]
                )

            # CPU history uses the same ring-buffer source as Processes > CPU Usage.
            try:
                cursor.execute(r"""
SELECT TOP (360)
    DATEADD(ms, -1 * (si.ms_ticks - rb.[timestamp]), GETDATE()) AS SampleTime,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS SqlCpu,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS IdleCpu,
    100
      - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int')
      - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS OtherCpu
FROM
(
    SELECT [timestamp], CONVERT(xml, record) AS record
    FROM sys.dm_os_ring_buffers
    WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
      AND record LIKE '%<SystemHealth>%'
) AS rb
CROSS JOIN sys.dm_os_sys_info AS si
ORDER BY rb.[timestamp] DESC;
""")
                cpu_samples = cursor.fetchall()
                history = [
                    (sample[0], int(sample[1] or 0), int(sample[3] or 0), int(sample[2] or 0))
                    for sample in reversed(cpu_samples) if sample[1] is not None
                ][-360:]
                self.server_health_cpu_chart.set_points(history)
                if history:
                    latest = history[-1]
                    self.server_health_cpu_summary.setText(
                        f"SQL Server: {latest[1]}%  |  Other: {latest[2]}%  |  Idle: {latest[3]}%"
                    )
                else:
                    self.server_health_cpu_summary.setText("CPU samples are not currently available.")
            except pyodbc.Error as exc:
                self.server_health_cpu_chart.set_points([])
                self.server_health_cpu_summary.setText("CPU data unavailable: " + str(exc)[:180])

            # Current physical memory and SQL Server process memory.
            try:
                cursor.execute(r"""
SELECT
    total_physical_memory_kb / 1024.0 AS TotalPhysicalMB,
    available_physical_memory_kb / 1024.0 AS AvailablePhysicalMB,
    system_memory_state_desc
FROM sys.dm_os_sys_memory;
""")
                os_memory = cursor.fetchone()
                cursor.execute(r"""
SELECT physical_memory_in_use_kb / 1024.0 AS SqlProcessPhysicalMB
FROM sys.dm_os_process_memory;
""")
                process_memory = cursor.fetchone()
                if os_memory:
                    total_physical_mb = float(os_memory[0] or 0.0)
                    self.server_health_total_memory_label.setText(
                        f"Total physical memory: {total_physical_mb / 1024.0:,.2f} GB"
                    )
                    self.server_health_memory_chart.set_data(
                        total_physical_mb, os_memory[1], process_memory[0] if process_memory else 0,
                        str(os_memory[2] or "Current memory snapshot")
                    )
                else:
                    self.server_health_total_memory_label.setText("Total physical memory: unavailable")
                    self.server_health_memory_chart.set_data(0, 0, 0, "Memory data unavailable.")
            except pyodbc.Error as exc:
                self.server_health_total_memory_label.setText("Total physical memory: unavailable")
                self.server_health_memory_chart.set_data(0, 0, 0, "Memory data unavailable: " + str(exc)[:120])

            # Disk space for volumes that contain SQL Server database files.
            try:
                cursor.execute(r"""
SELECT DISTINCT
    vs.volume_mount_point AS Drive,
    vs.file_system_type AS FileSystem,
    CAST(vs.total_bytes / 1073741824.0 AS DECIMAL(18, 2)) AS TotalGB,
    CAST(vs.available_bytes / 1073741824.0 AS DECIMAL(18, 2)) AS FreeGB,
    CAST(100.0 * vs.available_bytes / NULLIF(vs.total_bytes, 0) AS DECIMAL(5, 2)) AS FreePercent
FROM sys.master_files AS mf
CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS vs
ORDER BY Drive;
""")
                disk_rows = cursor.fetchall()
                self.server_health_disk_table.setSortingEnabled(False)
                self.server_health_disk_table.setRowCount(len(disk_rows))
                for ri, row in enumerate(disk_rows):
                    vals = [row[0] or "Unknown", row[1] or "Unknown", row[2], row[3], row[4]]
                    for ci, value in enumerate(vals):
                        text = "N/A" if value is None else (f"{float(value):,.2f}" if ci >= 2 else str(value))
                        item = SortableTableWidgetItem(text)
                        if ci >= 2 and value is not None:
                            item.setData(Qt.ItemDataRole.UserRole, float(value))
                            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        if ci == 4 and value is not None:
                            pct = float(value)
                            item.setForeground(QColor("#D93025") if pct < 10 else QColor("#F9AB00") if pct < 15 else self.palette().text().color())
                        self.server_health_disk_table.setItem(ri, ci, item)
                self.server_health_disk_table.resizeColumnsToContents()
                # Expand the Disk space section to show every discovered volume.
                # Include the header, all data rows and a small frame allowance.
                disk_table = self.server_health_disk_table
                row_height = disk_table.verticalHeader().defaultSectionSize()
                header_height = disk_table.horizontalHeader().height()
                disk_table_height = header_height + len(disk_rows) * row_height + 12
                disk_table_height = max(90, disk_table_height)
                disk_table.setMinimumHeight(disk_table_height)
                disk_table.setMaximumHeight(disk_table_height)
            except pyodbc.Error as exc:
                self._set_health_table_message(self.server_health_disk_table, "Disk data unavailable: " + str(exc)[:160])
            finally:
                self.server_health_disk_table.setSortingEnabled(True)

            # Compact used/free database bars, reusing the same source as Database space compared.
            try:
                cursor.execute("""
SELECT d.name
FROM sys.databases AS d
WHERE d.state_desc = 'ONLINE' AND HAS_DBACCESS(d.name) = 1
ORDER BY d.name;
""")
                database_names = [str(row[0]) for row in cursor.fetchall()]
                db_results = []
                for database_name in database_names:
                    quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
                    cursor.execute(f"USE {quoted_db};")
                    cursor.execute("""
SELECT
    COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN size END), 0) * 8.0 / 1024.0 AS DataTotalMB,
    COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN FILEPROPERTY(name, 'SpaceUsed') END), 0) * 8.0 / 1024.0 AS DataUsedMB,
    COALESCE(SUM(CASE WHEN type_desc = 'LOG' THEN size END), 0) * 8.0 / 1024.0 AS LogTotalMB
FROM sys.database_files;
""")
                    file_row = cursor.fetchone()
                    data_total = max(0.0, float(file_row[0] or 0.0))
                    data_used = min(data_total, max(0.0, float(file_row[1] or 0.0)))
                    log_total = max(0.0, float(file_row[2] or 0.0))
                    cursor.execute("SELECT used_log_space_in_bytes / 1048576.0 FROM sys.dm_db_log_space_usage;")
                    log_row = cursor.fetchone()
                    log_used = min(log_total, max(0.0, float(log_row[0] or 0.0)) if log_row else 0.0)
                    db_results.append((database_name, data_total, data_used, log_total, log_used))
                max_total = max((r[1] + r[3] for r in db_results), default=0.0)
                db_table = self.server_health_database_table
                db_table.setSortingEnabled(False)
                db_table.setRowCount(len(db_results))
                for ri, (name, data_total, data_used, log_total, log_used) in enumerate(db_results):
                    name_item = QTableWidgetItem(name)
                    name_item.setToolTip(name)
                    db_table.setItem(ri, 0, name_item)
                    data_bar = ProportionalDatabaseBar(data_total, data_used, 0, 0, max_total, db_table)
                    data_bar.setMaximumHeight(18)
                    data_bar.setMinimumHeight(16)
                    data_bar.setToolTip(f"Data total: {data_total:,.2f} MB\nUsed: {data_used:,.2f} MB\nFree: {max(0, data_total-data_used):,.2f} MB")
                    db_table.setCellWidget(ri, 1, data_bar)
                    log_bar = ProportionalDatabaseBar(0, 0, log_total, log_used, max_total, db_table)
                    log_bar.setMaximumHeight(18)
                    log_bar.setMinimumHeight(16)
                    log_bar.setToolTip(f"Log total: {log_total:,.2f} MB\nUsed: {log_used:,.2f} MB\nFree: {max(0, log_total-log_used):,.2f} MB")
                    db_table.setCellWidget(ri, 2, log_bar)
                    total_item = SortableTableWidgetItem(f"{data_total + log_total:,.2f}")
                    total_item.setData(Qt.ItemDataRole.UserRole, data_total + log_total)
                    total_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    db_table.setItem(ri, 3, total_item)
                    db_table.setRowHeight(ri, 23)
                db_table.resizeColumnsToContents()
                db_table.setColumnWidth(1, 240)
                db_table.setColumnWidth(2, 240)
                # Keep every database row visible; the dashboard scroll area will
                # scroll to the lower sections instead of clipping this table.
                db_table_height = db_table.horizontalHeader().height() + sum(
                    db_table.rowHeight(row_index) for row_index in range(db_table.rowCount())
                ) + 8
                db_table.setMinimumHeight(max(75, db_table_height))
                db_table.setMaximumHeight(max(10000, db_table_height))
            except pyodbc.Error as exc:
                self._set_health_table_message(self.server_health_database_table, "Database space data unavailable: " + str(exc)[:160])
            finally:
                self.server_health_database_table.setSortingEnabled(True)
                # Restore the connection context for the other dashboard queries.
                try:
                    cursor.execute("USE master;")
                except pyodbc.Error:
                    pass

            # Only show requests currently blocked by another session.
            try:
                cursor.execute(r"""
SELECT
    r.session_id,
    DB_NAME(r.database_id) AS DatabaseName,
    r.blocking_session_id AS BlockedBy,
    r.wait_time AS WaitMs,
    r.wait_type AS WaitType,
    SUBSTRING(st.text,
        (r.statement_start_offset / 2) + 1,
        ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(st.text) ELSE r.statement_end_offset END
          - r.statement_start_offset) / 2) + 1) AS SqlStatement
FROM sys.dm_exec_requests AS r
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) AS st
WHERE r.blocking_session_id > 0
ORDER BY r.wait_time DESC;
""")
                blocked_rows = cursor.fetchall()
                table = self.server_health_blocked_table
                table.setSortingEnabled(False)
                table.setRowCount(len(blocked_rows))
                for ri, row in enumerate(blocked_rows):
                    vals = [row[0], row[1] or "", row[2], row[3], row[4] or "", (row[5] or "").strip()]
                    for ci, value in enumerate(vals):
                        text = "" if value is None else (f"{int(value):,}" if ci in (0, 2, 3) else str(value))
                        item = SortableTableWidgetItem(text)
                        if ci in (0, 2, 3) and value is not None:
                            item.setData(Qt.ItemDataRole.UserRole, int(value))
                        item.setToolTip(text)
                        table.setItem(ri, ci, item)
                table.resizeColumnsToContents()
                table.setColumnWidth(5, 360)
                if not blocked_rows:
                    self._set_health_table_message(table, "No blocked requests currently detected.")
            except pyodbc.Error as exc:
                self._set_health_table_message(self.server_health_blocked_table, "Blocked-process data unavailable: " + str(exc)[:160])
            finally:
                self.server_health_blocked_table.setSortingEnabled(True)

            # Failed SQL Server Agent jobs in the previous 24 hours.
            try:
                cursor.execute(r"""
WITH FailedRuns AS
(
    SELECT j.name AS JobName,
           msdb.dbo.agent_datetime(h.run_date, h.run_time) AS EndTime,
           (h.run_duration / 10000) * 3600 + ((h.run_duration % 10000) / 100) * 60 + (h.run_duration % 100) AS DurationSeconds,
           h.message AS ErrorMessage
    FROM msdb.dbo.sysjobhistory AS h
    INNER JOIN msdb.dbo.sysjobs AS j ON j.job_id = h.job_id
    WHERE h.step_id = 0 AND h.run_status = 0 AND h.run_date > 0
)
SELECT JobName, EndTime,
       CAST(DurationSeconds / 3600 AS varchar(10)) + ':'
       + RIGHT('00' + CAST((DurationSeconds % 3600) / 60 AS varchar(2)), 2) + ':'
       + RIGHT('00' + CAST(DurationSeconds % 60 AS varchar(2)), 2) AS Duration,
       ErrorMessage
FROM FailedRuns
WHERE EndTime >= DATEADD(HOUR, -24, GETDATE()) AND EndTime <= GETDATE()
ORDER BY EndTime DESC;
""")
                failed_rows = cursor.fetchall()
                table = self.server_health_failed_jobs_table
                table.setSortingEnabled(False)
                table.setRowCount(len(failed_rows))
                for ri, row in enumerate(failed_rows):
                    vals = [str(row[0] or ""), str(row[1] or ""), str(row[2] or ""), str(row[3] or "")]
                    for ci, value in enumerate(vals):
                        item = QTableWidgetItem(value)
                        item.setToolTip(value)
                        if ci == 3:
                            item.setForeground(QColor("#D93025"))
                        table.setItem(ri, ci, item)
                table.resizeColumnsToContents()
                table.setColumnWidth(3, 450)
                if not failed_rows:
                    self._set_health_table_message(table, "No failed jobs recorded in the last 24 hours.")
            except pyodbc.Error as exc:
                self._set_health_table_message(self.server_health_failed_jobs_table, "Failed-job data unavailable: " + str(exc)[:160])
            finally:
                self.server_health_failed_jobs_table.setSortingEnabled(True)

            self.server_health_updated_label.setText(f"Last updated: {datetime.now():%Y-%m-%d %H:%M:%S}")
            self.statusBar().showMessage("Server Health Overview refreshed.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Server Health Overview", "Could not refresh the dashboard.\n\n" + str(exc))
            self.server_health_updated_label.setText("Refresh failed")
        finally:
            cursor.close()
            self.server_health_refresh_button.setEnabled(True)

    def show_server_information(self):
        if hasattr(self, "server_health_monitor_timer"):
            self.server_health_monitor_timer.stop()
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "server_health_monitor_panel"):
            self.server_health_monitor_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Display basic server and host information using read-only metadata queries."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if self._db is None or not self._db.is_connected():
            # _ensure_connection() calls show_server_information() after a
            # successful connection, so return here to avoid opening it twice.
            self._ensure_connection()
            return
        properties = []
        connection = self._db.get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute("""
                SELECT
                    CAST(CONNECTIONPROPERTY('local_net_address') AS nvarchar(128)) AS IPAddress,
                    CAST(SERVERPROPERTY('MachineName') AS nvarchar(128)) AS MachineName,
                    CAST(SERVERPROPERTY('ServerName') AS nvarchar(128)) AS ServerName,
                    CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS Edition,
                    CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS ProductVersion,
                    CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)) AS ProductLevel,
                    CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(128)) AS ProductUpdateLevel,
                    CAST(SERVERPROPERTY('Collation') AS nvarchar(128)) AS ServerCollation,
                    cpu_count,
                    hyperthread_ratio,
                    physical_memory_kb,
                    committed_kb,
                    sqlserver_start_time
                FROM sys.dm_os_sys_info;
            """)
            row = cursor.fetchone()
            if row:
                columns = [c[0] for c in cursor.description]
                info = dict(zip(columns, row))
                ip_address = info.get("IPAddress") or "Not available (local/shared-memory connection)"
                properties.extend([
                    ("Server", info.get("ServerName") or self._settings.server),
                    ("Machine name", info.get("MachineName") or "Unknown"),
                    ("IP Address", ip_address),
                    ("Operating system", self._get_operating_system(cursor)),
                    ("SQL Server version", " ".join(str(x) for x in [
                        info.get("ProductVersion") or "",
                        info.get("ProductLevel") or "",
                        info.get("ProductUpdateLevel") or ""
                    ] if x and str(x).strip())),
                    ("Edition", info.get("Edition") or "Unknown"),
                    ("Server Collation", info.get("ServerCollation") or "Unknown"),
                    ("Processors (logical CPUs)", str(info.get("cpu_count") or "Unknown")),
                    ("Hyperthread ratio", str(info.get("hyperthread_ratio") or "Unknown")),
                    ("Physical memory", self._format_memory_mb(info.get("physical_memory_kb"))),
                    ("SQL Server committed memory", self._format_memory_mb(info.get("committed_kb"))),
                    ("SQL Server start time", str(info.get("sqlserver_start_time") or "Unknown")),
                    ("Connected database", self._settings.database),
                    ("Authentication", str(
                        getattr(self._settings, "auth_type",
                                getattr(self._settings, "authentication",
                                        getattr(self._settings, "auth_mode", "Configured")))
                    )),
                    ("Connection encryption", str(getattr(self._settings, "encrypt", "Default"))),
                ])
        finally:
            cursor.close()

        self._leave_server_health_monitor()
        self._set_view_mode("server")
        self.table.hide()
        self.space_scroll.hide()
        self.tables_panel.hide()
        self.triggers_panel.hide()
        self.indexes_panel.hide()
        self.programmability_panel.hide()
        self.jobs_panel.hide()
        self.processes_panel.hide()
        self.cpu_panel.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.server_info_panel.show()
        self.server_info_table.setSortingEnabled(False)
        self.server_info_table.setRowCount(len(properties))
        self.server_info_table.setColumnCount(2)
        self.server_info_table.setHorizontalHeaderLabels(["Property", "Value"])
        for i, (name, value) in enumerate(properties):
            self.server_info_table.setItem(i, 0, QTableWidgetItem(str(name)))
            self.server_info_table.setItem(i, 1, QTableWidgetItem(str(value)))
        self.server_info_table.resizeRowsToContents()
        self.statusBar().showMessage(f"Connected to {self._settings.server}")

    @staticmethod
    def _format_memory_mb(value):
        if value is None:
            return "Unknown"
        return f"{float(value) / 1024:,.0f} MB ({float(value) / 1024 / 1024:,.2f} GB)"

    @staticmethod
    def _get_operating_system(cursor):
        # Available on newer SQL Server versions; guarded for older versions/permissions.
        try:
            cursor.execute("""
                IF OBJECT_ID('sys.dm_os_host_info') IS NOT NULL
                    SELECT host_platform + ' ' + host_distribution + ' ' + host_release
                    FROM sys.dm_os_host_info;
                ELSE
                    SELECT CAST(NULL AS nvarchar(256));
            """)
            row = cursor.fetchone()
            if row and row[0]:
                return str(row[0]).strip()
        except pyodbc.Error:
            pass
        try:
            cursor.execute("""
                SELECT windows_release, windows_service_pack_level, windows_sku
                FROM sys.dm_os_windows_info;
            """)
            row = cursor.fetchone()
            if row:
                return "Windows " + " ".join(str(v) for v in row if v and str(v).strip())
        except pyodbc.Error:
            pass
        return "Unavailable (SQL Server version/permissions)"

    def show_database_file_sizes(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return

        self.statusBar().showMessage("Loading database file sizes...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            connection = self._db.get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(DATABASE_FILES_QUERY)
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()

            self._show_table_view()
            self._populate_table(columns, rows)
            self.statusBar().showMessage(
                f"Loaded {len(rows):,} file(s) across databases."
            )
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(
                self,
                "Query failed",
                "Could not retrieve database file information.\n\n" + str(exc),
            )
            self.statusBar().showMessage("Query failed")
            # A failed connection can leave the stored connection unusable.
            if self._db is not None:
                self._db.disconnect()
                self._db = None
        finally:
            QApplication.restoreOverrideCursor()

    @staticmethod
    def _parse_space_value(value):
        """Convert sp_spaceused values such as '123.00 MB' or '4096 KB' to MB."""
        if value is None:
            return 0.0
        text = str(value).strip().upper().replace(',', '')
        parts = text.split()
        if not parts:
            return 0.0
        try:
            number = float(parts[0])
        except ValueError:
            return 0.0
        unit = parts[1] if len(parts) > 1 else 'KB'
        if unit.startswith('GB'):
            return number * 1024.0
        if unit.startswith('MB'):
            return number
        if unit.startswith('KB'):
            return number / 1024.0
        if unit.startswith('B'):
            return number / (1024.0 * 1024.0)
        return number

    def _show_table_view(self):
        self._leave_server_health_monitor()
        self._set_view_mode("table")
        self.space_scroll.hide()
        self.tables_panel.hide()
        self.server_info_panel.hide()
        self.triggers_panel.hide()
        self.indexes_panel.hide()
        self.programmability_panel.hide()
        self.jobs_panel.hide()
        self.processes_panel.hide()
        self.cpu_panel.hide()
        self.table.show()
        self.total_gb_label.show()
        self.total_mb_label.show()

    def _show_space_view(self):
        self._leave_server_health_monitor()
        self._set_view_mode("space")
        self.table.hide()
        self.tables_panel.hide()
        self.server_info_panel.hide()
        self.triggers_panel.hide()
        self.indexes_panel.hide()
        self.programmability_panel.hide()
        self.jobs_panel.hide()
        self.processes_panel.hide()
        self.cpu_panel.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.space_scroll.show()

    def show_space_usage(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Load per-database data-file and transaction-log space usage."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return

        self.statusBar().showMessage("Loading database space usage...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            connection = self._db.get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute("""
                    SELECT name
                    FROM sys.databases
                    WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
                    ORDER BY name;
                """)
                database_names = [row[0] for row in cursor.fetchall()]
                results = []
                for database_name in database_names:
                    quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
                    # Execute USE separately. Combining USE and a SELECT/EXEC in one
                    # pyodbc execute() can expose a no-result result set first, causing
                    # "No results. Previous SQL was not a query."
                    cursor.execute(f"USE {quoted_db};")
                    cursor.execute("EXEC sys.sp_spaceused @oneresultset = 1;")

                    # Stored procedures can return multiple result sets depending on
                    # SQL Server version/options. Advance to the first tabular result.
                    while cursor.description is None:
                        if not cursor.nextset():
                            break
                    if cursor.description is None:
                        continue
                    space_row = cursor.fetchone()
                    if space_row is None:
                        continue
                    space_columns = [column[0].lower() for column in cursor.description]
                    space_values = dict(zip(space_columns, space_row))
                    data_mb = self._parse_space_value(space_values.get("data"))
                    index_mb = self._parse_space_value(space_values.get("index_size"))
                    unused_mb = self._parse_space_value(space_values.get("unused"))

                    cursor.execute("""
                        SELECT
                            COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN size END), 0) * 8.0 / 1024 AS DataFileMB,
                            COALESCE(SUM(CASE WHEN type_desc = 'LOG' THEN size END), 0) * 8.0 / 1024 AS LogFileMB
                        FROM sys.database_files;
                    """)
                    file_sizes = cursor.fetchone()
                    total_data_mb = float(file_sizes[0] or 0.0)
                    total_log_mb = float(file_sizes[1] or 0.0)
                    unallocated_mb = max(0.0, total_data_mb - data_mb - index_mb - unused_mb)

                    cursor.execute("""
                        SELECT
                            total_log_size_in_bytes / 1048576.0 AS TotalLogMB,
                            used_log_space_in_bytes / 1048576.0 AS UsedLogMB
                        FROM sys.dm_db_log_space_usage;
                    """)
                    log_row = cursor.fetchone()
                    if log_row is not None:
                        total_log_mb = float(log_row[0] or total_log_mb)
                        used_log_mb = min(total_log_mb, max(0.0, float(log_row[1] or 0.0)))
                    else:
                        used_log_mb = 0.0

                    results.append({
                        "name": database_name,
                        "data_total": total_data_mb,
                        "data": data_mb,
                        "indexes": index_mb,
                        "unused": unused_mb,
                        "unallocated": unallocated_mb,
                        "log_total": total_log_mb,
                        "log_used": used_log_mb,
                        "log_free": max(0.0, total_log_mb - used_log_mb),
                    })
            finally:
                cursor.close()

            self._space_usage_results = results
            self._render_space_usage(results)
            self.statusBar().showMessage(f"Loaded space usage for {len(results):,} database(s).")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(
                self,
                "Space usage query failed",
                "Could not retrieve database space usage.\n\n" + str(exc),
            )
            self.statusBar().showMessage("Space usage query failed")
            if self._db is not None:
                self._db.disconnect()
                self._db = None
        finally:
            QApplication.restoreOverrideCursor()

    def show_database_space_compared(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Compare allocated data/log space and show used versus free space."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return

        self.statusBar().showMessage("Loading database space comparison...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            connection = self._db.get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute("""
                    SELECT d.name
                    FROM sys.databases AS d
                    WHERE d.state_desc = 'ONLINE'
                      AND HAS_DBACCESS(d.name) = 1
                    ORDER BY d.name;
                """)
                database_names = [str(row[0]) for row in cursor.fetchall()]
                results = []
                for database_name in database_names:
                    quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
                    cursor.execute(f"USE {quoted_db};")
                    cursor.execute("""
                        SELECT
                            COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN size END), 0) * 8.0 / 1024.0 AS DataTotalMB,
                            COALESCE(SUM(CASE WHEN type_desc = 'ROWS' THEN FILEPROPERTY(name, 'SpaceUsed') END), 0) * 8.0 / 1024.0 AS DataUsedMB,
                            COALESCE(SUM(CASE WHEN type_desc = 'LOG' THEN size END), 0) * 8.0 / 1024.0 AS LogTotalMB
                        FROM sys.database_files;
                    """)
                    file_row = cursor.fetchone()
                    data_total_mb = max(0.0, float(file_row[0] or 0.0))
                    data_used_mb = min(data_total_mb, max(0.0, float(file_row[1] or 0.0)))
                    log_total_mb = max(0.0, float(file_row[2] or 0.0))

                    cursor.execute("""
                        SELECT used_log_space_in_bytes / 1048576.0 AS LogUsedMB
                        FROM sys.dm_db_log_space_usage;
                    """)
                    log_row = cursor.fetchone()
                    log_used_mb = min(
                        log_total_mb,
                        max(0.0, float(log_row[0] or 0.0)) if log_row else 0.0,
                    )
                    results.append({
                        "name": database_name,
                        "data_mb": data_total_mb,
                        "data_used_mb": data_used_mb,
                        "data_free_mb": max(0.0, data_total_mb - data_used_mb),
                        "log_mb": log_total_mb,
                        "log_used_mb": log_used_mb,
                        "log_free_mb": max(0.0, log_total_mb - log_used_mb),
                    })
            finally:
                cursor.close()

            self._render_database_space_compared(results)
            self.statusBar().showMessage(
                f"Loaded proportional space comparison for {len(results):,} database(s)."
            )
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(
                self,
                "Database space comparison failed",
                "Could not retrieve database space comparison.\n\n" + str(exc),
            )
            self.statusBar().showMessage("Database space comparison failed")
            if self._db is not None:
                self._db.disconnect()
                self._db = None
        finally:
            QApplication.restoreOverrideCursor()

    def _render_database_space_compared(self, databases):
        # Reuse the scrollable visual area while keeping this view independent
        # from the more detailed Space usage view.
        while self.space_layout.count():
            item = self.space_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        legend = QLabel(
            '<b>Legend:</b> '
            '<span style="color:#2878B5">■ Database used</span> &nbsp; '
            '<span style="color:#8CC8EE">■ Database free</span> &nbsp; '
            '<span style="color:#7256A8">■ Transaction log used</span> &nbsp; '
            '<span style="color:#C4B5E8">■ Transaction log free</span>'
            '<br><span style="color:#555">Each bar is scaled against the largest database total (data files + transaction log). Hover over a bar for exact values.</span>'
        )
        legend.setWordWrap(True)
        self.space_layout.addWidget(legend)

        if not databases:
            self.space_layout.addWidget(QLabel("No accessible online databases were found."))
            self._show_space_view()
            self._leave_server_health_monitor()
            self._set_view_mode("space_compared")
            return

        max_total = max((db["data_mb"] + db["log_mb"] for db in databases), default=0.0)
        for db in databases:
            total = db["data_mb"] + db["log_mb"]
            row = QWidget(self.space_content)
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(4, 3, 4, 3)
            row_layout.setSpacing(3)
            caption = QLabel(
                f"<b>{self._html_escape(db['name'])}</b> — "
                f"Total: {total:,.2f} MB "
                f"(Database: {db['data_mb']:,.2f} MB, free: {db['data_free_mb']:,.2f} MB; "
                f"Log: {db['log_mb']:,.2f} MB, free: {db['log_free_mb']:,.2f} MB)"
            )
            row_layout.addWidget(caption)
            bar = ProportionalDatabaseBar(
                db["data_mb"], db["data_used_mb"], db["log_mb"],
                db["log_used_mb"], max_total, row
            )
            row_layout.addWidget(bar)
            self.space_layout.addWidget(row)

        self.space_layout.addStretch(1)
        self._show_space_view()
        self._leave_server_health_monitor()
        self._set_view_mode("space_compared")

    def show_disk_space(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Show disk capacity for volumes that host SQL Server database files."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return

        self.statusBar().showMessage("Loading SQL Server disk space...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("""
                    SELECT DISTINCT
                        vs.volume_mount_point AS Drive,
                        vs.file_system_type AS FileSystem,
                        CAST(vs.total_bytes / 1073741824.0 AS DECIMAL(18, 2)) AS TotalGB,
                        CAST(vs.available_bytes / 1073741824.0 AS DECIMAL(18, 2)) AS FreeGB,
                        CAST(100.0 * vs.available_bytes / NULLIF(vs.total_bytes, 0)
                             AS DECIMAL(5, 2)) AS FreePercent
                    FROM sys.master_files AS mf
                    CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS vs
                    ORDER BY Drive;
                """)
                columns = [column[0] for column in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()

            self._leave_server_health_monitor()
            self._set_view_mode("disk")
            self.table.setSortingEnabled(False)
            self.table.clear()
            self.table.setColumnCount(6)
            self.table.setHorizontalHeaderLabels(
                ["Status", "Drive / mount point", "File system", "Total GB", "Free GB", "Free %"]
            )
            self.table.setRowCount(len(rows))
            for row_index, record in enumerate(rows):
                free_pct = float(record.get("FreePercent") or 0)
                status_item = QTableWidgetItem("■" if free_pct < 15 else "")
                status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if free_pct < 10:
                    status_item.setForeground(QColor("#D93025"))
                    status_item.setToolTip("Critical: less than 10% disk space is free")
                elif free_pct < 15:
                    status_item.setForeground(QColor("#F9AB00"))
                    status_item.setToolTip("Warning: less than 15% disk space is free")
                self.table.setItem(row_index, 0, status_item)

                values = [
                    record.get("Drive") or "Unknown",
                    record.get("FileSystem") or "Unknown",
                    record.get("TotalGB"),
                    record.get("FreeGB"),
                    record.get("FreePercent"),
                ]
                for column_index, value in enumerate(values, start=1):
                    if value is None:
                        text = "N/A"
                    elif column_index >= 3:
                        text = f"{float(value):,.2f}"
                    else:
                        text = str(value)
                    item = QTableWidgetItem(text)
                    if column_index >= 3 and value is not None:
                        item.setData(Qt.ItemDataRole.UserRole, float(value))
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.table.setItem(row_index, column_index, item)

            self.table.setSortingEnabled(True)
            self.table.resizeColumnsToContents()
            self.table.setColumnWidth(0, 65)
            self.table.horizontalHeader().setStretchLastSection(True)
            self.total_gb_label.hide()
            self.total_mb_label.hide()
            self.space_scroll.hide()
            self.tables_panel.hide()
            self.server_info_panel.hide()
            self.triggers_panel.hide()
            self.indexes_panel.hide()
            self.programmability_panel.hide()
            self.jobs_panel.hide()
            self.processes_panel.hide()
            self.cpu_panel.hide()
            self.table.show()
            if rows:
                self.statusBar().showMessage(f"Loaded disk space for {len(rows)} volume(s).")
            else:
                self.statusBar().showMessage("No disk volumes found for SQL Server database files.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(
                self,
                "Disk space query failed",
                "Could not retrieve disk space. The SQL Server login may need permission to "
                "read sys.dm_os_volume_stats.\n\n" + str(exc),
            )
            self.statusBar().showMessage("Disk space query failed")
        finally:
            QApplication.restoreOverrideCursor()

    def show_tables_size(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Open the table-size view and populate the database dropdown."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("""
                    SELECT name
                    FROM sys.databases
                    WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
                    ORDER BY name;
                """)
                names = [row[0] for row in cursor.fetchall()]
            finally:
                cursor.close()

            previous = self.tables_database_combo.currentText()
            self.tables_database_combo.clear()
            self.tables_database_combo.addItems(names)
            if previous in names:
                self.tables_database_combo.setCurrentText(previous)
            elif self._settings.database in names:
                self.tables_database_combo.setCurrentText(self._settings.database)

            self._leave_server_health_monitor()
            self._set_view_mode("tables")
            self.table.hide()
            self.space_scroll.hide()
            self.total_gb_label.hide()
            self.total_mb_label.hide()
            self.server_info_panel.hide()
            self.triggers_panel.hide()
            self.indexes_panel.hide()
            self.programmability_panel.hide()
            self.jobs_panel.hide()
            self.processes_panel.hide()
            self.cpu_panel.hide()
            self.tables_panel.show()
            if names:
                self.refresh_tables_size()
            else:
                self.tables_result.clear()
                self.tables_result.setRowCount(0)
                self.tables_result.setColumnCount(0)
                self.tables_total_mb_label.setText("Total size: 0.00 MB")
                self.statusBar().showMessage("No accessible online databases found.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Tables query failed",
                                 "Could not load database list.\\n\\n" + str(exc))

    def show_tables_with_triggers(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Open the tables-with-triggers view and populate the database dropdown."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("""
                    SELECT name
                    FROM sys.databases
                    WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
                    ORDER BY name;
                """)
                names = [row[0] for row in cursor.fetchall()]
            finally:
                cursor.close()

            previous = self.triggers_database_combo.currentText()
            self.triggers_database_combo.clear()
            self.triggers_database_combo.addItems(names)
            if previous in names:
                self.triggers_database_combo.setCurrentText(previous)
            elif self._settings.database in names:
                self.triggers_database_combo.setCurrentText(self._settings.database)

            self._leave_server_health_monitor()
            self._set_view_mode("triggers")
            self.table.hide()
            self.space_scroll.hide()
            self.tables_panel.hide()
            self.server_info_panel.hide()
            self.indexes_panel.hide()
            self.programmability_panel.hide()
            self.jobs_panel.hide()
            self.processes_panel.hide()
            self.cpu_panel.hide()
            self.total_gb_label.hide()
            self.total_mb_label.hide()
            self.triggers_panel.show()
            if names:
                self.refresh_tables_with_triggers()
            else:
                self.triggers_tree.clear()
                self.trigger_sql_text.clear()
                self.statusBar().showMessage("No accessible online databases found.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Tables with Triggers", "Could not load database list.\n\n" + str(exc))

    def show_tables_with_indexes(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Open the tables-with-indexes view and populate the database dropdown."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("""
                    SELECT name
                    FROM sys.databases
                    WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
                    ORDER BY name;
                """)
                names = [row[0] for row in cursor.fetchall()]
            finally:
                cursor.close()

            previous = self.indexes_database_combo.currentText()
            self.indexes_database_combo.clear()
            self.indexes_database_combo.addItems(names)
            if previous in names:
                self.indexes_database_combo.setCurrentText(previous)
            elif self._settings.database in names:
                self.indexes_database_combo.setCurrentText(self._settings.database)

            self._leave_server_health_monitor()
            self._set_view_mode("indexes")
            self.table.hide()
            self.space_scroll.hide()
            self.tables_panel.hide()
            self.server_info_panel.hide()
            self.triggers_panel.hide()
            self.indexes_panel.show()
            self.jobs_panel.hide()
            self.processes_panel.hide()
            self.cpu_panel.hide()
            self.total_gb_label.hide()
            self.total_mb_label.hide()
            if names:
                self.refresh_tables_with_indexes()
            else:
                self.indexes_tree.clear()
                self.indexes_list.setRowCount(0)
                self.index_details_text.clear()
                self.statusBar().showMessage("No accessible online databases found.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Tables With Indexes", "Could not load database list.\n\n" + str(exc))

    def refresh_tables_with_indexes(self):
        """Load tables and indexes, including size and index configuration metadata."""
        if not self._ensure_connection():
            return
        database_name = self.indexes_database_combo.currentText()
        if not database_name:
            QMessageBox.information(self, "Tables With Indexes", "Select a database first.")
            return
        quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
        self.statusBar().showMessage(f"Loading indexes from {database_name}...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(f"USE {quoted_db};")
                cursor.execute("""
                    SELECT
                        s.name AS SchemaName,
                        t.name AS TableName,
                        i.name AS IndexName,
                        i.type_desc AS IndexType,
                        i.is_primary_key AS IsPrimaryKey,
                        i.is_unique AS IsUnique,
                        i.is_unique_constraint AS IsUniqueConstraint,
                        i.is_disabled AS IsDisabled,
                        i.fill_factor AS [FillFactor],
                        i.is_padded AS IsPadded,
                        i.ignore_dup_key AS IgnoreDupKey,
                        i.allow_row_locks AS AllowRowLocks,
                        i.allow_page_locks AS AllowPageLocks,
                        i.has_filter AS HasFilter,
                        i.filter_definition AS FilterDefinition,
                        i.is_hypothetical AS IsHypothetical,
                        i.data_space_id AS DataSpaceId,
                        ds.name AS DataSpaceName,
                        CAST(COALESCE(SUM(ps.reserved_page_count), 0) * 8.0 / 1024 AS decimal(19,2)) AS SizeMB,
                        COALESCE(SUM(ps.row_count), 0) AS [RowCount]
                    FROM sys.tables AS t
                    INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
                    INNER JOIN sys.indexes AS i ON i.object_id = t.object_id AND i.index_id > 0
                    LEFT JOIN sys.dm_db_partition_stats AS ps
                        ON ps.object_id = i.object_id AND ps.index_id = i.index_id
                    LEFT JOIN sys.data_spaces AS ds ON ds.data_space_id = i.data_space_id
                    WHERE t.is_ms_shipped = 0
                    GROUP BY s.name, t.name, i.name, i.type_desc, i.is_primary_key,
                        i.is_unique, i.is_unique_constraint, i.is_disabled, i.fill_factor,
                        i.is_padded, i.ignore_dup_key, i.allow_row_locks, i.allow_page_locks,
                        i.has_filter, i.filter_definition, i.is_hypothetical, i.data_space_id, ds.name
                    ORDER BY s.name, t.name, i.name;
                """)
                rows = cursor.fetchall()
            finally:
                cursor.close()

            self._all_index_rows = list(rows)
            self._render_index_rows()
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Index query failed", "Could not retrieve table indexes.\n\n" + str(exc))
            self.statusBar().showMessage("Index query failed")
        finally:
            QApplication.restoreOverrideCursor()

    def _apply_index_filters(self, *_args):
        """Apply table-name and minimum index-size filters to loaded index metadata."""
        if hasattr(self, "_all_index_rows"):
            self._render_index_rows()

    def _render_index_rows(self):
        """Render indexes matching the current filters."""
        self.indexes_tree.clear()
        self.indexes_list.setSortingEnabled(False)
        self.indexes_list.setRowCount(0)
        self.index_details_text.clear()
        table_filter = self.indexes_table_filter.text().strip().casefold()
        min_size_mb = float(self.indexes_size_filter.value())
        table_nodes = {}
        visible_count = 0
        total_index_size_mb = 0.0
        for row in getattr(self, "_all_index_rows", []):
            (schema_name, table_name, index_name, index_type, is_primary_key, is_unique,
             is_unique_constraint, is_disabled, fill_factor, is_padded, ignore_dup_key,
             allow_row_locks, allow_page_locks, has_filter, filter_definition,
             is_hypothetical, data_space_id, data_space_name, size_mb, row_count) = row
            full_table_name = f"{schema_name}.{table_name}"
            if table_filter and table_filter not in full_table_name.casefold():
                continue
            size_value = float(size_mb or 0)
            if min_size_mb > 0 and size_value <= min_size_mb:
                continue
            table_key = (schema_name, table_name)
            if table_key not in table_nodes:
                table_item = QTreeWidgetItem([full_table_name])
                table_item.setData(0, Qt.ItemDataRole.UserRole, {"kind": "table"})
                self.indexes_tree.addTopLevelItem(table_item)
                table_nodes[table_key] = table_item
            index_item = QTreeWidgetItem([index_name or f"(unnamed index {index_type})"])
            index_item.setData(0, Qt.ItemDataRole.UserRole, {
                "kind": "index", "schema": schema_name, "table": table_name,
                "name": index_name or "(unnamed)", "type": index_type,
                "primary_key": bool(is_primary_key), "unique": bool(is_unique),
                "unique_constraint": bool(is_unique_constraint), "disabled": bool(is_disabled),
                "fill_factor": fill_factor, "padded": bool(is_padded),
                "ignore_dup_key": bool(ignore_dup_key), "allow_row_locks": bool(allow_row_locks),
                "allow_page_locks": bool(allow_page_locks), "has_filter": bool(has_filter),
                "filter_definition": filter_definition, "hypothetical": bool(is_hypothetical),
                "data_space_id": data_space_id, "data_space_name": data_space_name,
                "size_mb": size_mb, "row_count": row_count,
            })
            if is_disabled:
                index_item.setForeground(0, QColor("#D93025"))
                index_item.setToolTip(0, "Index is disabled")
            else:
                index_item.setToolTip(0, f"{index_type}; {size_value:.2f} MB")
            table_nodes[table_key].addChild(index_item)
            list_row = self.indexes_list.rowCount()
            self.indexes_list.insertRow(list_row)
            list_values = [schema_name, table_name, index_name or "(unnamed)", index_type, f"{size_value:,.2f}"]
            for col, value in enumerate(list_values):
                cell = SortableTableWidgetItem(str(value))
                if col == 4:
                    cell.setData(Qt.ItemDataRole.UserRole, size_value)
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.indexes_list.setItem(list_row, col, cell)
            self.indexes_list.item(list_row, 0).setData(Qt.ItemDataRole.UserRole + 1, {
                "kind": "index", "schema": schema_name, "table": table_name,
                "name": index_name or "(unnamed)", "type": index_type,
                "primary_key": bool(is_primary_key), "unique": bool(is_unique),
                "unique_constraint": bool(is_unique_constraint), "disabled": bool(is_disabled),
                "fill_factor": fill_factor, "padded": bool(is_padded),
                "ignore_dup_key": bool(ignore_dup_key), "allow_row_locks": bool(allow_row_locks),
                "allow_page_locks": bool(allow_page_locks), "has_filter": bool(has_filter),
                "filter_definition": filter_definition, "hypothetical": bool(is_hypothetical),
                "data_space_id": data_space_id, "data_space_name": data_space_name,
                "size_mb": size_mb, "row_count": row_count,
            })
            visible_count += 1
            total_index_size_mb += size_value
        self.indexes_list.setSortingEnabled(True)
        self.indexes_list.sortItems(4, Qt.SortOrder.DescendingOrder)
        self.indexes_list.resizeColumnsToContents()
        self.indexes_tree.expandAll()
        self.indexes_tree.resizeColumnToContents(0)
        db_name = self.indexes_database_combo.currentText()
        if total_index_size_mb >= 1024:
            total_size_text = f"{total_index_size_mb / 1024:,.2f} GB ({total_index_size_mb:,.2f} MB)"
        else:
            total_size_text = f"{total_index_size_mb:,.2f} MB"
        self.indexes_total_label.setText(
            f"Total index size for displayed indexes: {total_size_text} | "
            f"{visible_count:,} index(es) across {len(table_nodes):,} table(s)"
        )
        self.statusBar().showMessage(
            f"Showing {visible_count:,} index(es) on {len(table_nodes):,} table(s) from {db_name}."
        )
        if visible_count == 0:
            self.index_details_text.setPlainText("No indexes match the current filters.")

    def _on_index_list_selection_changed(self):
        items = self.indexes_list.selectedItems()
        if not items:
            return
        data = self.indexes_list.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole + 1) or {}
        self._show_index_details(data)

    def _show_index_details(self, data):
        """Display index properties in the details pane."""
        if not data or data.get("kind") != "index":
            self.index_details_text.clear()
            return
        yes_no = lambda value: "Yes" if value else "No"
        details = [
            f"Index: {data.get('name')}",
            f"Table: [{data.get('schema')}].[{data.get('table')}]",
            f"Type: {data.get('type')}",
            f"Size: {float(data.get('size_mb') or 0):,.2f} MB",
            f"Rows (index partitions total): {int(data.get('row_count') or 0):,}",
            f"Fill factor: {data.get('fill_factor') or 0}% (0 means SQL Server default)",
            f"Primary key: {yes_no(data.get('primary_key'))}",
            f"Unique: {yes_no(data.get('unique'))}",
            f"Unique constraint: {yes_no(data.get('unique_constraint'))}",
            f"Disabled: {yes_no(data.get('disabled'))}",
            f"Padded: {yes_no(data.get('padded'))}",
            f"Ignore duplicate key: {yes_no(data.get('ignore_dup_key'))}",
            f"Allow row locks: {yes_no(data.get('allow_row_locks'))}",
            f"Allow page locks: {yes_no(data.get('allow_page_locks'))}",
            f"Filtered index: {yes_no(data.get('has_filter'))}",
            f"Filter definition: {data.get('filter_definition') or '(none)'}",
            f"Hypothetical: {yes_no(data.get('hypothetical'))}",
            f"Data space: {data.get('data_space_name') or '(unknown)'}",
            f"Data space ID: {data.get('data_space_id')}",
        ]
        self.index_details_text.setPlainText("\n".join(details))

    def _on_index_tree_selection_changed(self, current, previous):
        """Display the selected index properties in the details text box."""
        if current is None:
            self.index_details_text.clear()
            return
        data = current.data(0, Qt.ItemDataRole.UserRole) or {}
        self._show_index_details(data)

    def refresh_tables_with_triggers(self):
        """Load tables and their DML triggers, including definitions where visible."""
        if not self._ensure_connection():
            return
        database_name = self.triggers_database_combo.currentText()
        if not database_name:
            QMessageBox.information(self, "Tables with Triggers", "Select a database first.")
            return
        quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
        self.statusBar().showMessage(f"Loading triggers from {database_name}...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(f"USE {quoted_db};")
                cursor.execute("""
                    SELECT
                        s.name AS SchemaName,
                        t.name AS TableName,
                        tr.name AS TriggerName,
                        tr.is_disabled AS IsDisabled,
                        tr.is_instead_of_trigger AS IsInsteadOf,
                        OBJECT_DEFINITION(tr.object_id) AS TriggerDefinition
                    FROM sys.triggers AS tr
                    INNER JOIN sys.tables AS t ON t.object_id = tr.parent_id
                    INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
                    WHERE tr.parent_class = 1
                      AND tr.is_ms_shipped = 0
                    ORDER BY s.name, t.name, tr.name;
                """)
                rows = cursor.fetchall()
            finally:
                cursor.close()

            self.triggers_tree.clear()
            self.trigger_sql_text.clear()
            table_nodes = {}
            for row in rows:
                schema_name, table_name, trigger_name, is_disabled, is_instead_of, definition = row
                table_key = (schema_name, table_name)
                if table_key not in table_nodes:
                    table_item = QTreeWidgetItem([f"{schema_name}.{table_name}"])
                    table_item.setData(0, Qt.ItemDataRole.UserRole, {"kind": "table"})
                    self.triggers_tree.addTopLevelItem(table_item)
                    table_nodes[table_key] = table_item
                trigger_item = QTreeWidgetItem([trigger_name])
                trigger_item.setData(0, Qt.ItemDataRole.UserRole, {
                    "kind": "trigger",
                    "definition": definition or "",
                    "schema": schema_name,
                    "table": table_name,
                    "name": trigger_name,
                    "disabled": bool(is_disabled),
                    "instead_of": bool(is_instead_of),
                })
                if is_disabled:
                    trigger_item.setForeground(0, QColor("#D93025"))
                    trigger_item.setToolTip(0, "Trigger is disabled")
                else:
                    trigger_item.setToolTip(0, "INSTEAD OF trigger" if is_instead_of else "AFTER trigger")
                table_nodes[table_key].addChild(trigger_item)
            self.triggers_tree.expandAll()
            self.triggers_tree.resizeColumnToContents(0)
            self.statusBar().showMessage(
                f"Loaded {len(rows):,} trigger(s) on {len(table_nodes):,} table(s) from {database_name}."
            )
            if not rows:
                self.trigger_sql_text.setPlainText("No table triggers found in this database.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Trigger query failed", "Could not retrieve table triggers.\n\n" + str(exc))
            self.statusBar().showMessage("Trigger query failed")
        finally:
            QApplication.restoreOverrideCursor()

    def _on_trigger_tree_selection_changed(self, current, previous):
        """Display the selected trigger's SQL definition in the text box."""
        if current is None:
            self.trigger_sql_text.clear()
            return
        data = current.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") != "trigger":
            self.trigger_sql_text.clear()
            return
        definition = data.get("definition") or ""
        if definition:
            status = "DISABLED" if data.get("disabled") else ("INSTEAD OF" if data.get("instead_of") else "AFTER")
            self.trigger_sql_text.setPlainText(
                f"-- Trigger: [{data.get('schema')}].[{data.get('name')}]\n"
                f"-- Table: [{data.get('schema')}].[{data.get('table')}]\n"
                f"-- Type: {status}\n\n{definition}"
            )
        else:
            self.trigger_sql_text.setPlainText(
                "The trigger definition is not visible. The SQL Server login may lack VIEW DEFINITION permission, "
                "or the trigger may be encrypted."
            )

    def refresh_tables_size(self):
        """Query table size, row count, creation date and schema for selected database."""
        if not self._ensure_connection():
            return
        database_name = self.tables_database_combo.currentText()
        if not database_name:
            QMessageBox.information(self, "Tables size", "Select a database first.")
            return

        contains_text = self.tables_contains_edit.text().strip()
        min_size_mb = self.tables_min_size_spin.value()
        top_limit = 50 if self.tables_top50_radio.isChecked() else None
        quoted_db = "[" + database_name.replace("]", "]]") + "]"
        self.statusBar().showMessage(f"Loading table sizes from {database_name}...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            connection = self._db.get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(f"USE {quoted_db};")
                query = """
                    SELECT {top_clause}
                        s.name AS [Schema],
                        t.name AS [Table],
                        SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END) AS [Number of rows],
                        t.create_date AS [Created],
                        CAST(SUM(ps.reserved_page_count) * 8.0 / 1024 AS DECIMAL(19, 2)) AS [Size MB]
                    FROM sys.tables AS t
                    INNER JOIN sys.schemas AS s ON s.schema_id = t.schema_id
                    INNER JOIN sys.dm_db_partition_stats AS ps ON ps.object_id = t.object_id
                    WHERE t.is_ms_shipped = 0
                      AND t.name LIKE ?
                    GROUP BY s.name, t.name, t.create_date
                    HAVING (? = 0 OR SUM(ps.reserved_page_count) * 8.0 / 1024 > ?)
                    ORDER BY SUM(ps.reserved_page_count) DESC, s.name, t.name;
                """.format(top_clause="TOP (?)" if top_limit is not None else "")
                params = ([top_limit] if top_limit is not None else []) + [
                    f"%{contains_text}%", min_size_mb, min_size_mb
                ]
                cursor.execute(query, params)
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()

            self._tables_size_rows = [tuple(row) for row in rows]
            self.table_preview_label.setText("Select a table above to preview its first 10 rows.")
            self.table_preview_result.clear()
            self.table_preview_result.setRowCount(0)
            self.table_preview_result.setColumnCount(0)
            self.table_columns_label.setText("Columns and data types")
            self.table_columns_filter.clear()
            self.table_columns_result.clearContents()
            self.table_columns_result.setRowCount(0)
            self._populate_tables_result(columns, rows)
            self._leave_server_health_monitor()
            self._set_view_mode("tables")
            self.table.hide()
            self.space_scroll.hide()
            self.total_gb_label.hide()
            self.total_mb_label.hide()
            self.tables_panel.show()
            self.statusBar().showMessage(
                f"Loaded {len(rows):,} table(s) from {database_name}."
            )
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Tables size query failed",
                                 "Could not retrieve table sizes.\\n\\n" + str(exc))
            self.statusBar().showMessage("Tables size query failed")
        finally:
            QApplication.restoreOverrideCursor()

    def _filter_table_columns(self, text):
        """Show only column names containing the entered filter text."""
        needle = (text or "").strip().casefold()
        for row in range(self.table_columns_result.rowCount()):
            item = self.table_columns_result.item(row, 0)
            column_name = item.text() if item is not None else ""
            self.table_columns_result.setRowHidden(
                row, bool(needle) and needle not in column_name.casefold()
            )

    def _on_table_list_selection_changed(self):
        """Display up to 10 rows from the currently selected table."""
        selected_rows = self.tables_result.selectionModel().selectedRows()
        if not selected_rows:
            return

        row_index = selected_rows[0].row()
        schema_item = self.tables_result.item(row_index, 0)
        table_item = self.tables_result.item(row_index, 1)
        if schema_item is None or table_item is None:
            return

        schema_name = schema_item.text()
        table_name = table_item.text()
        database_name = self.tables_database_combo.currentText()
        if not database_name:
            return

        def quote_identifier(identifier):
            return "[" + identifier.replace("]", "]]" ) + "]"

        full_table_name = (
            f"{quote_identifier(database_name)}.{quote_identifier(schema_name)}."
            f"{quote_identifier(table_name)}"
        )
        self.table_preview_label.setText(
            f"First 10 rows: {schema_name}.{table_name}"
        )
        self.table_preview_result.clear()
        self.table_preview_result.setRowCount(0)
        self.table_preview_result.setColumnCount(0)
        self.table_columns_filter.clear()
        self.table_columns_result.clearContents()
        self.table_columns_result.setRowCount(0)
        self.table_columns_label.setText(f"Columns and data types: {schema_name}.{table_name}")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cursor = self._db.get_connection().cursor()
            try:
                quoted_db = quote_identifier(database_name)
                cursor.execute(f"USE {quoted_db};")
                cursor.execute("""
                    SELECT
                        c.name AS ColumnName,
                        CASE
                            WHEN ty.name IN ('varchar', 'char', 'varbinary', 'binary')
                                THEN ty.name + '(' + CASE WHEN c.max_length = -1 THEN 'MAX' ELSE CAST(c.max_length AS varchar(10)) END + ')'
                            WHEN ty.name IN ('nvarchar', 'nchar')
                                THEN ty.name + '(' + CASE WHEN c.max_length = -1 THEN 'MAX' ELSE CAST(c.max_length / 2 AS varchar(10)) END + ')'
                            WHEN ty.name IN ('decimal', 'numeric')
                                THEN ty.name + '(' + CAST(c.precision AS varchar(10)) + ',' + CAST(c.scale AS varchar(10)) + ')'
                            WHEN ty.name IN ('datetime2', 'datetimeoffset', 'time')
                                THEN ty.name + '(' + CAST(c.scale AS varchar(10)) + ')'
                            ELSE ty.name
                        END AS DataType
                    FROM sys.columns AS c
                    INNER JOIN sys.types AS ty ON c.user_type_id = ty.user_type_id
                    WHERE c.object_id = OBJECT_ID(?)
                    ORDER BY c.column_id;
                """, (f"{quote_identifier(schema_name)}.{quote_identifier(table_name)}",))
                column_info = cursor.fetchall()
                cursor.execute(f"SELECT TOP (10) * FROM {full_table_name};")
                preview_columns = [column[0] for column in cursor.description]
                rows = cursor.fetchmany(10)
            finally:
                cursor.close()

            self.table_columns_result.setRowCount(len(column_info))
            self.table_columns_result.setHorizontalHeaderLabels(["Column", "Data type"])
            for column_row, info in enumerate(column_info):
                self.table_columns_result.setItem(column_row, 0, QTableWidgetItem(str(info[0])))
                self.table_columns_result.setItem(column_row, 1, QTableWidgetItem(str(info[1])))
            self.table_columns_result.resizeColumnsToContents()
            self.table_columns_result.horizontalHeader().setStretchLastSection(True)

            self.table_preview_result.setColumnCount(len(preview_columns))
            self.table_preview_result.setRowCount(len(rows))
            self.table_preview_result.setHorizontalHeaderLabels(preview_columns)
            for row_number, row in enumerate(rows):
                for column_number, value in enumerate(row):
                    if value is None:
                        text = "NULL"
                    elif isinstance(value, (bytes, bytearray, memoryview)):
                        text = f"<binary data: {len(value)} bytes>"
                    else:
                        text = str(value)
                    self.table_preview_result.setItem(
                        row_number, column_number, QTableWidgetItem(text)
                    )
            self.table_preview_result.resizeColumnsToContents()
            self.statusBar().showMessage(
                f"Previewing {len(rows)} row(s) from {schema_name}.{table_name}."
            )
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            self.table_preview_label.setText(
                f"Could not preview {schema_name}.{table_name}."
            )
            self.table_preview_result.setColumnCount(1)
            self.table_preview_result.setRowCount(1)
            self.table_preview_result.setHorizontalHeaderLabels(["Error"])
            self.table_preview_result.setItem(0, 0, QTableWidgetItem(str(exc)))
            self.statusBar().showMessage("Could not load table preview.")
        finally:
            QApplication.restoreOverrideCursor()

    def _populate_tables_result(self, columns, rows):
        self.tables_result.setSortingEnabled(False)
        self.tables_result.clear()
        self.tables_result.setColumnCount(len(columns))
        self.tables_result.setRowCount(len(rows))
        self.tables_result.setHorizontalHeaderLabels(columns)
        total_mb = 0.0
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                if value is None:
                    text = ""
                elif column_index == 3 and hasattr(value, "strftime"):
                    text = value.strftime("%Y-%m-%d %H:%M:%S")
                elif column_index in (2, 4) and isinstance(value, (int, float, Decimal)):
                    text = f"{value:,.2f}" if column_index == 4 else f"{int(value):,}"
                else:
                    text = str(value)
                item = SortableTableWidgetItem(text)
                if isinstance(value, (int, float, Decimal)):
                    item.setData(Qt.ItemDataRole.UserRole, float(value))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.tables_result.setItem(row_index, column_index, item)
            try:
                total_mb += float(row[4] or 0.0)
            except (ValueError, TypeError, IndexError):
                pass
        self.tables_result.setSortingEnabled(True)
        self.tables_result.resizeColumnsToContents()
        # Keep the numeric size column compact instead of letting it consume excess width.
        if len(columns) >= 5 and columns[4] == "Size MB":
            self.tables_result.setColumnWidth(4, 90)
        self.tables_result.horizontalHeader().setStretchLastSection(False)
        self.tables_total_mb_label.setText(f"Total size: {total_mb:,.2f} MB")

    def _render_space_usage(self, databases):
        # Clear previous cards.
        while self.space_layout.count():
            item = self.space_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        legend = QLabel(
            '<b>Data files:</b> '
            '<span style="color:#2878B5">■ Data</span> &nbsp; '
            '<span style="color:#168A72">■ Indexes</span> &nbsp; '
            '<span style="color:#D89B28">■ Unused</span> &nbsp; '
            '<span style="color:#B8BEC7">■ Unallocated</span> '
            '&nbsp;&nbsp;&nbsp; <b>Transaction log:</b> '
            '<span style="color:#7256A8">■ Used</span> &nbsp; '
            '<span style="color:#B8BEC7">■ Free</span>'
        )
        legend.setWordWrap(True)
        self.space_layout.addWidget(legend)

        if not databases:
            self.space_layout.addWidget(QLabel("No accessible online databases were found."))
            self._show_space_view()
            return

        for db in databases:
            card = QFrame(self.space_content)
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setStyleSheet("QFrame { border: 1px solid #D5D9DE; border-radius: 6px; } QLabel { border: none; }")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 10)
            card_layout.setSpacing(5)

            title = QLabel(f"<b>{self._html_escape(db['name'])}</b>")
            title.setStyleSheet("font-size: 14px; padding-bottom: 2px;")
            card_layout.addWidget(title)

            data_caption = QLabel(
                f"Data Files — {db['data_total']:,.2f} MB total "
                f"({db['data_total'] / 1024.0:,.3f} GB)"
            )
            card_layout.addWidget(data_caption)
            data_bar = SegmentedBar([
                ("Data", db["data"], "#2878B5"),
                ("Indexes", db["indexes"], "#168A72"),
                ("Unused", db["unused"], "#D89B28"),
                ("Unallocated", db["unallocated"], "#B8BEC7"),
            ], card)
            card_layout.addWidget(data_bar)

            log_caption = QLabel(
                f"Transaction Log — {db['log_total']:,.2f} MB total "
                f"({db['log_total'] / 1024.0:,.3f} GB); "
                f"used {db['log_used']:,.2f} MB ({(db['log_used'] / db['log_total'] * 100.0) if db['log_total'] else 0.0:.1f}%)"
            )
            card_layout.addWidget(log_caption)
            log_bar = SegmentedBar([
                ("Used", db["log_used"], "#7256A8"),
                ("Free", db["log_free"], "#B8BEC7"),
            ], card)
            card_layout.addWidget(log_bar)
            self.space_layout.addWidget(card)

        self.space_layout.addStretch(1)
        self._show_space_view()

    @staticmethod
    def _html_escape(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")

    def _populate_table(self, columns, rows):
        self.table.setSortingEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(columns)

        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                if value is None:
                    text = ""
                elif column_index in (3, 4) and isinstance(value, (int, float, Decimal)):
                    text = f"{value:,.2f}" if column_index == 3 else f"{value:,.3f}"
                else:
                    text = str(value)

                item = QTableWidgetItem(text)
                if isinstance(value, (int, float, Decimal)):
                    item.setData(Qt.ItemDataRole.UserRole, float(value))
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                else:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(row_index, column_index, item)

        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        # Avoid the full physical paths taking over the entire window.
        if len(columns) >= 8:
            self.table.setColumnWidth(7, 330)
        self._update_totals()

    def _update_totals(self):
        """Calculate totals from the numeric SizeMB values in the current table."""
        size_mb_column = 3  # SizeMB in DATABASE_FILES_QUERY
        total_mb = 0.0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, size_mb_column)
            if item is not None:
                numeric_value = item.data(Qt.ItemDataRole.UserRole)
                if numeric_value is not None:
                    total_mb += float(numeric_value)
                else:
                    try:
                        total_mb += float(item.text().replace(",", ""))
                    except ValueError:
                        pass
        total_gb = total_mb / 1024.0
        self.total_gb_label.setText(f"Total GB: {total_gb:,.3f}")
        self.total_mb_label.setText(f"Total MB: {total_mb:,.2f}")

    def show_jobs_timeline(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "cpu_panel"):
            self.cpu_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Show SQL Server Agent job runs as bars over the selected time window."""
        if not self._ensure_connection():
            return
        self._leave_server_health_monitor()
        self._set_view_mode("timeline")
        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
            self.jobs_panel, self.steps_timeline_panel, self.processes_panel, self.cpu_panel,
        ):
            widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.timeline_panel.show()
        self.refresh_jobs_timeline()

    def _load_timeline_job_checklist(self, job_names):
        """Populate the timeline job filter, preserving existing selections."""
        previous = {}
        for index in range(self.timeline_job_list.count()):
            item = self.timeline_job_list.item(index)
            previous[item.text()] = item.checkState() == Qt.CheckState.Checked

        names = sorted(set(str(name) for name in job_names), key=str.casefold)
        self._timeline_updating_job_list = True
        try:
            self.timeline_job_list.clear()
            for name in names:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # New jobs are checked by default; keep the user's choices on refresh.
                checked = previous.get(name, True)
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self.timeline_job_list.addItem(item)
        finally:
            self._timeline_updating_job_list = False

    def _set_timeline_job_checks(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.timeline_job_list.blockSignals(True)
        try:
            for index in range(self.timeline_job_list.count()):
                self.timeline_job_list.item(index).setCheckState(state)
        finally:
            self.timeline_job_list.blockSignals(False)
        self._apply_timeline_job_filter()

    def _on_timeline_job_item_changed(self, item):
        if not self._timeline_updating_job_list:
            self._apply_timeline_job_filter()

    def _apply_timeline_job_filter(self):
        selected = {
            self.timeline_job_list.item(index).text()
            for index in range(self.timeline_job_list.count())
            if self.timeline_job_list.item(index).checkState() == Qt.CheckState.Checked
        }
        filtered_runs = [run for run in self._timeline_all_runs if run[0] in selected]
        if self.timeline_chart.window_start is not None and self.timeline_chart.window_end is not None:
            self.timeline_chart.set_data(
                filtered_runs, self.timeline_chart.window_start, self.timeline_chart.window_end
            )
        hours = self.timeline_hours_spin.value()
        self.timeline_status_label.setText(
            f"Last {hours} h — showing {len(filtered_runs)} of {len(self._timeline_all_runs)} job run(s). "
            "Green = succeeded, red = failed, yellow = cancelled. "
            "Use the checkboxes on the left to filter jobs."
        )
        self.statusBar().showMessage(
            f"Showing {len(filtered_runs)} of {len(self._timeline_all_runs)} job run(s)"
        )

    def refresh_jobs_timeline(self):
        """Load job history and update the timeline plus its job checklist."""
        if not self._ensure_connection():
            return
        from datetime import datetime, timedelta
        hours = self.timeline_hours_spin.value()
        query = r"""
WITH JobRuns AS
(
    SELECT j.name AS JobName,
           msdb.dbo.agent_datetime(h.run_date, h.run_time) AS StartTime,
           (h.run_duration / 10000) * 3600
             + ((h.run_duration % 10000) / 100) * 60
             + (h.run_duration % 100) AS DurationSeconds,
           h.run_status AS RunStatus
    FROM msdb.dbo.sysjobhistory AS h
    INNER JOIN msdb.dbo.sysjobs AS j ON j.job_id = h.job_id
    WHERE h.step_id = 0 AND h.run_date > 0 AND h.run_status IN (0, 1, 3)
),
RunsWithTimes AS
(
    SELECT JobName, StartTime,
           DATEADD(SECOND, DurationSeconds, StartTime) AS EndTime, RunStatus
    FROM JobRuns
)
SELECT JobName, StartTime, EndTime, RunStatus
FROM RunsWithTimes
WHERE EndTime >= DATEADD(HOUR, -?, GETDATE())
  AND StartTime <= GETDATE()
ORDER BY StartTime ASC, EndTime ASC, JobName ASC;
"""
        jobs_query = "SELECT name FROM msdb.dbo.sysjobs ORDER BY name;"
        try:
            connection = self._db.get_connection()
            cursor = connection.cursor()
            try:
                # SQL Agent history timestamps are in the SQL Server's local time.
                # Align them with the workstation's local time before drawing the chart.
                cursor.execute("SELECT GETDATE()")
                server_now = cursor.fetchone()[0]
                local_now = datetime.now()
                time_delta = local_now - server_now if server_now is not None else timedelta(0)
                cursor.execute(query, hours)
                rows = cursor.fetchall()
                cursor.execute(jobs_query)
                job_rows = cursor.fetchall()
            finally:
                cursor.close()
            now = local_now
            window_start = now - timedelta(hours=hours)
        except pyodbc.Error as exc:
            QMessageBox.critical(
                self, "Jobs Timeline",
                "Could not load SQL Server Agent job history. Ensure SQL Server Agent history is available "
                "and your login has permission to view msdb job history.\n\n" + str(exc),
            )
            return
        self._timeline_all_runs = [
            (str(name), start + time_delta, end + time_delta, int(status))
            for name, start, end, status in rows if start is not None and end is not None
        ]
        self._load_timeline_job_checklist([row[0] for row in job_rows])
        self.timeline_chart.set_data(self._timeline_all_runs, window_start, now)
        self._apply_timeline_job_filter()

    def show_steps_timeline(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "cpu_panel"):
            self.cpu_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Show each job run followed by its individual step execution bars."""
        if not self._ensure_connection():
            return
        self._leave_server_health_monitor()
        self._set_view_mode("steps_timeline")
        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
            self.jobs_panel, self.timeline_panel,
        ):
            widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.steps_timeline_panel.show()
        self.refresh_steps_timeline()

    def refresh_steps_timeline(self):
        """Load job outcome and step history, matching steps to their parent run."""
        if not self._ensure_connection():
            return
        from datetime import datetime, timedelta
        hours = self.steps_timeline_hours_spin.value()
        query = r"""
WITH JobRuns AS
(
    SELECT h.job_id, j.name AS JobName,
           msdb.dbo.agent_datetime(h.run_date, h.run_time) AS StartTime,
           (h.run_duration / 10000) * 3600
             + ((h.run_duration % 10000) / 100) * 60
             + (h.run_duration % 100) AS DurationSeconds,
           h.run_status AS RunStatus, h.instance_id AS OutcomeInstanceId
    FROM msdb.dbo.sysjobhistory AS h
    INNER JOIN msdb.dbo.sysjobs AS j ON j.job_id = h.job_id
    WHERE h.step_id = 0 AND h.run_date > 0 AND h.run_status IN (0, 1, 3)
), RunsWithTimes AS
(
    SELECT job_id, JobName, StartTime,
           DATEADD(SECOND, DurationSeconds, StartTime) AS EndTime,
           RunStatus, OutcomeInstanceId
    FROM JobRuns
), Steps AS
(
    SELECT h.job_id, h.instance_id, h.step_id, h.step_name AS StepName,
           msdb.dbo.agent_datetime(h.run_date, h.run_time) AS StartTime,
           DATEADD(SECOND,
             (h.run_duration / 10000) * 3600
             + ((h.run_duration % 10000) / 100) * 60
             + (h.run_duration % 100),
             msdb.dbo.agent_datetime(h.run_date, h.run_time)) AS EndTime,
           h.run_status AS RunStatus
    FROM msdb.dbo.sysjobhistory AS h
    WHERE h.step_id > 0 AND h.run_date > 0
)
SELECT r.JobName, r.StartTime AS JobStart, r.EndTime AS JobEnd, r.RunStatus AS JobStatus,
       s.step_id AS StepId, s.StepName, s.StartTime AS StepStart, s.EndTime AS StepEnd,
       s.RunStatus AS StepStatus
FROM RunsWithTimes AS r
LEFT JOIN Steps AS s
  ON s.job_id = r.job_id
 AND s.instance_id < r.OutcomeInstanceId
 AND s.StartTime >= r.StartTime
 AND s.EndTime <= DATEADD(SECOND, 2, r.EndTime)
WHERE r.EndTime >= DATEADD(HOUR, -?, GETDATE())
  AND r.StartTime <= GETDATE()
ORDER BY r.StartTime ASC, r.EndTime ASC, r.JobName ASC, s.instance_id ASC;
"""
        jobs_query = "SELECT name FROM msdb.dbo.sysjobs ORDER BY name;"
        try:
            cursor = self._db.get_connection().cursor()
            try:
                # SQL Agent history timestamps are in the SQL Server's local time.
                # Align them with the workstation's local time before drawing the chart.
                cursor.execute("SELECT GETDATE()")
                server_now = cursor.fetchone()[0]
                local_now = datetime.now()
                time_delta = local_now - server_now if server_now is not None else timedelta(0)
                cursor.execute(query, hours)
                rows = cursor.fetchall()
                cursor.execute(jobs_query)
                job_name_rows = cursor.fetchall()
            finally:
                cursor.close()
            now = local_now
            window_start = now - timedelta(hours=hours)
        except pyodbc.Error as exc:
            QMessageBox.critical(
                self, "Steps Timeline",
                "Could not load SQL Server Agent job/step history. Ensure SQL Server Agent history is available "
                "and your login has permission to view msdb job history.\n\n" + str(exc),
            )
            return
        jobs_by_key = {}
        for row in rows:
            name, job_start, job_end, job_status, step_id, step_name, step_start, step_end, step_status = row
            if job_start is None or job_end is None:
                continue
            # Shift server-local timestamps to the workstation's local clock consistently.
            job_start = job_start + time_delta
            job_end = job_end + time_delta
            if step_start is not None:
                step_start = step_start + time_delta
            if step_end is not None:
                step_end = step_end + time_delta
            # A job run's timestamps make a stable grouping key for the rows returned by the join.
            key = (str(name), job_start, job_end)
            if key not in jobs_by_key:
                jobs_by_key[key] = {"name": str(name), "start": job_start, "end": job_end,
                                    "status": int(job_status), "steps": []}
            if step_id is not None and step_start is not None and step_end is not None:
                step = {"step_id": int(step_id), "name": str(step_name), "start": step_start,
                        "end": step_end, "status": int(step_status)}
                if not any(existing["step_id"] == step["step_id"] and existing["start"] == step["start"]
                           and existing["end"] == step["end"] for existing in jobs_by_key[key]["steps"]):
                    jobs_by_key[key]["steps"].append(step)
        jobs = sorted(jobs_by_key.values(), key=lambda item: (item["start"], item["end"], item["name"]))
        for job in jobs:
            job["steps"].sort(key=lambda item: (item["start"], item["step_id"]))
        self._steps_timeline_all_jobs = jobs
        self._load_steps_timeline_job_checklist([row[0] for row in job_name_rows])
        filtered_jobs = self._get_checked_steps_timeline_jobs()
        self.steps_timeline_chart.set_data(filtered_jobs, window_start, now)
        total_steps = sum(len(job["steps"]) for job in filtered_jobs)
        self.steps_timeline_status_label.setText(
            f"Last {hours} h — showing {len(filtered_jobs)} of {len(jobs)} job run(s), {total_steps} recorded step(s). "
            "Use the checkboxes on the left to filter jobs."
        )
        self.statusBar().showMessage(f"Showing {len(filtered_jobs)} of {len(jobs)} job run(s) and {total_steps} step(s) for the last {hours} hours")

    def _load_steps_timeline_job_checklist(self, job_names):
        """Populate the Steps Timeline job filter, preserving existing selections."""
        previous = {}
        for index in range(self.steps_timeline_job_list.count()):
            item = self.steps_timeline_job_list.item(index)
            previous[item.text()] = item.checkState() == Qt.CheckState.Checked
        names = sorted(set(str(name) for name in job_names), key=str.casefold)
        self._steps_timeline_updating_job_list = True
        try:
            self.steps_timeline_job_list.clear()
            for name in names:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if previous.get(name, True) else Qt.CheckState.Unchecked)
                self.steps_timeline_job_list.addItem(item)
        finally:
            self._steps_timeline_updating_job_list = False

    def _get_checked_steps_timeline_jobs(self):
        selected = {
            self.steps_timeline_job_list.item(index).text()
            for index in range(self.steps_timeline_job_list.count())
            if self.steps_timeline_job_list.item(index).checkState() == Qt.CheckState.Checked
        }
        return [job for job in self._steps_timeline_all_jobs if job["name"] in selected]

    def _set_steps_timeline_job_checks(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.steps_timeline_job_list.blockSignals(True)
        try:
            for index in range(self.steps_timeline_job_list.count()):
                self.steps_timeline_job_list.item(index).setCheckState(state)
        finally:
            self.steps_timeline_job_list.blockSignals(False)
        self._apply_steps_timeline_job_filter()

    def _on_steps_timeline_job_item_changed(self, item):
        if not self._steps_timeline_updating_job_list:
            self._apply_steps_timeline_job_filter()

    def _apply_steps_timeline_job_filter(self):
        filtered_jobs = self._get_checked_steps_timeline_jobs()
        if self.steps_timeline_chart.window_start is not None and self.steps_timeline_chart.window_end is not None:
            self.steps_timeline_chart.set_data(
                filtered_jobs, self.steps_timeline_chart.window_start, self.steps_timeline_chart.window_end
            )
        total_steps = sum(len(job["steps"]) for job in filtered_jobs)
        self.steps_timeline_status_label.setText(
            f"Last {self.steps_timeline_hours_spin.value()} h — showing {len(filtered_jobs)} of "
            f"{len(self._steps_timeline_all_jobs)} job run(s), {total_steps} recorded step(s). "
            "Use the checkboxes on the left to filter jobs."
        )
        self.statusBar().showMessage(
            f"Showing {len(filtered_jobs)} job run(s) and {total_steps} step(s) in Steps Timeline"
        )


    def show_cpu_usage(self):
        """Display CPU utilization and top CPU-consuming cached query plans."""
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if not self._ensure_connection():
            return
        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
            self.jobs_panel, self.timeline_panel, self.steps_timeline_panel,
            self.processes_panel, self.cpu_panel,
        ):
            widget.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self._leave_server_health_monitor()
        self._set_view_mode("cpu_usage")
        self.cpu_panel.show()
        self.refresh_cpu_usage()

    def refresh_cpu_usage(self):
        """Refresh CPU counters, cached query statistics, and collected chart samples."""
        if not self._ensure_connection():
            return
        self.cpu_refresh_button.setEnabled(False)
        self.cpu_last_updated_label.setText("Refreshing...")
        try:
            connection = self._db.get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute(r"""
SELECT TOP (360)
    DATEADD(ms, -1 * (si.ms_ticks - rb.[timestamp]), GETDATE()) AS SampleTime,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS SqlCpu,
    rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS IdleCpu,
    100
      - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int')
      - rb.record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS OtherCpu
FROM
(
    SELECT [timestamp], CONVERT(xml, record) AS record
    FROM sys.dm_os_ring_buffers
    WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR'
      AND record LIKE '%<SystemHealth>%'
) AS rb
CROSS JOIN sys.dm_os_sys_info AS si
ORDER BY rb.[timestamp] DESC;
""")
                cpu_samples = cursor.fetchall()
                if not cpu_samples or cpu_samples[0][1] is None:
                    raise RuntimeError("SQL Server has not provided a CPU utilization sample yet.")
                latest_sample = cpu_samples[0]
                sql_cpu, idle_cpu, other_cpu = [max(0, min(100, int(v or 0))) for v in
                                                (latest_sample[1], latest_sample[2], latest_sample[3])]
                now = datetime.now()
                self.cpu_sql_label.setText(f"SQL Server CPU: {sql_cpu}%")
                self.cpu_other_label.setText(f"Other Processes: {other_cpu}%")
                self.cpu_idle_label.setText(f"CPU Idle: {idle_cpu}%")
                self.cpu_last_updated_label.setText(f"Last updated: {now:%Y-%m-%d %H:%M:%S}")
                self._cpu_history = [
                    (sample[0], int(sample[1] or 0), int(sample[3] or 0), int(sample[2] or 0))
                    for sample in reversed(cpu_samples) if sample[1] is not None
                ][-360:]
                self.cpu_chart.set_points(self._cpu_history)

                cursor.execute(r"""
SELECT TOP (20)
    DB_NAME(st.dbid) AS DatabaseName,
    qs.total_worker_time / 1000.0 AS TotalCpuMs,
    (qs.total_worker_time / 1000.0) / NULLIF(qs.execution_count, 0) AS AverageCpuMs,
    qs.execution_count AS ExecutionCount,
    qs.last_execution_time AS LastExecution,
    SUBSTRING(
        st.text,
        (qs.statement_start_offset / 2) + 1,
        (
            (CASE qs.statement_end_offset
                WHEN -1 THEN DATALENGTH(st.text)
                ELSE qs.statement_end_offset
             END - qs.statement_start_offset) / 2
        ) + 1
    ) AS SqlStatement
FROM sys.dm_exec_query_stats AS qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
WHERE qs.total_worker_time > 0
ORDER BY qs.total_worker_time DESC;
""")
                query_rows = cursor.fetchall()
            finally:
                cursor.close()

            self.cpu_queries_table.setUpdatesEnabled(False)
            self.cpu_queries_table.setSortingEnabled(False)
            try:
                self.cpu_queries_table.setRowCount(len(query_rows))
                for row_index, row in enumerate(query_rows):
                    database, total_cpu, avg_cpu, executions, last_exec, statement = row
                    values = [
                        str(row_index + 1),
                        str(database or ""),
                        f"{float(total_cpu or 0):,.0f}",
                        f"{float(avg_cpu or 0):,.2f}",
                        f"{int(executions or 0):,}",
                        str(last_exec or ""),
                        str(statement or "").strip(),
                    ]
                    sort_values = [
                        row_index + 1,
                        str(database or "").casefold(),
                        float(total_cpu or 0),
                        float(avg_cpu or 0),
                        int(executions or 0),
                        last_exec,
                        str(statement or "").strip().casefold(),
                    ]
                    for col_index, value in enumerate(values):
                        item = SortableTableWidgetItem(value)
                        item.setData(Qt.ItemDataRole.UserRole, sort_values[col_index])
                        item.setToolTip(value)
                        if col_index in (0, 2, 3, 4):
                            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        self.cpu_queries_table.setItem(row_index, col_index, item)
                self.cpu_queries_table.resizeColumnsToContents()
                self.cpu_queries_table.setColumnWidth(6, 520)
            finally:
                self.cpu_queries_table.setSortingEnabled(True)
                self.cpu_queries_table.setUpdatesEnabled(True)
            self.statusBar().showMessage("CPU usage refreshed.")
        except (pyodbc.Error, ValueError, RuntimeError, ConnectionError) as exc:
            QMessageBox.critical(
                self,
                "CPU Usage",
                "Could not retrieve CPU utilization or query statistics. Check the SQL Server permissions "
                "VIEW SERVER STATE (or the equivalent permission for your SQL Server version).\\n\\n"
                + str(exc),
            )
            self.cpu_last_updated_label.setText("Refresh failed")
        finally:
            self.cpu_refresh_button.setEnabled(True)

    def show_active_processes(self):
        """Display active SQL Server requests without changing row order on refresh."""
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return

        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
            self.jobs_panel, self.timeline_panel, self.steps_timeline_panel, self.cpu_panel,
        ):
            widget.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self._leave_server_health_monitor()
        self._set_view_mode("active_processes")
        self.processes_panel.show()
        self.refresh_active_processes()

    def refresh_active_processes(self):
        """Refresh active requests while preserving the selected session and scroll position."""
        if not self._ensure_connection():
            return

        selected_session = None
        selected_rows = self.processes_table.selectionModel().selectedRows()
        if selected_rows:
            item = self.processes_table.item(selected_rows[0].row(), 0)
            if item is not None:
                selected_session = item.data(Qt.ItemDataRole.UserRole)
        scrollbar = self.processes_table.verticalScrollBar()
        old_scroll = scrollbar.value()
        connection = self._db.get_connection()
        cursor = connection.cursor()
        sql = r"""
;WITH SessionRequests AS
(
    SELECT
        s.session_id,
        s.login_name,
        DB_NAME(s.database_id) AS database_name,
        s.program_name,
        s.status AS session_status,
        r.status AS request_status,
        r.wait_time,
        r.wait_type,
        r.blocking_session_id,
        s.memory_usage,
        r.sql_handle,
        r.statement_start_offset,
        r.statement_end_offset,
        r.request_id
    FROM sys.dm_exec_sessions AS s
    LEFT JOIN sys.dm_exec_requests AS r
        ON r.session_id = s.session_id
    WHERE s.is_user_process = 1
      AND s.session_id <> @@SPID
),
BlockChain AS
(
    SELECT sr.session_id AS original_session_id,
           sr.blocking_session_id AS blocker_session_id,
           0 AS depth
    FROM SessionRequests AS sr
    WHERE sr.blocking_session_id > 0
    UNION ALL
    SELECT bc.original_session_id,
           parent.blocking_session_id,
           bc.depth + 1
    FROM BlockChain AS bc
    INNER JOIN SessionRequests AS parent
        ON parent.session_id = bc.blocker_session_id
    WHERE parent.blocking_session_id > 0 AND bc.depth < 32
),
HeadBlockers AS
(
    SELECT original_session_id, blocker_session_id,
           ROW_NUMBER() OVER (PARTITION BY original_session_id ORDER BY depth DESC) AS rn
    FROM BlockChain
)
SELECT
    sr.session_id,
    sr.login_name,
    sr.database_name,
    sr.program_name,
    sr.wait_time AS wait_time_ms,
    sr.wait_type,
    NULLIF(sr.blocking_session_id, 0) AS blocked_by,
    COALESCE(hb.blocker_session_id, NULLIF(sr.blocking_session_id, 0)) AS head_blocker,
    CAST(sr.memory_usage * 8.0 / 1024.0 AS decimal(18,2)) AS memory_use_mb,
    txt.text AS sql_text,
    sr.statement_start_offset,
    sr.statement_end_offset,
    sr.request_id
FROM SessionRequests AS sr
LEFT JOIN HeadBlockers AS hb
    ON hb.original_session_id = sr.session_id AND hb.rn = 1
OUTER APPLY sys.dm_exec_sql_text(sr.sql_handle) AS txt
ORDER BY sr.session_id, sr.request_id
OPTION (MAXRECURSION 32);
"""
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
        except pyodbc.Error as exc:
            QMessageBox.critical(
                self, "Active Processes", 
                "Could not read active SQL Server requests. The connected login may need VIEW SERVER STATE permission "
                "(or VIEW SERVER PERFORMANCE STATE on newer SQL Server versions).\n\n" + str(exc),
            )
            return
        finally:
            cursor.close()

        data = []
        for row in rows:
            item = dict(zip(columns, row))
            sql_text = item.get("sql_text") or ""
            start_offset = item.get("statement_start_offset") or 0
            end_offset = item.get("statement_end_offset")
            if start_offset >= 0:
                start_char = start_offset // 2
                end_char = len(sql_text) if end_offset is None or end_offset < 0 else end_offset // 2
                sql_text = sql_text[start_char:end_char]
            item["display_sql"] = sql_text.strip()
            data.append(item)

        self.processes_table.setUpdatesEnabled(False)
        self.processes_table.setSortingEnabled(False)
        self.processes_table.blockSignals(True)
        try:
            self.processes_table.clearContents()
            self.processes_table.setRowCount(len(data))
            for row_index, item in enumerate(data):
                values = [
                    item.get("session_id"), item.get("login_name"), item.get("database_name"),
                    item.get("program_name"), item.get("wait_time_ms"), item.get("wait_type"),
                    item.get("blocked_by"), item.get("head_blocker"), item.get("memory_use_mb"),
                ]
                for col_index, value in enumerate(values):
                    if col_index == 4 and value is not None:
                        display = f"{int(value):,}"
                    elif col_index == 8 and value is not None:
                        display = f"{float(value):,.2f}"
                    else:
                        display = "" if value is None else str(value)
                    cell = SortableTableWidgetItem(display)
                    if col_index in (0, 4, 6, 7, 8):
                        cell.setData(Qt.ItemDataRole.UserRole, value)
                    else:
                        cell.setData(Qt.ItemDataRole.UserRole, display.casefold())
                    self.processes_table.setItem(row_index, col_index, cell)
            if selected_session is not None:
                for row_index in range(self.processes_table.rowCount()):
                    cell = self.processes_table.item(row_index, 0)
                    if cell and cell.data(Qt.ItemDataRole.UserRole) == selected_session:
                        self.processes_table.selectRow(row_index)
                        break
            self.processes_table.verticalScrollBar().setValue(old_scroll)
        finally:
            self.processes_table.blockSignals(False)
            self.processes_table.setSortingEnabled(True)
            self.processes_table.setUpdatesEnabled(True)
        self.processes_count_label.setText(f"{len(data):,} active process(es)")
        self.statusBar().showMessage(f"Loaded {len(data):,} active SQL Server request(s).")
        self._show_selected_process_query()

    def _show_selected_process_query(self):
        """Fetch the current SQL text for the selected session from the refreshed row data."""
        selected_rows = self.processes_table.selectionModel().selectedRows()
        if not selected_rows:
            self.processes_query.clear()
            return
        row_index = selected_rows[0].row()
        session_cell = self.processes_table.item(row_index, 0)
        if session_cell is None:
            self.processes_query.clear()
            return
        session_id = session_cell.data(Qt.ItemDataRole.UserRole)
        if session_id is None or not self._ensure_connection():
            self.processes_query.clear()
            return
        connection = self._db.get_connection()
        cursor = connection.cursor()
        try:
            cursor.execute("""
                SELECT r.sql_handle, r.statement_start_offset, r.statement_end_offset,
                       txt.text AS sql_text, r.request_id
                FROM sys.dm_exec_requests AS r
                OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) AS txt
                WHERE r.session_id = ?
            """, (session_id,))
            row = cursor.fetchone()
            if row is None:
                self.processes_query.setPlainText("The request has completed or is no longer visible. Refresh the list.")
                return
            sql_text = row.sql_text or ""
            start_offset = row.statement_start_offset or 0
            end_offset = row.statement_end_offset
            start_char = max(0, start_offset // 2)
            end_char = len(sql_text) if end_offset is None or end_offset < 0 else end_offset // 2
            self.processes_query.setPlainText(sql_text[start_char:end_char].strip() or sql_text)
        except pyodbc.Error as exc:
            self.processes_query.setPlainText("Could not retrieve SQL text:\n" + str(exc))
        finally:
            cursor.close()

    def show_jobs_list(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "cpu_panel"):
            self.cpu_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Open the SQL Server Agent jobs view and load jobs/schedules."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        self._leave_server_health_monitor()
        self._set_view_mode("jobs")
        for widget in (
            self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
            self.indexes_panel, self.programmability_panel, self.server_info_panel,
        ):
            widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.jobs_panel.show()
        self.job_history_controls.hide()
        self.job_history_list_panel.hide()
        self.job_history_content_splitter.setSizes([0, 1270])
        for widget in (self.jobs_refresh_button, self.jobs_step_search_edit,
                       self.jobs_step_search_button, self.jobs_step_search_clear_button):
            widget.show()
        self.jobs_bottom_splitter.show()
        self.jobs_steps_panel.show()
        self.jobs_details_title.setText("Job / Schedule details")
        self.refresh_jobs_list()

    def show_failed_jobs_last_24h(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "cpu_panel"):
            self.cpu_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Show SQL Server Agent jobs with a failed outcome in the last 24 hours."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        self._leave_server_health_monitor()
        self._set_view_mode("failed_jobs")
        for widget in (self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
                       self.indexes_panel, self.programmability_panel, self.server_info_panel):
            widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.jobs_panel.show()
        for widget in (self.jobs_refresh_button, self.jobs_step_search_edit,
                       self.jobs_step_search_button, self.jobs_step_search_clear_button):
            widget.hide()
        self.job_history_controls.show()
        self.job_history_list_panel.show()
        self.job_history_content_splitter.setSizes([220, 1050])
        self.jobs_bottom_splitter.show()
        self.jobs_steps_panel.hide()
        self.jobs_details_title.setText("Full error message — select a failed job above")
        self.jobs_splitter.setStretchFactor(0, 2)
        self.jobs_splitter.setStretchFactor(1, 3)

        query = r"""
WITH FailedRuns AS
(
    SELECT
        j.name AS [Job Name],
        msdb.dbo.agent_datetime(h.run_date, h.run_time) AS [End Time],
        (h.run_duration / 10000) * 3600
            + ((h.run_duration % 10000) / 100) * 60
            + (h.run_duration % 100) AS DurationSeconds,
        h.run_duration,
        h.message AS [Error]
    FROM msdb.dbo.sysjobhistory AS h
    INNER JOIN msdb.dbo.sysjobs AS j ON j.job_id = h.job_id
    WHERE h.step_id = 0
      AND h.run_status = 0
      AND h.run_date > 0
)
SELECT
    [Job Name],
    DATEADD(SECOND, -DurationSeconds, [End Time]) AS [Start Time],
    [End Time],
    CAST(DurationSeconds / 3600 AS varchar(10)) + ':'
      + RIGHT('00' + CAST((DurationSeconds % 3600) / 60 AS varchar(2)), 2) + ':'
      + RIGHT('00' + CAST(DurationSeconds % 60 AS varchar(2)), 2) AS [Duration],
    [Error]
FROM FailedRuns
WHERE [End Time] >= DATEADD(HOUR, -?, GETDATE())
  AND [End Time] <= GETDATE()
ORDER BY [End Time] DESC;
"""
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(query, self.job_history_hours_spin.value())
                headers = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except pyodbc.Error as exc:
            QMessageBox.critical(
                self, "Failed jobs last 24h",
                "Could not load failed SQL Server Agent job history. Ensure SQL Server Agent history is available "
                "and your login has permission to view msdb job history.\n\n" + str(exc),
            )
            return

        self._job_history_headers = headers
        self._job_history_all_rows = list(rows)
        self._load_job_history_checklist()
        self._render_job_history_rows()

    def show_completed_jobs_last_24h(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "cpu_panel"):
            self.cpu_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Show SQL Server Agent jobs whose overall execution succeeded in the last 24 hours."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        self._leave_server_health_monitor()
        self._set_view_mode("completed_jobs")
        for widget in (self.table, self.space_scroll, self.tables_panel, self.triggers_panel,
                       self.indexes_panel, self.programmability_panel, self.server_info_panel):
            widget.hide()
        self.total_gb_label.hide()
        self.total_mb_label.hide()
        self.jobs_panel.show()
        for widget in (self.jobs_refresh_button, self.jobs_step_search_edit,
                       self.jobs_step_search_button, self.jobs_step_search_clear_button):
            widget.hide()
        self.job_history_controls.show()
        self.job_history_list_panel.show()
        self.job_history_content_splitter.setSizes([220, 1050])
        self.jobs_bottom_splitter.show()
        self.jobs_steps_panel.hide()
        self.jobs_details_title.setText("Completed job execution")
        self.jobs_splitter.setStretchFactor(0, 3)
        self.jobs_splitter.setStretchFactor(1, 1)

        query = r"""
WITH CompletedRuns AS
(
    SELECT
        j.name AS [Job Name],
        msdb.dbo.agent_datetime(h.run_date, h.run_time) AS [End Time],
        (h.run_duration / 10000) * 3600
            + ((h.run_duration % 10000) / 100) * 60
            + (h.run_duration % 100) AS DurationSeconds
    FROM msdb.dbo.sysjobhistory AS h
    INNER JOIN msdb.dbo.sysjobs AS j ON j.job_id = h.job_id
    WHERE h.step_id = 0
      AND h.run_status = 1
      AND h.run_date > 0
)
SELECT
    [Job Name],
    DATEADD(SECOND, -DurationSeconds, [End Time]) AS [Start Time],
    [End Time],
    CAST(DurationSeconds / 3600 AS varchar(10)) + ':'
      + RIGHT('00' + CAST((DurationSeconds % 3600) / 60 AS varchar(2)), 2) + ':'
      + RIGHT('00' + CAST(DurationSeconds % 60 AS varchar(2)), 2) AS [Duration]
FROM CompletedRuns
WHERE [End Time] >= DATEADD(HOUR, -?, GETDATE())
  AND [End Time] <= GETDATE()
ORDER BY [End Time] DESC;
"""
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(query, self.job_history_hours_spin.value())
                headers = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except pyodbc.Error as exc:
            QMessageBox.critical(
                self, "Completed jobs last 24h",
                "Could not load completed SQL Server Agent job history. Ensure SQL Server Agent history is available "
                "and your login has permission to view msdb job history.\n\n" + str(exc),
            )
            return

        self._job_history_headers = headers
        self._job_history_all_rows = list(rows)
        self._load_job_history_checklist()
        self._render_job_history_rows()

    def _refresh_current_job_history(self):
        if self._view_mode == "failed_jobs":
            self.show_failed_jobs_last_24h()
        elif self._view_mode == "completed_jobs":
            self.show_completed_jobs_last_24h()

    def _load_job_history_checklist(self):
        """Load SQL Agent job names and preserve existing checkbox selections."""
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("SELECT name FROM msdb.dbo.sysjobs ORDER BY name;")
                names = [str(row[0]) for row in cursor.fetchall()]
            finally:
                cursor.close()
        except pyodbc.Error as exc:
            self.statusBar().showMessage(f"Could not load job checklist: {exc}")
            names = sorted({str(row[0]) for row in self._job_history_all_rows if row and row[0] is not None})

        old_states = {
            self.job_history_job_list.item(i).text(): self.job_history_job_list.item(i).checkState()
            for i in range(self.job_history_job_list.count())
        }
        self._job_history_updating_list = True
        self.job_history_job_list.blockSignals(True)
        try:
            self.job_history_job_list.clear()
            for name in names:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(old_states.get(name, Qt.CheckState.Checked))
                self.job_history_job_list.addItem(item)
        finally:
            self.job_history_job_list.blockSignals(False)
            self._job_history_updating_list = False

    def _set_job_history_checks(self, checked):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.job_history_job_list.blockSignals(True)
        try:
            for i in range(self.job_history_job_list.count()):
                self.job_history_job_list.item(i).setCheckState(state)
        finally:
            self.job_history_job_list.blockSignals(False)
        self._render_job_history_rows()

    def _on_job_history_item_changed(self, item):
        if not self._job_history_updating_list:
            self._render_job_history_rows()

    def _render_job_history_rows(self):
        selected_names = {
            self.job_history_job_list.item(i).text()
            for i in range(self.job_history_job_list.count())
            if self.job_history_job_list.item(i).checkState() == Qt.CheckState.Checked
        }
        rows = [row for row in self._job_history_all_rows if row and str(row[0]) in selected_names]
        headers = self._job_history_headers
        self.jobs_result.setSortingEnabled(False)
        self.jobs_result.clear()
        self.jobs_result.setColumnCount(len(headers))
        self.jobs_result.setHorizontalHeaderLabels(headers)
        self.jobs_result.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else ("" if value is None else str(value))
                self.jobs_result.setItem(r, c, QTableWidgetItem(text))
        self.jobs_result.setSortingEnabled(True)
        self.jobs_result.resizeColumnsToContents()
        self.jobs_result.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.jobs_result.horizontalHeader().setStretchLastSection(True)
        self.jobs_details_text.clear()
        if self._view_mode == "failed_jobs":
            self.jobs_details_text.setPlaceholderText("Select a failed job above to view the complete error message.")
            self.jobs_details_title.setText("Full error message — select a failed job above")
        else:
            self.jobs_details_text.setPlaceholderText("Select a completed job above to view its execution details.")
            self.jobs_details_title.setText("Completed job execution")
        self.jobs_steps_result.setRowCount(0)
        hours = self.job_history_hours_spin.value()
        self.statusBar().showMessage(
            f"Showing {len(rows)} of {len(self._job_history_all_rows)} {self._view_mode.replace('_', ' ')} run(s) in the last {hours} hour(s)"
        )
        if rows:
            self.jobs_result.selectRow(0)
    def refresh_jobs_list(self):
        """Refresh the jobs/schedules grid without leaving the Jobs list view."""
        if not self._ensure_connection():
            return
        query = r"""
SELECT
    j.name AS [Job Name],
    COALESCE(s.name, '(No schedule)') AS [Schedule Name],
    CASE WHEN j.enabled = 1 THEN 'Active' ELSE 'Inactive' END AS [Job Status],
    CASE
        WHEN s.schedule_id IS NULL THEN 'N/A'
        WHEN s.enabled = 1 THEN 'Active'
        ELSE 'Inactive'
    END AS [Schedule Status],
    CASE
        WHEN j.enabled = 1 AND (s.schedule_id IS NULL OR s.enabled = 1)
            THEN 'Active'
        ELSE 'Inactive'
    END AS [Overall Status],
    CASE s.freq_type
        WHEN 1 THEN 'Once'
        WHEN 4 THEN 'Daily'
        WHEN 8 THEN 'Weekly'
        WHEN 16 THEN 'Monthly'
        WHEN 32 THEN 'Monthly relative'
        WHEN 64 THEN 'When SQL Server Agent starts'
        WHEN 128 THEN 'When computer is idle'
        ELSE CASE WHEN s.schedule_id IS NULL THEN 'N/A' ELSE 'Other' END
    END AS [Frequency],
    CASE
        WHEN js.next_run_date IS NULL OR js.next_run_date = 0 THEN NULL
        ELSE msdb.dbo.agent_datetime(js.next_run_date, js.next_run_time)
    END AS [Next Run]
FROM msdb.dbo.sysjobs AS j
LEFT JOIN msdb.dbo.sysjobschedules AS js ON j.job_id = js.job_id
LEFT JOIN msdb.dbo.sysschedules AS s ON js.schedule_id = s.schedule_id
ORDER BY j.name, s.name;
"""
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(query)
                headers = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
            finally:
                cursor.close()
        except pyodbc.Error as exc:
            QMessageBox.critical(
                self, "Jobs list",
                "Could not load SQL Server Agent jobs. Ensure that SQL Server Agent is available "
                "and your login has permission to view jobs in msdb.\n\n" + str(exc),
            )
            return

        self.jobs_result.setSortingEnabled(False)
        self.jobs_result.clear()
        self.jobs_result.setColumnCount(len(headers))
        self.jobs_result.setHorizontalHeaderLabels(headers)
        self.jobs_result.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else ("" if value is None else str(value))
                self.jobs_result.setItem(r, c, QTableWidgetItem(text))
        self.jobs_result.setSortingEnabled(True)
        self.jobs_result.resizeColumnsToContents()
        self.jobs_result.horizontalHeader().setStretchLastSection(True)
        # Keep the active search applied after a manual refresh.
        if self._jobs_step_search:
            self._apply_job_step_search_filter()
        self.jobs_details_text.clear()
        self.jobs_steps_result.setRowCount(0)
        self.statusBar().showMessage(f"Loaded {len(rows)} SQL Server Agent job schedule row(s)")

    def search_job_steps(self):
        """Find jobs whose step command contains the requested text."""
        if not self._ensure_connection():
            return
        search_text = self.jobs_step_search_edit.text().strip()
        if not search_text:
            self.clear_job_step_search()
            return
        self._jobs_step_search = search_text
        if self.jobs_result.rowCount() == 0:
            self.refresh_jobs_list()
        self._apply_job_step_search_filter()
        self.jobs_result.clearSelection()
        self.jobs_details_text.clear()
        self.jobs_steps_result.setRowCount(0)

    def _apply_job_step_search_filter(self):
        """Show only jobs with at least one matching step command."""
        if not self._db or not self._jobs_step_search:
            for row in range(self.jobs_result.rowCount()):
                self.jobs_result.setRowHidden(row, False)
            return
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(r"""
SELECT DISTINCT j.name
FROM msdb.dbo.sysjobs AS j
INNER JOIN msdb.dbo.sysjobsteps AS st ON st.job_id = j.job_id
WHERE UPPER(st.command) LIKE UPPER(?);
""", "%" + self._jobs_step_search + "%")
                matching_jobs = {str(row[0]) for row in cursor.fetchall()}
            finally:
                cursor.close()
        except pyodbc.Error as exc:
            QMessageBox.critical(self, "Search job steps", "Could not search SQL Server Agent job steps.\n\n" + str(exc))
            return

        visible_rows = 0
        for row in range(self.jobs_result.rowCount()):
            item = self.jobs_result.item(row, 0)
            matched = item is not None and item.text() in matching_jobs
            self.jobs_result.setRowHidden(row, not matched)
            if matched:
                visible_rows += 1
        self.statusBar().showMessage(
            f"Search '{self._jobs_step_search}': {len(matching_jobs)} job(s), "
            f"{visible_rows} job/schedule row(s) matched"
        )

    def clear_job_step_search(self):
        """Clear search and restore the complete jobs list."""
        self._jobs_step_search = ""
        self.jobs_step_search_edit.clear()
        for row in range(self.jobs_result.rowCount()):
            self.jobs_result.setRowHidden(row, False)
        self.jobs_result.clearSelection()
        self.jobs_details_text.clear()
        self.jobs_steps_result.setRowCount(0)
        self.statusBar().showMessage("Job-step search cleared")

    def _on_job_selection_changed(self):
        """Show job details, or the complete error for a failed execution."""
        selected = self.jobs_result.selectedItems()
        if not selected or not self._db:
            self.jobs_details_text.clear()
            self.jobs_steps_result.setRowCount(0)
            return
        row_index = selected[0].row()
        if self._view_mode == "failed_jobs":
            error_item = self.jobs_result.item(row_index, 4)
            self.jobs_details_text.setPlainText(error_item.text() if error_item else "No error message recorded.")
            self.jobs_steps_result.setRowCount(0)
            return
        if self._view_mode == "completed_jobs":
            values = [self.jobs_result.item(row_index, col).text() if self.jobs_result.item(row_index, col) else "" for col in range(self.jobs_result.columnCount())]
            self.jobs_details_text.setPlainText(
                f"Job: {values[0]}\nStart time: {values[1]}\nEnd time: {values[2]}\nDuration: {values[3]}"
            )
            self.jobs_steps_result.setRowCount(0)
            return
        job_item = self.jobs_result.item(row_index, 0)
        schedule_item = self.jobs_result.item(row_index, 1)
        if job_item is None:
            return
        job_name = job_item.text()
        schedule_name = schedule_item.text() if schedule_item else "(No schedule)"
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(r"""
SELECT
    j.date_created,
    j.description AS JobDescription,
    COALESCE(s.name, '(No schedule)') AS ScheduleName,
    s.enabled AS ScheduleEnabled,
    s.freq_type,
    s.freq_interval,
    s.freq_subday_type,
    s.freq_subday_interval,
    s.freq_recurrence_factor,
    s.active_start_date,
    s.active_end_date,
    s.active_start_time,
    s.active_end_time
FROM msdb.dbo.sysjobs AS j
LEFT JOIN msdb.dbo.sysjobschedules AS js ON j.job_id = js.job_id
LEFT JOIN msdb.dbo.sysschedules AS s ON js.schedule_id = s.schedule_id
WHERE j.name = ? AND COALESCE(s.name, '(No schedule)') = ?;
""", job_name, schedule_name)
                detail_rows = cursor.fetchall()
                cursor.execute(r"""
SELECT step_id, step_name, subsystem, command
FROM msdb.dbo.sysjobsteps AS st
INNER JOIN msdb.dbo.sysjobs AS j ON st.job_id = j.job_id
WHERE j.name = ?
ORDER BY step_id;
""", job_name)
                steps = cursor.fetchall()
                if self._jobs_step_search:
                    steps = [step for step in steps if self._jobs_step_search.casefold() in str(step[3] or "").casefold()]
            finally:
                cursor.close()
        except pyodbc.Error as exc:
            self.jobs_details_text.setPlainText("Could not load job details.\n\n" + str(exc))
            self.jobs_steps_result.setRowCount(0)
            return

        if detail_rows:
            detail = detail_rows[0]
            created = detail[0].strftime("%Y-%m-%d %H:%M:%S") if hasattr(detail[0], "strftime") else (str(detail[0]) if detail[0] is not None else "")
            job_description = str(detail[1] or "")
            if detail[2] == "(No schedule)":
                schedule_description = "No schedule assigned"
                schedule_status = "N/A"
            else:
                schedule_status = "Enabled" if detail[3] == 1 else "Disabled"
                freq_type, interval, subday_type, subday_interval, recurrence = detail[4:9]
                start_date, end_date = detail[9], detail[10]
                start_time_value = detail[11] or 0

                def format_agent_time(value):
                    value = int(value or 0)
                    return f"{value // 10000:02d}:{(value // 100) % 100:02d}:{value % 100:02d}"

                if freq_type == 1:
                    frequency_text = f"Once on {start_date}"
                elif freq_type == 4:
                    frequency_text = "Daily" if (interval or 1) == 1 else f"Every {interval} days"
                elif freq_type == 8:
                    days = [(1, "Sunday"), (2, "Monday"), (4, "Tuesday"), (8, "Wednesday"),
                            (16, "Thursday"), (32, "Friday"), (64, "Saturday")]
                    day_names = [name for bit, name in days if (interval or 0) & bit]
                    frequency_text = "Weekly" + (f" on {', '.join(day_names)}" if day_names else "")
                    if recurrence and recurrence > 1:
                        frequency_text += f", every {recurrence} weeks"
                elif freq_type == 16:
                    frequency_text = f"Monthly on day {interval}"
                    if recurrence and recurrence > 1:
                        frequency_text += f", every {recurrence} months"
                elif freq_type == 32:
                    frequency_text = "Monthly relative schedule"
                elif freq_type == 64:
                    frequency_text = "When SQL Server Agent starts"
                elif freq_type == 128:
                    frequency_text = "When computer is idle"
                else:
                    frequency_text = f"Other schedule (frequency type {freq_type})"

                if freq_type not in (64, 128):
                    if subday_type == 2:
                        frequency_text += f", every {subday_interval} seconds"
                    elif subday_type == 4:
                        frequency_text += f", every {subday_interval} minutes"
                    elif subday_type == 8:
                        frequency_text += f", every {subday_interval} hours"
                    else:
                        frequency_text += f" at {format_agent_time(start_time_value)}"
                schedule_description = frequency_text
                if start_date:
                    schedule_description += f"\nActive from: {start_date}"
                if end_date and str(end_date) != "99991231":
                    schedule_description += f"\nActive until: {end_date}"

            self.jobs_details_text.setPlainText(
                f"Job: {job_name}\n"
                f"Created: {created}\n\n"
                f"Description:\n{job_description or '(No description)'}\n\n"
                f"Schedule: {schedule_name}\n"
                f"Schedule status: {schedule_status}\n\n"
                f"Schedule description:\n{schedule_description}"
            )
        else:
            self.jobs_details_text.setPlainText(f"Job: {job_name}\nSchedule: {schedule_name}\nNo details found.")

        self.jobs_steps_result.setSortingEnabled(False)
        self.jobs_steps_result.setRowCount(len(steps))
        for r, step in enumerate(steps):
            for c, value in enumerate(step):
                item = QTableWidgetItem("" if value is None else str(value))
                if c == 3:
                    item.setToolTip("" if value is None else str(value))
                self.jobs_steps_result.setItem(r, c, item)
        self.jobs_steps_result.resizeColumnsToContents()
        self.jobs_steps_result.setColumnWidth(3, max(250, min(self.jobs_steps_result.columnWidth(3), 600)))
        self.jobs_steps_result.setSortingEnabled(False)

    def show_about(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Display application information and the MIT license."""
        dialog = QDialog(self)
        dialog.setWindowTitle("About Simple SQL Server Monitor")
        dialog.resize(650, 620)
        layout = QVBoxLayout(dialog)

        message = QLabel(
            "Simple SQL Server Monitor\n\n"
            "Author: Srdjan Janjin\n"
            "Year: 2026\n"
            "AI-assisted development: ChatGPT\n"
            "Version: 1.0",
            dialog,
        )
        message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(message)

        license_heading = QLabel("MIT License", dialog)
        license_heading.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(license_heading)

        license_text = QTextEdit(dialog)
        license_text.setReadOnly(True)
        license_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        license_text.setPlainText("""MIT License

Copyright (c) 2026 Srdjan Janjin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.""")
        license_text.setMinimumHeight(390)
        layout.addWidget(license_text, 1)

        close_button = QPushButton("OK", dialog)
        close_button.setDefault(True)
        close_button.clicked.connect(dialog.accept)
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)
        dialog.exec()

    def show_view_structure(self):
        """Open the view dependency browser and populate database/view selectors."""
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if not self._ensure_connection():
            return
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("""
                    SELECT name FROM sys.databases
                    WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
                    ORDER BY name;
                """)
                names = [row[0] for row in cursor.fetchall()]
            finally:
                cursor.close()
            previous = self.view_structure_database_combo.currentText()
            self.view_structure_database_combo.blockSignals(True)
            self.view_structure_database_combo.clear()
            self.view_structure_database_combo.addItems(names)
            preferred = previous if previous in names else (
                self._settings.database if self._settings.database in names else
                (names[0] if names else "")
            )
            if preferred:
                self.view_structure_database_combo.setCurrentText(preferred)
            self.view_structure_database_combo.blockSignals(False)
            self._leave_server_health_monitor()
            self._set_view_mode("view_structure")
            self._hide_non_view_structure_panels()
            self.view_structure_panel.show()
            self.table.hide()
            self.total_gb_label.hide()
            self.total_mb_label.hide()
            self.space_scroll.hide()
            self._load_view_structure_names()
        except Exception as exc:
            QMessageBox.critical(self, "View Structure", f"Could not load databases: {exc}")

    def _hide_non_view_structure_panels(self):
        """Hide other custom views when opening View Structure."""
        for name in (
            "programmability_panel", "tables_panel", "triggers_panel",
            "indexes_panel", "jobs_panel", "jobs_steps_panel", "timeline_panel",
            "steps_timeline_panel", "server_info_panel"
        ):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.hide()

    def _load_view_structure_names(self, *_):
        database_name = self.view_structure_database_combo.currentText()
        if not database_name or not self._db:
            return
        quoted_db = "[" + database_name.replace("]", "]]") + "]"
        selected = None
        row = self.view_structure_list.currentRow()
        if row >= 0:
            schema_item = self.view_structure_list.item(row, 0)
            view_item = self.view_structure_list.item(row, 1)
            if schema_item and view_item:
                selected = (schema_item.text(), view_item.text())
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(f"USE {quoted_db};")
                cursor.execute("""
                    SELECT s.name AS SchemaName, v.name AS ViewName
                    FROM sys.views AS v
                    INNER JOIN sys.schemas AS s ON s.schema_id = v.schema_id
                    WHERE v.is_ms_shipped = 0
                    ORDER BY s.name, v.name;
                """)
                views = [(str(r[0]), str(r[1])) for r in cursor.fetchall()]
            finally:
                cursor.close()
            self._view_structure_all_views = views
            self._filter_view_structure_names()
            if selected:
                for r in range(self.view_structure_list.rowCount()):
                    if (self.view_structure_list.item(r, 0).text(),
                        self.view_structure_list.item(r, 1).text()) == selected:
                        self.view_structure_list.selectRow(r)
                        break
            self.view_structure_status.setText(f"{len(views)} view(s) in {database_name}")
        except Exception as exc:
            self.view_structure_status.setText(f"Could not load views: {exc}")

    def _filter_view_structure_names(self, *_):
        views = getattr(self, "_view_structure_all_views", [])
        needle = self.view_structure_search.text().strip().casefold()
        current = None
        row = self.view_structure_list.currentRow()
        if row >= 0 and self.view_structure_list.item(row, 0) and self.view_structure_list.item(row, 1):
            current = (self.view_structure_list.item(row, 0).text(), self.view_structure_list.item(row, 1).text())
        self.view_structure_list.blockSignals(True)
        self.view_structure_list.setRowCount(0)
        for schema, name in views:
            if needle and needle not in f"{schema}.{name}".casefold():
                continue
            r = self.view_structure_list.rowCount()
            self.view_structure_list.insertRow(r)
            self.view_structure_list.setItem(r, 0, QTableWidgetItem(schema))
            self.view_structure_list.setItem(r, 1, QTableWidgetItem(name))
        self.view_structure_list.blockSignals(False)
        if current:
            for r in range(self.view_structure_list.rowCount()):
                if (self.view_structure_list.item(r, 0).text(), self.view_structure_list.item(r, 1).text()) == current:
                    self.view_structure_list.selectRow(r)
                    break

    def _refresh_view_structure(self):
        self._view_structure_all_views = []
        self.view_structure_tree.clear()
        self._load_view_structure_names()

    @staticmethod
    def _view_referenced_objects(definition):
        """Extract likely two-part or one-part object names following FROM/JOIN."""
        if not definition:
            return []
        # Remove comments and string literals so their contents are not mistaken for object references.
        sql = re.sub(r"/\*.*?\*/", " ", definition, flags=re.S)
        sql = re.sub(r"--[^\r\n]*", " ", sql)
        sql = re.sub(r"N?'(?:''|[^'])*'", "''", sql, flags=re.I)
        pattern = re.compile(
            r"\b(?:FROM|JOIN)\s+"
            r"(?P<name>(?:\[[^]]+\]|[A-Za-z_#@][\w$#@]*)"
            r"(?:\s*\.\s*(?:\[[^]]+\]|[A-Za-z_#@][\w$#@]*)){0,2})",
            re.I
        )
        result = []
        for match in pattern.finditer(sql):
            raw = match.group("name").strip()
            parts = [p.strip().strip("[]").replace("]]", "]") for p in re.split(r"\s*\.\s*", raw)]
            if not parts or parts[0].casefold() in ("select", "values"):
                continue
            # Ignore CTE references later when resolving against actual database objects.
            result.append(tuple(parts[-2:]) if len(parts) >= 2 else (parts[0],))
        return result

    def _show_selected_view_structure(self):
        row = self.view_structure_list.currentRow()
        if row < 0:
            return
        schema_item = self.view_structure_list.item(row, 0)
        name_item = self.view_structure_list.item(row, 1)
        if not schema_item or not name_item:
            return
        database_name = self.view_structure_database_combo.currentText()
        quoted_db = "[" + database_name.replace("]", "]]") + "]"
        selected_key = (schema_item.text(), name_item.text())
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(f"USE {quoted_db};")
                cursor.execute("""
                    SELECT s.name, o.name, o.type, m.definition, o.object_id
                    FROM sys.objects AS o
                    INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
                    LEFT JOIN sys.sql_modules AS m ON m.object_id = o.object_id
                    WHERE o.type IN ('V', 'U') AND o.is_ms_shipped = 0;
                """)
                objects = {}
                objects_by_id = {}
                for schema, name, typ, definition, object_id in cursor.fetchall():
                    obj = {
                        "schema": str(schema), "name": str(name), "type": str(typ),
                        "definition": definition, "object_id": int(object_id)
                    }
                    objects[(str(schema).casefold(), str(name).casefold())] = obj
                    objects_by_id[int(object_id)] = obj

                # Prefer SQL Server's dependency metadata over parsing SQL text. This
                # handles bracketed identifiers, aliases, comments, CTEs and formatting
                # more reliably than looking only for FROM/JOIN tokens.
                dependencies = {}
                cursor.execute("""
                    SELECT d.referencing_id, d.referenced_id,
                           d.referenced_schema_name, d.referenced_entity_name
                    FROM sys.sql_expression_dependencies AS d
                    WHERE d.referencing_id IS NOT NULL;
                """)
                for referencing_id, referenced_id, ref_schema, ref_name in cursor.fetchall():
                    parent_obj = objects_by_id.get(int(referencing_id))
                    if not parent_obj:
                        continue
                    referenced_obj = objects_by_id.get(int(referenced_id)) if referenced_id is not None else None
                    if referenced_obj is None and ref_name:
                        # SQL Server can leave referenced_id NULL for unresolved or
                        # cross-database names. Resolve local schema/object names when possible.
                        if ref_schema:
                            referenced_obj = objects.get((str(ref_schema).casefold(), str(ref_name).casefold()))
                        else:
                            matches = [o for (sch, nm), o in objects.items() if nm == str(ref_name).casefold()]
                            if len(matches) == 1:
                                referenced_obj = matches[0]
                    if referenced_obj and referenced_obj["type"] in ("U", "V"):
                        parent_key = (parent_obj["schema"].casefold(), parent_obj["name"].casefold())
                        dependencies.setdefault(parent_key, []).append(referenced_obj)

                # Ask SQL Server to resolve each view's references as a second
                # metadata-based path. This is more reliable than expression
                # dependencies alone on some older servers / metadata states.
                for parent_obj in objects.values():
                    if parent_obj["type"] != "V":
                        continue
                    qualified_name = ("[" + parent_obj["schema"].replace("]", "]]" ) + "].[" +
                                      parent_obj["name"].replace("]", "]]" ) + "]")
                    try:
                        cursor.execute(
                            "SELECT referenced_schema_name, referenced_entity_name "
                            "FROM sys.dm_sql_referenced_entities(?, 'OBJECT') "
                            "WHERE referenced_entity_name IS NOT NULL;",
                            (qualified_name,)
                        )
                        parent_key = (parent_obj["schema"].casefold(), parent_obj["name"].casefold())
                        for ref_schema, ref_name in cursor.fetchall():
                            if not ref_name:
                                continue
                            if ref_schema:
                                referenced_obj = objects.get((str(ref_schema).casefold(), str(ref_name).casefold()))
                            else:
                                matches = [o for (sch, nm), o in objects.items() if nm == str(ref_name).casefold()]
                                referenced_obj = matches[0] if len(matches) == 1 else None
                            if referenced_obj and referenced_obj["type"] in ("U", "V"):
                                dependencies.setdefault(parent_key, []).append(referenced_obj)
                    except Exception:
                        # Fall back to the parsed definition below if SQL Server
                        # cannot resolve metadata for this view.
                        pass
            finally:
                cursor.close()
            root_data = objects.get((selected_key[0].casefold(), selected_key[1].casefold()))
            if not root_data:
                self.view_structure_status.setText("View definition is unavailable or permissions are insufficient.")
                return
            self.view_structure_tree.clear()
            root = QTreeWidgetItem([f"VIEW  [{selected_key[0]}].[{selected_key[1]}]"])
            root.setToolTip(0, root_data.get("definition") or "Definition unavailable (possibly encrypted or permission-restricted).")
            self.view_structure_tree.addTopLevelItem(root)
            visited = {(selected_key[0].casefold(), selected_key[1].casefold())}
            self._populate_view_dependency_tree(root, root_data, objects, dependencies, visited, 0)

            # Hard fallback: if the recursive builder did not add any child nodes,
            # render the parsed FROM/JOIN references directly. This deliberately
            # does not depend on SQL Server dependency metadata or its object count.
            # The view inventory loaded from sys.views is used to classify known
            # views; every other parsed reference is shown as a table, as requested.
            if root.childCount() == 0:
                parsed_refs = self._view_referenced_objects(root_data.get("definition"))
                known_views = {
                    (str(schema).casefold(), str(name).casefold()): (str(schema), str(name))
                    for schema, name in getattr(self, "_view_structure_all_views", [])
                }
                fallback_nodes = []
                seen_fallback = set()
                for parts in parsed_refs:
                    if not parts:
                        continue
                    if len(parts) >= 2:
                        ref_schema, ref_name = parts[-2], parts[-1]
                        ref_key = (ref_schema.casefold(), ref_name.casefold())
                    else:
                        ref_name = parts[-1]
                        matches = [(sch, nm) for (sch, nm) in known_views if nm == ref_name.casefold()]
                        if len(matches) == 1:
                            ref_key = matches[0]
                            ref_schema, ref_name = known_views[ref_key]
                        else:
                            ref_schema, ref_key = "dbo", ("dbo", ref_name.casefold())
                    if ref_key in seen_fallback:
                        continue
                    seen_fallback.add(ref_key)
                    obj = objects.get(ref_key)
                    # The sys.views inventory is authoritative for classification.
                    # Do not downgrade a known view to TABLE just because the
                    # general object metadata lookup is incomplete.
                    if ref_key in known_views:
                        canonical_schema, canonical_name = known_views[ref_key]
                        view_obj = obj or {
                            "schema": canonical_schema, "name": canonical_name, "type": "V",
                            "definition": None, "object_id": None, "metadata_unavailable": True,
                        }
                        view_obj["schema"] = canonical_schema
                        view_obj["name"] = canonical_name
                        view_obj["type"] = "V"
                        fallback_nodes.append((1, canonical_schema, canonical_name, view_obj))
                    else:
                        # If it is not in the database's view list, treat it as a table.
                        fallback_nodes.append((0, ref_schema, ref_name, obj))
                for is_view, ref_schema, ref_name, obj in sorted(
                    fallback_nodes, key=lambda entry: (entry[0], entry[1].casefold(), entry[2].casefold())
                ):
                    if is_view:
                        item = QTreeWidgetItem([f"VIEW  [{ref_schema}].[{ref_name}]"])
                        item.setToolTip(0, (obj or {}).get("definition") or "Definition unavailable.")
                        root.addChild(item)
                        if obj:
                            key = (obj["schema"].casefold(), obj["name"].casefold())
                            if key not in visited:
                                visited.add(key)
                                self._populate_view_dependency_tree(item, obj, objects, dependencies, visited, 1)
                                visited.remove(key)
                    else:
                        item = QTreeWidgetItem([f"TABLE  [{ref_schema}].[{ref_name}]"])
                        item.setToolTip(0, "Inferred as a table because it was not found in the database's view list.")
                        root.addChild(item)

            root.setExpanded(True)
            # Expand every level so recursively discovered child views are visible immediately.
            self.view_structure_tree.expandAll()
            child_count = root.childCount()
            if child_count == 0:
                parsed_refs = self._view_referenced_objects(root_data.get("definition"))
                parsed_text = ", ".join(".".join(parts) for parts in parsed_refs) or "none detected"
                self.view_structure_status.setText(
                    f"0 direct dependencies for [{selected_key[0]}].[{selected_key[1]}]. "
                    f"Parsed FROM/JOIN references: {parsed_text}. "
                    f"Catalog objects found: {len(objects)}. Check that referenced tables exist in the selected database "
                    "and that your login can view their metadata."
                )
            else:
                self.view_structure_status.setText(
                    f"Structure for [{selected_key[0]}].[{selected_key[1]}]: {child_count} direct dependency/dependencies. "
                    "Tables are listed first, followed by nested views. Hover over a view to inspect its definition."
                )
        except Exception as exc:
            self.view_structure_status.setText(f"Could not build view structure: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def _populate_view_dependency_tree(self, parent, source_obj, objects, dependencies, visited, depth):
        """Build dependency tree from each view's SQL definition.

        Parse references independently for every view. In particular, do not use a
        shared dependency/visited flag to decide whether sibling table references
        should be rendered: tables and nested views must both be displayed at every
        level. The database's view inventory is authoritative for classifying views;
        other FROM/JOIN references are shown as tables.
        """
        if depth > 40 or not source_obj or source_obj.get("type") != "V":
            return

        source_key = (str(source_obj["schema"]).casefold(), str(source_obj["name"]).casefold())
        # Cycle detection is handled by the parent before it recurses into this
        # view. The caller has already added source_key to `visited`, so checking
        # it here would incorrectly stop every nested view at its first entry.

        # Ensure the current view's definition is available, if possible.
        definition = source_obj.get("definition")
        if not definition:
            try:
                database_name = self.view_structure_database_combo.currentText()
                quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
                cursor = self._db.get_connection().cursor()
                try:
                    cursor.execute(f"USE {quoted_db};")
                    cursor.execute("""
                        SELECT m.definition
                        FROM sys.views AS v
                        INNER JOIN sys.schemas AS s ON s.schema_id = v.schema_id
                        LEFT JOIN sys.sql_modules AS m ON m.object_id = v.object_id
                        WHERE s.name = ? AND v.name = ?;
                    """, (source_obj["schema"], source_obj["name"]))
                    row = cursor.fetchone()
                    if row and row[0]:
                        definition = row[0]
                        source_obj["definition"] = definition
                        if source_key in objects:
                            objects[source_key]["definition"] = definition
                finally:
                    cursor.close()
            except Exception:
                definition = None

        if not definition:
            return

        view_inventory = {
            (str(schema).casefold(), str(name).casefold()): (str(schema), str(name))
            for schema, name in getattr(self, "_view_structure_all_views", [])
        }
        # Build a fresh set of direct references for this view. This is deliberately
        # independent of sys.sql_expression_dependencies, which can be incomplete.
        direct = {}
        for parts in self._view_referenced_objects(definition):
            if not parts:
                continue
            if len(parts) >= 2:
                schema_name, object_name = str(parts[-2]), str(parts[-1])
                key = (schema_name.casefold(), object_name.casefold())
            else:
                object_name = str(parts[-1])
                matches = [(key, canonical) for key, canonical in view_inventory.items()
                           if key[1] == object_name.casefold()]
                if len(matches) == 1:
                    key, canonical = matches[0]
                    schema_name, object_name = canonical
                else:
                    # If SQL Server metadata is available, use a unique catalog match;
                    # otherwise follow the requested rule and infer a dbo table.
                    catalog_matches = [(key, obj) for key, obj in objects.items()
                                       if key[1] == object_name.casefold()]
                    if len(catalog_matches) == 1:
                        key, obj = catalog_matches[0]
                        schema_name, object_name = obj["schema"], obj["name"]
                    else:
                        schema_name = "dbo"
                        key = (schema_name.casefold(), object_name.casefold())
            if key == source_key:
                continue
            if key in view_inventory:
                canonical_schema, canonical_name = view_inventory[key]
                obj = objects.get(key)
                if obj is None:
                    obj = {"schema": canonical_schema, "name": canonical_name, "type": "V",
                           "definition": None, "object_id": None, "metadata_unavailable": True}
                else:
                    obj["schema"], obj["name"], obj["type"] = canonical_schema, canonical_name, "V"
                direct[key] = ("V", obj)
            else:
                obj = objects.get(key)
                if obj is None:
                    obj = {"schema": schema_name, "name": object_name, "type": "U",
                           "definition": None, "object_id": None, "inferred_table": True}
                elif obj.get("type") == "V":
                    # Defensive consistency: if the view inventory says otherwise,
                    # trust the inventory rather than stale/ambiguous catalog data.
                    obj["type"] = "U"
                direct[key] = ("U", obj)

        tables = [obj for typ, obj in direct.values() if typ == "U"]
        views = [obj for typ, obj in direct.values() if typ == "V"]

        # Always render all tables first, regardless of whether sibling views exist.
        for obj in sorted(tables, key=lambda item: (item["schema"].casefold(), item["name"].casefold())):
            item = QTreeWidgetItem([f"TABLE  [{obj['schema']}].[{obj['name']}]"])
            if obj.get("inferred_table"):
                item.setToolTip(0, "Not found in the view inventory; treated as a table as requested.")
            parent.addChild(item)

        # Then render views and recursively build each one's own children.
        for obj in sorted(views, key=lambda item: (item["schema"].casefold(), item["name"].casefold())):
            key = (obj["schema"].casefold(), obj["name"].casefold())
            item = QTreeWidgetItem([f"VIEW  [{obj['schema']}].[{obj['name']}]"])
            item.setToolTip(0, obj.get("definition") or "Definition unavailable (possibly encrypted or permission-restricted).")
            parent.addChild(item)
            if key in visited:
                item.addChild(QTreeWidgetItem(["(circular view reference — recursion stopped)"]))
                continue
            visited.add(key)
            self._populate_view_dependency_tree(item, obj, objects, dependencies, visited, depth + 1)
            visited.remove(key)
            if item.childCount() == 0 and not obj.get("definition"):
                item.setToolTip(0, "Definition unavailable, so nested dependencies could not be parsed. Check VIEW DEFINITION permission.")

    def show_programmability_objects(self):
        if hasattr(self, "server_health_panel"):
            self.server_health_panel.hide()
        if hasattr(self, "view_structure_panel"):
            self.view_structure_panel.hide()
        """Open stored procedure/function inventory and load available databases."""
        if hasattr(self, "timeline_panel"):
            self.timeline_panel.hide()
        if hasattr(self, "steps_timeline_panel"):
            self.steps_timeline_panel.hide()
        if not self._ensure_connection():
            return
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute("""
                    SELECT name FROM sys.databases
                    WHERE state_desc = 'ONLINE' AND HAS_DBACCESS(name) = 1
                    ORDER BY name;
                """)
                names = [row[0] for row in cursor.fetchall()]
            finally:
                cursor.close()
            previous = self.programmability_database_combo.currentText()
            self.programmability_database_combo.clear()
            self.programmability_database_combo.addItems(names)
            if previous in names:
                self.programmability_database_combo.setCurrentText(previous)
            elif self._settings.database in names:
                self.programmability_database_combo.setCurrentText(self._settings.database)
            self._leave_server_health_monitor()
            self._set_view_mode("programmability")
            self.table.hide(); self.space_scroll.hide(); self.tables_panel.hide()
            self.server_info_panel.hide(); self.triggers_panel.hide(); self.indexes_panel.hide()
            self.programmability_panel.show()
            self.jobs_panel.hide()
            self.processes_panel.hide()
            self.cpu_panel.hide()
            self.total_gb_label.hide(); self.total_mb_label.hide()
            if names:
                self.refresh_programmability_objects()
            else:
                self.programmability_list.setRowCount(0)
                self.programmability_sql_text.clear()
                self.programmability_count_label.setText("No accessible online databases found.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "Stored Procedures/Functions", "Could not load database list.\n\n" + str(exc))

    def refresh_programmability_objects(self):
        """Load procedures, functions, views, and triggers from the selected database."""
        if not self._ensure_connection():
            return
        database_name = self.programmability_database_combo.currentText()
        if not database_name:
            QMessageBox.information(self, "List and Search", "Select a database first.")
            return
        quoted_db = "[" + database_name.replace("]", "]]" ) + "]"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(f"Loading programmable objects from {database_name}...")
        try:
            cursor = self._db.get_connection().cursor()
            try:
                cursor.execute(f"USE {quoted_db};")
                cursor.execute("""
                    SELECT
                        CASE WHEN o.type = 'P' THEN 'Stored Procedure' WHEN o.type = 'V' THEN 'View' WHEN o.type = 'TR' THEN 'Trigger' ELSE 'Function' END AS [ObjectType],
                        s.name AS [SchemaName], o.name AS [ObjectName],
                        o.create_date AS [CreatedDate], o.modify_date AS [ModifiedDate],
                        m.definition AS [Definition], o.type AS [ObjectTypeCode]
                    FROM sys.objects AS o
                    INNER JOIN sys.schemas AS s ON s.schema_id = o.schema_id
                    LEFT JOIN sys.sql_modules AS m ON m.object_id = o.object_id
                    WHERE o.is_ms_shipped = 0
                      AND o.type IN ('P','FN','IF','TF','FS','FT','V','TR')
                    ORDER BY CASE WHEN o.type = 'P' THEN 0 WHEN o.type = 'TR' THEN 3 ELSE 1 END, s.name, o.name;
                """)
                rows = cursor.fetchall()
            finally:
                cursor.close()
            self._all_programmability_rows = list(rows)
            self._apply_programmability_filters()
            self.statusBar().showMessage(f"Loaded {len(rows):,} programmable object(s) from {database_name}.")
        except (pyodbc.Error, ValueError, ConnectionError) as exc:
            QMessageBox.critical(self, "List and Search", "Could not retrieve object definitions.\n\n" + str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _apply_programmability_filters(self, *_args):
        """Filter objects by type and text found in their SQL definitions."""
        if not hasattr(self, "_all_programmability_rows"):
            return
        show_procs = self.programmability_procs_check.isChecked()
        show_funcs = self.programmability_funcs_check.isChecked()
        show_views = self.programmability_views_check.isChecked()
        show_triggers = self.programmability_triggers_check.isChecked()
        term = self.programmability_search_edit.text().strip().casefold()
        filtered = []
        for row in self._all_programmability_rows:
            object_type, schema_name, object_name, created, modified, definition, type_code = row
            # Use the displayed type label for UI filtering, so the checkbox
            # semantics cannot drift from the labels shown in the first column.
            is_proc = str(object_type).strip().casefold() == "stored procedure"
            is_func = str(object_type).strip().casefold() == "function"
            is_view = str(object_type).strip().casefold() == "view"
            is_trigger = str(object_type).strip().casefold() == "trigger"
            if is_proc and not show_procs:
                continue
            if is_func and not show_funcs:
                continue
            if is_view and not show_views:
                continue
            if is_trigger and not show_triggers:
                continue
            if not (is_proc or is_func or is_view or is_trigger):
                continue
            searchable = " ".join([str(schema_name or ""), str(object_name or ""), str(definition or "")]).casefold()
            if term and term not in searchable:
                continue
            filtered.append(row)
        self.programmability_list.setSortingEnabled(False)
        self.programmability_list.setRowCount(len(filtered))
        for r, row in enumerate(filtered):
            object_type, schema_name, object_name, created, modified, definition, type_code = row
            values = [object_type, schema_name, object_name, str(created or ""), str(modified or "")]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if c in (3, 4):
                    item.setData(Qt.ItemDataRole.UserRole, (created if c == 3 else modified))
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole + 1, definition)
                self.programmability_list.setItem(r, c, item)
        self.programmability_list.setSortingEnabled(True)
        self.programmability_list.resizeColumnsToContents()
        procedure_count = sum(
            1 for row in filtered
            if str(row[0]).strip().casefold() == "stored procedure"
        )
        function_count = sum(
            1 for row in filtered
            if str(row[0]).strip().casefold() == "function"
        )
        view_count = sum(
            1 for row in filtered
            if str(row[0]).strip().casefold() == "view"
        )
        trigger_count = sum(
            1 for row in filtered
            if str(row[0]).strip().casefold() == "trigger"
        )
        self.programmability_count_label.setText(
            f"Showing {len(filtered):,} object(s) — {procedure_count:,} stored procedure(s), "
            f"{function_count:,} function(s), {view_count:,} view(s), {trigger_count:,} trigger(s)"
        )
        self.programmability_sql_text.clear()

    def _on_programmability_selection_changed(self):
        selected = self.programmability_list.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        type_item = self.programmability_list.item(row, 0)
        definition = type_item.data(Qt.ItemDataRole.UserRole + 1) if type_item else None
        schema_name = self.programmability_list.item(row, 1).text()
        object_name = self.programmability_list.item(row, 2).text()
        object_type = type_item.text() if type_item else "Object"
        if definition is None:
            definition = "Definition is unavailable (the object may be encrypted or you may lack VIEW DEFINITION permission)."
        display_text = f"-- {object_type}: [{schema_name}].[{object_name}]\n\n{definition}"
        self.programmability_sql_text.setPlainText(display_text)
        self._highlight_programmability_search_term()

    def _highlight_programmability_search_term(self):
        """Highlight every case-insensitive occurrence of the search term in SQL text."""
        term = self.programmability_search_edit.text().strip()
        text = self.programmability_sql_text.toPlainText()
        selections = []

        if term:
            fmt = QTextCharFormat()
            fmt.setBackground(QColor("#fff176"))
            fmt.setForeground(QColor("#202020"))

            # Find matches in the Python string, then select the corresponding
            # character ranges in the QPlainTextEdit document.
            for match in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
                cursor = QTextCursor(self.programmability_sql_text.document())
                cursor.setPosition(match.start())
                cursor.setPosition(match.end(), QTextCursor.MoveMode.KeepAnchor)
                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format = fmt
                selections.append(selection)

        self.programmability_sql_text.setExtraSelections(selections)

    def _export_programmability_to_excel(self):
        if self.programmability_list.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export", "No stored procedures, functions, or views match the current filters.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export procedures/functions/views to Excel",
            "programmability_objects.xlsx", "Excel workbook (*.xlsx)")
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter
            wb = Workbook()
            ws = wb.active
            ws.title = "Programmability"
            ws.append(["Type", "Schema", "Name", "Created", "Last Modified", "SQL Definition"])
            for r in range(self.programmability_list.rowCount()):
                type_item = self.programmability_list.item(r, 0)
                ws.append([type_item.text(), self.programmability_list.item(r, 1).text(),
                    self.programmability_list.item(r, 2).text(), self.programmability_list.item(r, 3).text(),
                    self.programmability_list.item(r, 4).text(), type_item.data(Qt.ItemDataRole.UserRole + 1) or ""])
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F4E78")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                letter = get_column_letter(col[0].column)
                ws.column_dimensions[letter].width = min(max(max(len(str(c.value or "")) for c in col) + 2, 12), 55)
            ws.column_dimensions["F"].width = 80
            wb.save(path)
            QMessageBox.information(self, "Export complete", f"Exported {self.programmability_list.rowCount():,} object(s) to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Excel export failed", str(exc))

    def export_to_excel(self):
        """Export the currently displayed database file or space usage data."""
        if self._view_mode == "space":
            self._export_space_usage_to_excel()
            return
        if self._view_mode == "tables":
            self._export_tables_size_to_excel()
            return
        if self._view_mode == "indexes":
            self._export_indexes_to_excel()
            return
        if self._view_mode == "programmability":
            self._export_programmability_to_excel()
            return
        if self._view_mode != "table":
            QMessageBox.information(
                self,
                "Excel export",
                "Excel export is currently available for Database files size. "
                "Select Database files > Database files size first.",
            )
            return
        if self.table.columnCount() == 0 or self.table.rowCount() == 0:
            QMessageBox.information(
                self,
                "Nothing to export",
                "Run Database files > Database files size before exporting.",
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export to Excel",
            "database_file_sizes.xlsx",
            "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter

            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Database file sizes"

            headers = [
                self.table.horizontalHeaderItem(column).text()
                for column in range(self.table.columnCount())
            ]
            worksheet.append(headers)
            for header_cell in worksheet[1]:
                header_cell.font = Font(bold=True, color="FFFFFF")
                header_cell.fill = PatternFill("solid", fgColor="34495E")
                header_cell.alignment = Alignment(vertical="center")
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{self.table.rowCount() + 1}"

            for row in range(self.table.rowCount()):
                excel_row = []
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    if item is None:
                        excel_row.append(None)
                        continue
                    value = item.data(Qt.ItemDataRole.UserRole)
                    if value is not None and isinstance(value, (int, float, Decimal)):
                        excel_row.append(float(value))
                    else:
                        excel_row.append(item.text())
                worksheet.append(excel_row)

            # Add a summary row for the file sizes.
            total_mb = 0.0
            for row in range(2, worksheet.max_row + 1):
                value = worksheet.cell(row=row, column=4).value
                if isinstance(value, (int, float, Decimal)):
                    total_mb += float(value)
            summary_row = worksheet.max_row + 2
            worksheet.cell(row=summary_row, column=1, value="TOTAL")
            worksheet.cell(row=summary_row, column=4, value=round(total_mb, 2))
            worksheet.cell(row=summary_row, column=5, value=round(total_mb / 1024.0, 3))
            for cell in worksheet[summary_row]:
                if cell.value is not None:
                    cell.font = Font(bold=True)

            # Reasonable default widths, capped for long file paths.
            for column_cells in worksheet.columns:
                column_letter = get_column_letter(column_cells[0].column)
                max_length = max(
                    (len(str(cell.value)) for cell in column_cells if cell.value is not None),
                    default=10,
                )
                worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 12), 60)
            worksheet.column_dimensions["H"].width = 60

            workbook.save(path)
            self.statusBar().showMessage(f"Exported {self.table.rowCount():,} row(s) to Excel.")
            QMessageBox.information(self, "Export complete", f"Excel file saved to:\n{path}")
        except ImportError:
            QMessageBox.critical(
                self,
                "Missing dependency",
                "Excel export requires openpyxl. Install it with:\n\npip install openpyxl",
            )
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.critical(
                self,
                "Export failed",
                "Could not save the Excel file. Make sure it is not open in Excel and that you have write permission.\n\n"
                + str(exc),
            )

    def _export_indexes_to_excel(self):
        """Export the currently filtered index list to Excel."""
        if self.indexes_list.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export", "No indexes match the current filters.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export indexes to Excel", "table_indexes.xlsx", "Excel workbook (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            QMessageBox.critical(self, "Excel export", "Excel export requires openpyxl. Install it with:\n\npip install openpyxl")
            return
        headers = ["Schema", "Table", "Index", "Type", "Size MB", "RowCount", "FillFactor",
                   "IsPrimaryKey", "IsUnique", "IsUniqueConstraint", "IsDisabled", "IsPadded",
                   "IgnoreDupKey", "AllowRowLocks", "AllowPageLocks", "HasFilter", "FilterDefinition",
                   "IsHypothetical", "DataSpace", "DataSpaceId"]
        wb = Workbook()
        ws = wb.active
        ws.title = "Indexes"
        ws.append(headers)
        for r in range(self.indexes_list.rowCount()):
            data = self.indexes_list.item(r, 0).data(Qt.ItemDataRole.UserRole + 1) or {}
            ws.append([data.get("schema"), data.get("table"), data.get("name"), data.get("type"),
                       float(data.get("size_mb") or 0), int(data.get("row_count") or 0), data.get("fill_factor"),
                       data.get("primary_key"), data.get("unique"), data.get("unique_constraint"),
                       data.get("disabled"), data.get("padded"), data.get("ignore_dup_key"),
                       data.get("allow_row_locks"), data.get("allow_page_locks"), data.get("has_filter"),
                       data.get("filter_definition"), data.get("hypothetical"), data.get("data_space_name"),
                       data.get("data_space_id")])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col in ws.columns:
            letter = get_column_letter(col[0].column)
            ws.column_dimensions[letter].width = min(max(max(len(str(c.value or "")) for c in col) + 2, 12), 45)
        try:
            wb.save(path)
        except Exception as exc:
            QMessageBox.critical(self, "Excel export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Exported {self.indexes_list.rowCount():,} indexes to:\n{path}")

    def _export_tables_size_to_excel(self):
        if self.tables_result.rowCount() == 0:
            QMessageBox.information(self, "Nothing to export",
                                    "Run Tables > Tables size and load results first.")
            return
        db_name = self.tables_database_combo.currentText() or "database"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export table sizes to Excel",
            f"{db_name}_table_sizes.xlsx", "Excel workbook (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter

            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Tables size"
            headers = [self.tables_result.horizontalHeaderItem(c).text()
                       for c in range(self.tables_result.columnCount())]
            worksheet.append(headers)
            for cell in worksheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="34495E")
            for r in range(self.tables_result.rowCount()):
                output = []
                for c in range(self.tables_result.columnCount()):
                    item = self.tables_result.item(r, c)
                    if item is None:
                        output.append(None)
                    elif c in (2, 4):
                        value = item.data(Qt.ItemDataRole.UserRole)
                        output.append(value if value is not None else item.text())
                    else:
                        output.append(item.text())
                worksheet.append(output)
            total_mb = sum(float(row[4] or 0) for row in self._tables_size_rows)
            total_row = worksheet.max_row + 1
            worksheet.cell(total_row, 1, "TOTAL")
            worksheet.cell(total_row, 5, round(total_mb, 2))
            for cell in worksheet[total_row]:
                if cell.value is not None:
                    cell.font = Font(bold=True)
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{worksheet.max_row - 1}"
            for col in worksheet.columns:
                letter = get_column_letter(col[0].column)
                max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
                worksheet.column_dimensions[letter].width = min(max(max_len + 2, 12), 40)
            workbook.save(path)
            QMessageBox.information(self, "Export complete", f"Excel file saved to:\\n{path}")
        except ImportError:
            QMessageBox.critical(self, "Missing dependency",
                                 "Excel export requires openpyxl. Install it with:\\n\\npip install openpyxl")
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.critical(self, "Export failed", "Could not save Excel file.\\n\\n" + str(exc))

    def _export_space_usage_to_excel(self):
        """Export the space usage bars as numeric data in an Excel workbook."""
        if not self._space_usage_results:
            QMessageBox.information(
                self,
                "Nothing to export",
                "Run Database files > Space usage before exporting.",
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export space usage to Excel",
            "database_space_usage.xlsx",
            "Excel workbook (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter

            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = "Space usage"
            headers = [
                "Database", "Data files total (MB)", "Data files total (GB)",
                "Data (MB)", "Indexes (MB)", "Unused (MB)", "Unallocated (MB)",
                "Log total (MB)", "Log total (GB)", "Log used (MB)",
                "Log free (MB)", "Log used (%)",
            ]
            worksheet.append(headers)
            for cell in worksheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="34495E")
                cell.alignment = Alignment(vertical="center", wrap_text=True)

            for db in self._space_usage_results:
                total_log = float(db["log_total"] or 0.0)
                log_used = float(db["log_used"] or 0.0)
                worksheet.append([
                    db["name"], float(db["data_total"]), float(db["data_total"]) / 1024.0,
                    float(db["data"]), float(db["indexes"]), float(db["unused"]),
                    float(db["unallocated"]), total_log, total_log / 1024.0,
                    log_used, float(db["log_free"]),
                    (log_used / total_log) if total_log else 0.0,
                ])
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{worksheet.max_row}"
            for row in worksheet.iter_rows(min_row=2, min_col=12, max_col=12):
                row[0].number_format = "0.0%"
            for col in worksheet.columns:
                letter = get_column_letter(col[0].column)
                max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
                worksheet.column_dimensions[letter].width = min(max(max_len + 2, 14), 32)
            workbook.save(path)
            self.statusBar().showMessage(
                f"Exported space usage for {len(self._space_usage_results):,} database(s)."
            )
            QMessageBox.information(self, "Export complete", f"Excel file saved to:\n{path}")
        except ImportError:
            QMessageBox.critical(
                self, "Missing dependency",
                "Excel export requires openpyxl. Install it with:\n\npip install openpyxl",
            )
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.critical(
                self, "Export failed",
                "Could not save the Excel file. Make sure it is not open in Excel and that you have write permission.\n\n" + str(exc),
            )

    def closeEvent(self, event):
        if self._db is not None:
            self._db.disconnect()
            self._db = None
        event.accept()
