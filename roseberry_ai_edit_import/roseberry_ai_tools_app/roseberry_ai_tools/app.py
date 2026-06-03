from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .backend_bridge import (
    BACKEND_NAME,
    LOG_PATH,
    BackendBridge,
    JsonValidation,
    ResolveContext,
    TimelineActionResult,
    log,
)


APP_TITLE = "Roseberry AI Tools"
MODULE_TITLE = "AI Edit Import"
APP_BUILD = "0.2.0"


class TaskWorker(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str, str)

    def __init__(self, action: str, json_path: str = "") -> None:
        super().__init__()
        self.action = action
        self.json_path = json_path

    def run(self) -> None:
        try:
            bridge = BackendBridge()
            if self.action == "context":
                self.finished.emit(self.action, bridge.get_context())
                return
            context = bridge.get_context()
            if self.action == "validate":
                self.finished.emit(self.action, bridge.validate_json(self.json_path, context))
                return
            if self.action == "markers":
                self.finished.emit(self.action, bridge.run_markers(self.json_path))
                return
            if self.action == "timeline":
                self.finished.emit(self.action, bridge.run_create_timeline(self.json_path))
                return
            raise RuntimeError("Unknown task: {}".format(self.action))
        except Exception as exc:
            details = traceback.format_exc()
            log("Task {} failed: {}".format(self.action, exc))
            log(details)
            self.failed.emit(self.action, str(exc), details)


class StepCard(QFrame):
    def __init__(self, number: str, title: str, description: str) -> None:
        super().__init__()
        self.setObjectName("StepCard")
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(13)

        header = QHBoxLayout()
        badge = QLabel(number)
        badge.setObjectName("StepNumber")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(28, 28)
        self.badge = badge
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        header.addWidget(badge)
        header.addWidget(title_label)
        header.addStretch(1)
        layout.addLayout(header)

        helper = QLabel(description)
        helper.setObjectName("CardDescription")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        layout.addLayout(self.body)

    def set_state(self, state: str) -> None:
        self.badge.setProperty("state", state)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)


class DetailGrid(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(7)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for row, (label, value) in enumerate(rows):
            key = QLabel(label)
            key.setObjectName("FieldLabel")
            val = QLabel(value or "Unavailable")
            val.setObjectName("FieldValue")
            val.setWordWrap(True)
            self.grid.addWidget(key, row, 0)
            self.grid.addWidget(val, row, 1)
        self.setFixedHeight(max(52, len(rows) * 29))
        self.grid.setColumnStretch(0, 0)
        self.grid.setColumnStretch(1, 1)


class RoseberryWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("{} - {}".format(APP_TITLE, MODULE_TITLE))
        self.resize(1180, 900)
        self.setMinimumSize(920, 720)
        self.context: Optional[ResolveContext] = None
        self.validation: Optional[JsonValidation] = None
        self.json_path = ""
        self.thread: Optional[QThread] = None
        self.worker: Optional[TaskWorker] = None
        self.is_busy = False
        self.last_traceback = ""
        self.last_action_summary = ""
        self.timeline_result: Optional[TimelineActionResult] = None
        self.review_filter = "ALL"
        self.workflow_state = "NO_JSON"
        self.action_log_lines: list[str] = []
        self.context_refresh_reason = "launch"
        self.bridge = BackendBridge()

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        content.setObjectName("Content")
        scroll.setWidget(content)
        root_layout.addWidget(scroll)

        outer = QVBoxLayout(content)
        outer.setSizeConstraint(QLayout.SetMinimumSize)
        outer.setContentsMargins(40, 32, 40, 34)
        outer.setSpacing(18)
        outer.addWidget(self._build_header())

        overview = QHBoxLayout()
        overview.setSpacing(18)
        overview.addWidget(self._build_timeline_step(), 1)
        overview.addWidget(self._build_json_step(), 1)
        outer.addLayout(overview)

        lower = QHBoxLayout()
        lower.setSpacing(16)
        lower.addWidget(self._build_validation_step(), 1)
        lower.addWidget(self._build_action_step(), 1)
        outer.addLayout(lower)
        outer.addWidget(self._build_report_panel())
        outer.addWidget(self._build_segment_review_panel())
        outer.addWidget(self._build_advanced_panel())

        self._set_initial_state()
        self.refresh_context()

    def _build_header(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("HeroPanel")
        header = QHBoxLayout(frame)
        header.setContentsMargins(26, 23, 26, 23)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        eyebrow = QLabel("ROSEBERRY")
        eyebrow.setObjectName("Eyebrow")
        product = QLabel(APP_TITLE)
        product.setObjectName("ProductTitle")
        module = QLabel(MODULE_TITLE)
        module.setObjectName("ModuleTitle")
        subtitle = QLabel("Validate and import AI-generated edit segments into DaVinci Resolve.")
        subtitle.setObjectName("Subtitle")
        module_row = QHBoxLayout()
        module_row.setSpacing(10)
        module_row.addWidget(module)
        module_row.addStretch(1)
        title_box.addWidget(eyebrow)
        title_box.addWidget(product)
        title_box.addLayout(module_row)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        self.status_badge = QLabel("STARTING")
        self.status_badge.setObjectName("StatusBadge")
        status_box = QVBoxLayout()
        status_box.setAlignment(Qt.AlignTop | Qt.AlignRight)
        self.global_status = QLabel("Checking DaVinci timeline...")
        self.global_status.setObjectName("GlobalStatus")
        self.global_status.setAlignment(Qt.AlignRight)
        status_box.addWidget(self.status_badge, alignment=Qt.AlignRight)
        status_box.addWidget(self.global_status, alignment=Qt.AlignRight)
        header.addLayout(status_box)
        return frame

    def _build_timeline_step(self) -> QFrame:
        card = StepCard(
            "1",
            "Timeline",
            "Active Resolve context. Reload only after switching timelines.",
        )
        self.timeline_card = card
        self.context_grid = DetailGrid()
        card.body.addWidget(self.context_grid)
        self.refresh_button = QPushButton("Reload Timeline Context")
        self.refresh_button.setObjectName("UtilityButton")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_button.clicked.connect(self.reload_timeline_context)
        self.refresh_button.setToolTip("Reload the active Resolve timeline after switching timelines.")
        card.body.addWidget(self.refresh_button, alignment=Qt.AlignLeft)
        return card

    def _build_json_step(self) -> QFrame:
        card = StepCard(
            "2",
            "Segments File",
            "Select the AI edit plan to validate against this timeline.",
        )
        self.json_card = card
        row = QHBoxLayout()
        self.choose_json_button = QPushButton("Choose Segments File")
        self.choose_json_button.setObjectName("PrimaryButton")
        self.choose_json_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.choose_json_button.clicked.connect(self.choose_json)
        self.selected_file = QLabel("No segments file selected")
        self.selected_file.setObjectName("SelectedFile")
        self.selected_file.setWordWrap(True)
        row.addWidget(self.choose_json_button, 0)
        row.addWidget(self.selected_file, 1)
        card.body.addLayout(row)
        self.json_grid = DetailGrid()
        card.body.addWidget(self.json_grid)
        return card

    def _build_validation_step(self) -> QFrame:
        card = StepCard(
            "3",
            "Validate",
            "Check structure, FPS metadata, and timeline compatibility. Resolve is not modified.",
        )
        self.validation_card = card
        self.validate_button = QPushButton("Validate Segments")
        self.validate_button.setObjectName("PrimaryButton")
        self.validate_button.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
        self.validate_button.clicked.connect(self.validate_json)
        card.body.addWidget(self.validate_button)
        self.validation_hint = QLabel("Select a JSON or Excel segments file to continue.")
        self.validation_hint.setObjectName("InlineHint")
        self.validation_hint.setWordWrap(True)
        card.body.addWidget(self.validation_hint)
        return card

    def _build_action_step(self) -> QFrame:
        card = StepCard(
            "4",
            "Run Action",
            "Create the editor review timeline after validation passes.",
        )
        self.action_card = card
        self.markers_button = QPushButton("Add Markers Only")
        self.markers_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.markers_button.clicked.connect(self.run_markers)
        self.timeline_button = QPushButton("Create Edited Timeline")
        self.timeline_button.setObjectName("PrimaryButton")
        self.timeline_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogNewFolder))
        self.timeline_button.clicked.connect(self.run_timeline)
        card.body.addWidget(self.markers_button)
        card.body.addWidget(self.timeline_button)
        return card

    def _build_report_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ReportPanel")
        frame.setMinimumHeight(104)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        title = QLabel("Editor Guidance")
        title.setObjectName("CardTitle")
        self.report = QLabel("Starting Roseberry AI Tools...")
        self.report.setObjectName("ReportText")
        self.report.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.report)
        return frame

    def _build_segment_review_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ReviewPanel")
        frame.setVisible(False)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Created Segment Review")
        title.setObjectName("CardTitle")
        self.review_summary = QLabel("")
        self.review_summary.setObjectName("CardDescription")
        self.review_summary.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(self.review_summary)
        heading.addLayout(title_box)
        heading.addStretch(1)
        self.review_status = QLabel("PASS")
        self.review_status.setObjectName("RowStatusBadge")
        heading.addWidget(self.review_status, alignment=Qt.AlignTop)
        layout.addLayout(heading)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.review_total_chip = QLabel("0 SEGMENTS")
        self.review_pass_chip = QLabel("0 PASS")
        self.review_warning_chip = QLabel("0 WARNINGS")
        self.review_mismatch_chip = QLabel("0 MISMATCHES")
        for chip, state in (
            (self.review_total_chip, "NEUTRAL"),
            (self.review_pass_chip, "PASS"),
            (self.review_warning_chip, "WARNING"),
            (self.review_mismatch_chip, "MISMATCH"),
        ):
            chip.setObjectName("SummaryChip")
            chip.setProperty("state", state)
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(7)
        filter_label = QLabel("SHOW")
        filter_label.setObjectName("FilterLabel")
        filter_row.addWidget(filter_label)
        self.review_filter_buttons: dict[str, QPushButton] = {}
        for filter_name, label in (
            ("ALL", "All"),
            ("WARNING", "Warnings"),
            ("MISMATCH", "Mismatches"),
        ):
            button = QPushButton(label)
            button.setObjectName("FilterButton")
            button.setCheckable(True)
            button.setChecked(filter_name == "ALL")
            button.clicked.connect(
                lambda _checked=False, selected=filter_name: self._set_review_filter(selected)
            )
            self.review_filter_buttons[filter_name] = button
            filter_row.addWidget(button)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        self.review_timing_note = QLabel(
            "Source Time uses milliseconds: HH:MM:SS.mmm. Timeline Time uses Resolve timecode: HH:MM:SS:FF."
        )
        self.review_timing_note.setObjectName("InlineHint")
        self.review_timing_note.setWordWrap(True)
        layout.addWidget(self.review_timing_note)

        self.review_table = QTableWidget()
        self.review_table.setObjectName("ReviewTable")
        self.review_table.setColumnCount(7)
        self.review_table.setHorizontalHeaderLabels([
            "Segment",
            "Name",
            "Source Time",
            "Timeline Time",
            "Duration",
            "Frames",
            "Reason for Cut",
        ])
        self.review_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.review_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.review_table.setAlternatingRowColors(True)
        self.review_table.setShowGrid(False)
        self.review_table.setMinimumHeight(310)
        self.review_table.verticalHeader().setVisible(False)
        header = self.review_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.review_table.setColumnWidth(1, 190)
        self.review_table.setColumnWidth(6, 260)
        layout.addWidget(self.review_table)

        actions = QHBoxLayout()
        self.open_mapping_button = QPushButton("Open Mapping Report")
        self.open_mapping_button.clicked.connect(self.open_mapping_report)
        self.reveal_report_button = QPushButton("Reveal Report Folder")
        self.reveal_report_button.clicked.connect(self.reveal_report_folder)
        self.export_validation_button = QPushButton("Export Validation CSV")
        self.export_validation_button.clicked.connect(self.export_validation_csv)
        self.copy_summary_button = QPushButton("Copy Summary")
        self.copy_summary_button.clicked.connect(self.copy_validation_summary)
        for button in (
            self.open_mapping_button,
            self.reveal_report_button,
            self.export_validation_button,
            self.copy_summary_button,
        ):
            button.setObjectName("UtilityButton")
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.review_panel = frame
        return frame

    def _build_advanced_panel(self) -> QGroupBox:
        panel = QGroupBox("Advanced / Debug")
        panel.setObjectName("AdvancedPanel")
        panel.setCheckable(True)
        panel.setChecked(False)
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        panel.setMaximumHeight(36)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 18, 16, 14)
        self.advanced_text = QPlainTextEdit()
        self.advanced_text.setObjectName("AdvancedText")
        self.advanced_text.setReadOnly(True)
        self.advanced_text.setMinimumHeight(190)
        self.advanced_text.setVisible(False)
        self.reveal_log_button = QPushButton("Reveal Debug Log")
        self.reveal_log_button.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.reveal_log_button.clicked.connect(self.reveal_log)
        self.reveal_log_button.setVisible(False)
        layout.addWidget(self.advanced_text)
        layout.addWidget(self.reveal_log_button, alignment=Qt.AlignLeft)
        panel.toggled.connect(self.advanced_text.setVisible)
        panel.toggled.connect(self.reveal_log_button.setVisible)
        panel.toggled.connect(self._toggle_advanced_panel)
        return panel

    def _toggle_advanced_panel(self, expanded: bool) -> None:
        self.sender().setMaximumHeight(340 if expanded else 36)

    def _set_initial_state(self) -> None:
        self.context_grid.set_rows([
            ("Project", "Loading"),
            ("Timeline", "Loading"),
            ("FPS", "Loading"),
            ("Safety", "Checking"),
        ])
        self.json_grid.set_rows([
            ("File", "No segments file selected"),
            ("Segments", "Unavailable"),
            ("Compatibility", "Unavailable"),
        ])
        self._set_report("Checking the current DaVinci timeline...")
        self._set_workflow_status("NO_JSON", "No JSON selected")
        self._update_action_state()

    def refresh_context(self, reason: str = "launch") -> None:
        self.context_refresh_reason = reason
        self._append_action_log(
            "Loading the active DaVinci timeline..."
            if reason == "launch"
            else "Reloading the active DaVinci timeline..."
        )
        self._run_task("context")

    def reload_timeline_context(self) -> None:
        if self.json_path:
            self.validation = None
            self._clear_segment_review()
            self._set_workflow_status("JSON_SELECTED", "JSON selected, validation required")
            self.validation_hint.setText(
                "Timeline context is reloading. Validate the selected segments file again when it finishes."
            )
        self.refresh_context(reason="manual")

    def choose_json(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose segments JSON or Excel file",
            str(Path.home() / "Desktop"),
            "Segments files (*.json *.xlsx);;JSON files (*.json);;Excel files (*.xlsx);;All files (*)",
        )
        if not path:
            self.validation_hint.setText("Selection cancelled. Your current file was not changed.")
            return
        self.json_path = path
        self.validation = None
        self._clear_segment_review()
        self.selected_file.setText(Path(path).name)
        self._render_json_placeholder()
        self._update_action_state()
        self._log_selection_state()

    def validate_json(self) -> None:
        if not self.json_path:
            self.validation_hint.setText("Choose a segments JSON or Excel file first.")
            return
        self._set_workflow_status("VALIDATING", "Validating...")
        self._append_action_log("Validating segments. Resolve will not be modified.")
        self._run_task("validate", self.json_path)

    def run_markers(self) -> None:
        if not self._can_run_mutation():
            self._append_action_log("Markers were not run. Validate the segments file successfully first.")
            return
        if self._confirm_timeline_change("Add Markers Only"):
            self._append_action_log("Adding markers to the active timeline...")
            self._run_task("markers", self.json_path)

    def run_timeline(self) -> None:
        if not self._can_run_mutation(require_edit_timeline=True):
            self._append_action_log("Edited timeline was not created. Validate the segments file successfully first.")
            return
        if self._confirm_timeline_change("Create Edited Timeline"):
            self._append_action_log("Creating a new edited timeline. Keep DaVinci Resolve open.")
            self._run_task("timeline", self.json_path)

    def reveal_log(self) -> None:
        LOG_PATH.touch(exist_ok=True)
        subprocess.run(["open", "-R", str(LOG_PATH)], check=False)

    def open_mapping_report(self) -> None:
        if self.timeline_result and self.timeline_result.mapping_path:
            subprocess.run(["open", self.timeline_result.mapping_path], check=False)

    def reveal_report_folder(self) -> None:
        if self.timeline_result and self.timeline_result.mapping_path:
            subprocess.run(["open", "-R", self.timeline_result.mapping_path], check=False)

    def export_validation_csv(self) -> None:
        if not self.timeline_result:
            return
        default_path = Path(self.timeline_result.report_folder) / "ai_edit_segment_validation.csv"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export segment validation CSV",
            str(default_path),
            "CSV files (*.csv)",
        )
        if not path:
            return
        rows = [row.as_csv_row() for row in self.timeline_result.rows]
        fieldnames = list(rows[0].keys()) if rows else []
        with Path(path).expanduser().open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        self._append_action_log("Validation CSV exported: {}".format(Path(path).name))

    def copy_validation_summary(self) -> None:
        if not self.timeline_result:
            return
        QApplication.clipboard().setText(self._validation_summary_text(self.timeline_result))
        self._append_action_log("Segment validation summary copied.")

    def _run_task(self, action: str, json_path: str = "") -> None:
        self._set_busy(True)
        self.thread = QThread()
        self.worker = TaskWorker(action, json_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._task_finished)
        self.worker.failed.connect(self._task_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(lambda: self._set_busy(False))
        self.thread.start()

    def _task_finished(self, action: str, result: object) -> None:
        if isinstance(result, ResolveContext):
            previous_context = self.context
            self.context = result
            self._render_context(previous_context)
            return
        if isinstance(result, JsonValidation):
            self.validation = result
            self._render_validation()
            return
        if isinstance(result, TimelineActionResult):
            self.timeline_result = result
            self.last_action_summary = result.summary
            self._render_segment_review(result)
            self._set_workflow_status("READY_TO_RUN", "Edited timeline created")
            self._append_action_log(
                "Edited timeline created successfully. {} segments ready for review.".format(
                    result.segment_count
                )
            )
            self._render_advanced()
            return
        self.last_action_summary = str(result)
        self._set_workflow_status("READY_TO_RUN", "Ready to run")
        self._append_action_log(
            "Markers added successfully."
            if action == "markers"
            else "Edited timeline created successfully."
        )
        self._render_advanced()

    def _task_failed(self, action: str, message: str, details: str) -> None:
        self.last_traceback = details
        self._set_workflow_status("FAILED", "Failed")
        self._append_action_log("{} failed: {}".format(action.replace("_", " ").title(), message))
        self._render_advanced()

    def _render_context(self, previous_context: Optional[ResolveContext] = None) -> None:
        assert self.context is not None
        safety = "Ready to validate"
        if self.context.errors:
            safety = "Blocked"
        elif self.context.warnings:
            safety = "Ready with warnings"
        self.context_grid.set_rows([
            ("Project", self.context.project_name),
            ("Timeline", self.context.timeline_name),
            ("Timeline FPS", self.context.timeline_fps),
            ("Start timecode", self.context.timeline_start_tc),
            ("Duration", self.context.timeline_duration),
            ("Tracks", "{} video / {} audio".format(
                self.context.video_tracks,
                self.context.audio_tracks,
            )),
            ("Safety", safety),
        ])
        if self.context.errors:
            self._set_workflow_status("BLOCKED", "Blocked")
            self._append_action_log("DaVinci is not ready: {}".format(" ".join(self.context.errors)))
        else:
            self._set_workflow_status(
                "JSON_SELECTED" if self.json_path else "NO_JSON",
                "JSON selected, validation required" if self.json_path else "No JSON selected",
            )
            timeline_changed = bool(
                previous_context
                and previous_context.active_timeline
                and self.context.active_timeline
                and (
                    previous_context.project_name != self.context.project_name
                    or previous_context.timeline_name != self.context.timeline_name
                )
            )
            if timeline_changed:
                message = (
                    "Timeline changed from {} to {}. Please validate again before running actions."
                ).format(previous_context.timeline_name, self.context.timeline_name)
                self.validation_hint.setText(message)
                self._append_action_log(message)
            elif self.json_path and self.context_refresh_reason == "manual":
                message = "Timeline context refreshed. Please validate the selected segments file again."
                self.validation_hint.setText(message)
                self._append_action_log(message)
            elif self.json_path:
                self.validation_hint.setText("Segments file selected. Validate it against the current timeline.")
                self._append_action_log("Segments file selected. Validate it against the current timeline.")
            else:
                self._append_action_log("Choose a segments JSON or Excel file to continue.")
            if self.context.warnings:
                self._append_action_log("Timeline note: {}".format(" ".join(self.context.warnings)))
        self.context_refresh_reason = "idle"
        self._render_advanced()
        self._update_action_state()

    def _render_json_placeholder(self) -> None:
        self.json_grid.set_rows([
            ("File", Path(self.json_path).name),
            ("Detected type", "Validate to inspect"),
            ("Segments", "Validate to inspect"),
            ("Compatibility", "Validate to inspect"),
        ])
        self.validation_hint.setText("Segments file selected. Validate it against the current timeline.")
        self._set_workflow_status("JSON_SELECTED", "JSON selected, validation required")
        self._append_action_log("Segments file selected. Please validate before running an action.")
        self._render_advanced()

    def _render_validation(self) -> None:
        assert self.validation is not None
        v = self.validation
        self.json_grid.set_rows([
            ("File", v.file_name),
            ("Detected type", v.detected_type),
            ("Segments", v.segment_count),
            ("First segment", v.first_segment),
            ("Last segment", v.last_segment),
            ("Covered duration", v.covered_duration),
            ("JSON timeline FPS", v.json_timeline_fps),
            ("JSON source FPS", v.json_source_fps),
            ("Compatibility", v.compatibility),
        ])
        if v.errors:
            state = "BLOCKED" if v.status == "BLOCKED" else "FAILED"
            self._set_workflow_status(state, "Blocked" if state == "BLOCKED" else "Failed")
            summary = self._blocking_validation_summary(v.errors)
            guidance = summary
        elif v.warnings:
            self._set_workflow_status("READY_WITH_WARNINGS", "Ready with warnings")
            summary = "Validation passed with warnings. {} segments ready. Review before running an action.".format(
                v.segment_count
            )
            warning_lines = self._group_validation_warnings(v.warnings)
            if warning_lines:
                summary = "{}\n{}".format(
                    summary,
                    "\n".join("- {}".format(line) for line in warning_lines),
                )
            guidance = (
                "Ready with warnings. {} segments are ready. Review the segment table before running an action."
            ).format(v.segment_count)
        else:
            self._set_workflow_status("READY_TO_RUN", "Ready to run")
            summary = "Validation passed. {} segments ready.".format(v.segment_count)
            guidance = summary
        self.validation_hint.setText(summary)
        self._append_action_log(guidance)
        self._render_advanced()
        self._update_action_state()

    @staticmethod
    def _blocking_validation_summary(errors: list[str]) -> str:
        if not errors:
            return "Validation blocked."
        message = " ".join(errors)
        message = re.sub(r"\bWorksheet row\b", "Row", message)
        return "Blocked: {}".format(message)

    @staticmethod
    def _group_validation_warnings(warnings: list[str]) -> list[str]:
        grouped: list[str] = []
        seen: set[str] = set()
        gap_deltas: list[float] = []
        for warning in warnings:
            text = str(warning).strip()
            if not text:
                continue
            gap_match = re.search(
                r"Worksheet row \d+ starts ([0-9.]+) seconds after the previous segment ends",
                text,
            )
            if gap_match:
                gap_deltas.append(float(gap_match.group(1)))
                continue
            if text == "Temporary Excel compatibility mode: parsed the first worksheet only.":
                display = "Excel compatibility mode: first worksheet only."
            elif text == "JSON has no FPS metadata. Review before running an action.":
                display = "Input file has no FPS metadata."
            else:
                display = text
            if display not in seen:
                grouped.append(display)
                seen.add(display)
        if gap_deltas:
            one_second_gaps = all(abs(delta - 1.0) < 0.001 for delta in gap_deltas)
            if one_second_gaps:
                grouped.append(
                    "{} one-second gaps detected between segments.".format(len(gap_deltas))
                )
            else:
                grouped.append(
                    "{} timing gaps detected between segments.".format(len(gap_deltas))
                )
        return grouped

    def _render_advanced(self) -> None:
        identity = self.bridge.get_backend_identity()
        lines = [
            "Desktop app build: {}".format(APP_BUILD),
            "Backend: {}".format(identity["name"]),
            "Backend modified: {}".format(identity["modified_at"]),
            "Backend path: {}".format(identity["path"]),
            "Desktop bridge: {}".format(identity["bridge_path"]),
            "Startup log: {}".format(identity["startup_log"]),
        ]
        if self.context:
            lines.extend([
                "",
                "Timeline start frame: {}".format(self.context.timeline_start_frame),
                "Raw timeline FPS: {}".format(
                    json.dumps(self.context.fps_info, default=str, indent=2)
                    if self.context.fps_info
                    else "Unavailable"
                ),
            ])
        if self.validation:
            lines.extend([
                "",
                "JSON path: {}".format(self.validation.file_path),
                "Detected shape: {}".format(self.validation.detected_shape),
                "JSON timecode base: {}".format(self.validation.timecode_base),
                "JSON source timecode: {}".format(self.validation.source_timecode),
                "Marker items: {}".format(self.validation.valid_marker_items),
                "Edited timeline segments: {}".format(self.validation.valid_edit_segments),
                "Warnings: {}".format(json.dumps(self.validation.warnings, indent=2)),
                "Errors: {}".format(json.dumps(self.validation.errors, indent=2)),
            ])
        if self.last_action_summary:
            lines.extend(["", "Last backend action report:", self.last_action_summary])
        if self.timeline_result:
            lines.extend([
                "",
                "Post-import mapping: {}".format(self.timeline_result.mapping_path),
                "Generated timeline: {}".format(self.timeline_result.timeline_name),
                "Review rows: {}".format(self.timeline_result.segment_count),
                "Review warnings: {}".format(
                    json.dumps(self.timeline_result.warnings, indent=2)
                ),
            ])
        if self.last_traceback:
            lines.extend(["", "Last traceback:", self.last_traceback])
        self.advanced_text.setPlainText("\n".join(lines))

    def _render_segment_review(self, result: TimelineActionResult) -> None:
        warning_count = sum(row.status == "WARNING" for row in result.rows)
        mismatch_count = sum(row.status == "MISMATCH" for row in result.rows)
        pass_count = sum(row.status == "PASS" for row in result.rows)
        overall = "MISMATCH" if mismatch_count else "WARNING" if warning_count else "PASS"
        self.review_status.setText(overall)
        self.review_status.setProperty("state", overall)
        self.review_status.style().unpolish(self.review_status)
        self.review_status.style().polish(self.review_status)
        self.review_summary.setText(
            "Generated timeline: {}  |  FPS: {}".format(
                result.timeline_name,
                result.fps_label,
            )
        )
        self.review_total_chip.setText("{} SEGMENTS".format(result.segment_count))
        self.review_pass_chip.setText("{} PASS".format(pass_count))
        self.review_warning_chip.setText("{} WARNINGS".format(warning_count))
        self.review_mismatch_chip.setText("{} MISMATCHES".format(mismatch_count))
        self.review_filter = "ALL"
        self._sync_review_filter_buttons()
        self._render_review_rows(result.rows)
        self.review_panel.setVisible(True)

    def _render_review_rows(self, rows: list[Any]) -> None:
        visible_rows = [
            row
            for row in rows
            if self.review_filter == "ALL" or row.status == self.review_filter
        ]
        self.review_table.setRowCount(len(visible_rows))
        for row_index, row in enumerate(visible_rows):
            values = [
                "{:02d}".format(row.segment_number),
                row.title,
                "{} → {}".format(
                    self._source_time_display(row.json_start),
                    self._source_time_display(row.json_end),
                ),
                "{} → {}".format(row.timeline_start, row.timeline_end),
                "{:.3f}s".format(row.timeline_duration_seconds),
                str(row.timeline_duration_frames),
                row.reason_for_cut or "-",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                self.review_table.setItem(row_index, column, item)

    @staticmethod
    def _source_time_display(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return "-"
        try:
            seconds_float = float(text)
        except ValueError:
            seconds_float = -1.0
        if seconds_float >= 0:
            whole_seconds = int(seconds_float)
            milliseconds = int(round((seconds_float - whole_seconds) * 1000))
            if milliseconds == 1000:
                whole_seconds += 1
                milliseconds = 0
            hours = whole_seconds // 3600
            minutes = (whole_seconds % 3600) // 60
            seconds = whole_seconds % 60
            return "{:02d}:{:02d}:{:02d}.{:03d}".format(
                hours,
                minutes,
                seconds,
                milliseconds,
            )

        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d+))?", text)
        if not match:
            return text
        first = int(match.group(1))
        second = int(match.group(2))
        third = match.group(3)
        fraction = (match.group(4) or "0")[:3].ljust(3, "0")
        if third is None:
            hours = 0
            minutes = first
            seconds = second
        else:
            hours = first
            minutes = second
            seconds = int(third)
        return "{:02d}:{:02d}:{:02d}.{}".format(hours, minutes, seconds, fraction)

    def _set_review_filter(self, selected: str) -> None:
        self.review_filter = selected
        self._sync_review_filter_buttons()
        if self.timeline_result:
            self._render_review_rows(self.timeline_result.rows)

    def _sync_review_filter_buttons(self) -> None:
        for filter_name, button in self.review_filter_buttons.items():
            button.setChecked(filter_name == self.review_filter)
            button.style().unpolish(button)
            button.style().polish(button)

    def _clear_segment_review(self) -> None:
        self.timeline_result = None
        if hasattr(self, "review_table"):
            self.review_table.setRowCount(0)
            self.review_panel.setVisible(False)

    @staticmethod
    def _validation_summary_text(result: TimelineActionResult) -> str:
        warning_count = sum(row.status == "WARNING" for row in result.rows)
        mismatch_count = sum(row.status == "MISMATCH" for row in result.rows)
        summary = (
            "Roseberry AI Tools - AI Edit Import\n"
            "Generated timeline: {timeline}\n"
            "Segments created: {segments}\n"
            "Timeline FPS: {fps}\n"
            "PASS: {passed}\n"
            "WARNING: {warnings}\n"
            "MISMATCH: {mismatches}\n"
            "Mapping report: {mapping}"
        ).format(
            timeline=result.timeline_name,
            segments=result.segment_count,
            fps=result.fps_label,
            passed=sum(row.status == "PASS" for row in result.rows),
            warnings=warning_count,
            mismatches=mismatch_count,
            mapping=result.mapping_path,
        )
        review_notes = [
            "Segment {:02d} [{}]: {}".format(
                row.segment_number,
                row.status,
                row.notes or "Review frame alignment.",
            )
            for row in result.rows
            if row.status in ("WARNING", "MISMATCH")
        ]
        if review_notes:
            summary += "\n\nReview notes:\n" + "\n".join(review_notes)
        return summary

    def _update_action_state(self) -> None:
        busy = self.is_busy
        has_json = bool(self.json_path)
        context_ok = bool(self.context and not self.context.errors)
        validation_ok = bool(
            self.validation and self.validation.status in ("PASS", "PASS_WITH_WARNINGS")
        )
        for button in (self.choose_json_button, self.refresh_button):
            button.setEnabled(not busy)
        self.validate_button.setEnabled(not busy and has_json and context_ok)
        self.markers_button.setEnabled(not busy and validation_ok)
        self.timeline_button.setEnabled(
            not busy
            and validation_ok
            and bool(self.context and self.context.safe_to_create_cut_timeline)
        )
        self._update_primary_action()
        self._update_step_indicators(context_ok, has_json, validation_ok)

    def _can_run_mutation(self, require_edit_timeline: bool = False) -> bool:
        if not self.validation or self.validation.status not in ("PASS", "PASS_WITH_WARNINGS"):
            return False
        if not self.context or self.context.errors:
            return False
        if require_edit_timeline:
            return bool(self.context.safe_to_create_cut_timeline)
        return True

    def _update_primary_action(self) -> None:
        self.choose_json_button.setObjectName("SecondaryButton")
        self.validate_button.setObjectName("SecondaryButton")
        self.timeline_button.setObjectName("SecondaryButton")
        if not self.json_path:
            self.choose_json_button.setObjectName("PrimaryButton")
        elif not self.validation or self.validation.status not in ("PASS", "PASS_WITH_WARNINGS"):
            self.validate_button.setObjectName("PrimaryButton")
        elif self.timeline_button.isEnabled():
            self.timeline_button.setObjectName("PrimaryButton")
        for button in (self.choose_json_button, self.validate_button, self.timeline_button):
            button.style().unpolish(button)
            button.style().polish(button)

    def _update_step_indicators(
        self,
        context_ok: bool,
        has_json: bool,
        validation_ok: bool,
    ) -> None:
        if not hasattr(self, "timeline_card"):
            return
        self.timeline_card.set_state("completed" if context_ok else "active")
        self.json_card.set_state(
            "completed" if has_json else "active" if context_ok else "inactive"
        )
        self.validation_card.set_state(
            "completed"
            if validation_ok
            else "active"
            if has_json and context_ok
            else "inactive"
        )
        self.action_card.set_state(
            "completed"
            if self.timeline_result
            else "active"
            if validation_ok
            else "inactive"
        )

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        if busy:
            for button in (
                self.choose_json_button,
                self.validate_button,
                self.markers_button,
                self.timeline_button,
                self.refresh_button,
            ):
                button.setEnabled(False)
        else:
            self._update_action_state()

    def _set_badge(self, text: str) -> None:
        display = {
            "NO_JSON": "NO JSON SELECTED",
            "JSON_SELECTED": "READY TO VALIDATE",
            "VALIDATING": "VALIDATING",
            "READY_TO_RUN": "READY TO RUN",
            "READY_WITH_WARNINGS": "READY WITH WARNINGS",
            "BLOCKED": "BLOCKED",
            "FAILED": "FAILED",
        }.get(text, text.replace("_", " "))
        self.status_badge.setText(display)
        self.status_badge.setProperty("state", text)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

    def _set_workflow_status(self, state: str, text: str) -> None:
        self.workflow_state = state
        self._set_badge(state)
        self.global_status.setText(text)

    def _set_report(self, text: str) -> None:
        self.report.setText(text)

    def _append_action_log(self, text: str) -> None:
        self.action_log_lines.append(text)
        self.action_log_lines = self.action_log_lines[-4:]
        self._set_report("\n".join(self.action_log_lines))

    def _log_selection_state(self) -> None:
        has_timeline_context = bool(
            self.context
            and self.context.active_timeline
            and not self.context.errors
        )
        mutation_actions_enabled = bool(
            self.markers_button.isEnabled() or self.timeline_button.isEnabled()
        )
        last_activity = self.action_log_lines[-1] if self.action_log_lines else ""
        log(
            "JSON selection UI state: has_timeline_context={}; "
            "selected_json_path={}; validation_enabled={}; "
            "validation_state={}; mutation_actions_enabled={}; "
            "last_activity_message={}".format(
                has_timeline_context,
                self.json_path,
                self.validate_button.isEnabled(),
                self.validation.status if self.validation else "NOT_VALIDATED",
                mutation_actions_enabled,
                last_activity,
            )
        )

    def _confirm_timeline_change(self, action: str) -> bool:
        project = self.context.project_name if self.context else "Unavailable"
        timeline = self.context.timeline_name if self.context else "Unavailable"
        selected_json = Path(self.json_path).name if self.json_path else "Unavailable"
        segment_count = self.validation.segment_count if self.validation else "Unavailable"
        warnings = bool(self.validation and self.validation.warnings)
        message = (
            "Confirm this Resolve action:\n\n"
            "Action: {action}\n"
            "Project: {project}\n"
            "Timeline: {timeline}\n"
            "Segments file: {selected_json}\n\n"
            "Segments ready: {segment_count}\n"
            "Warnings: {warnings}\n\n"
            "{warning_note}"
            "Continue?"
        ).format(
            action=action,
            project=project,
            timeline=timeline,
            selected_json=selected_json,
            segment_count=segment_count,
            warnings="Yes" if warnings else "No",
            warning_note=(
                "Review warnings before continuing.\n\n"
                if warnings
                else "Validation passed.\n\n"
            ),
        )
        return QMessageBox.question(
            self,
            "Confirm Resolve Action",
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes


def apply_style(app: QApplication) -> None:
    app.setFont(QFont("Avenir Next", 12))
    app.setStyleSheet(
        """
        QWidget#Root, QWidget#Content {
            background: #050505;
            color: #F4EFE7;
        }
        QScrollArea {
            background: #050505;
            border: none;
        }
        QFrame#HeroPanel {
            background: #0B0A0C;
            border: 1px solid #2A252B;
            border-radius: 8px;
        }
        QLabel#Eyebrow {
            color: #C85B7C;
            font-size: 10px;
            font-weight: 800;
        }
        QLabel#ProductTitle {
            color: #F4EFE7;
            font-size: 31px;
            font-weight: 700;
        }
        QLabel#ModuleTitle {
            color: #F1D08A;
            font-size: 17px;
            font-weight: 650;
        }
        QLabel#Subtitle, QLabel#CardDescription, QLabel#InlineHint {
            color: #B8AEA2;
            font-size: 12px;
        }
        QFrame#StepCard, QFrame#ReportPanel, QFrame#ReviewPanel {
            background: #111013;
            border: 1px solid #2A252B;
            border-radius: 8px;
        }
        QLabel#StepNumber {
            background: #171419;
            color: #B8AEA2;
            border: 1px solid #2A252B;
            border-radius: 14px;
            font-size: 12px;
            font-weight: 800;
        }
        QLabel#StepNumber[state="inactive"] {
            background: #171419;
            color: #B8AEA2;
            border: 1px solid #2A252B;
        }
        QLabel#StepNumber[state="active"] {
            background: #3A2A10;
            color: #F1D08A;
            border: 1px solid #D6A84F;
        }
        QLabel#StepNumber[state="completed"] {
            background: #123326;
            color: #8FE0B6;
            border: 1px solid #2E8B63;
        }
        QLabel#CardTitle {
            color: #F4EFE7;
            font-size: 17px;
            font-weight: 700;
        }
        QLabel#FieldLabel {
            color: #7F766E;
            font-size: 11px;
            font-weight: 650;
        }
        QLabel#FieldValue, QLabel#SelectedFile {
            color: #F4EFE7;
            font-size: 12px;
            font-weight: 550;
        }
        QLabel#ReportText {
            color: #B8AEA2;
            font-size: 12px;
        }
        QLabel#StatusBadge {
            background: #171419;
            color: #D8CBB8;
            border: 1px solid #6C604F;
            border-radius: 15px;
            padding: 7px 14px;
            font-size: 10px;
            font-weight: 800;
        }
        QLabel#StatusBadge[state="PASS"], QLabel#StatusBadge[state="READY"],
        QLabel#StatusBadge[state="DONE"], QLabel#StatusBadge[state="READY_TO_RUN"] {
            background: #123326;
            color: #8FE0B6;
            border-color: #2E8B63;
        }
        QLabel#StatusBadge[state="PASS_WITH_WARNINGS"],
        QLabel#StatusBadge[state="READY_WITH_WARNINGS"],
        QLabel#StatusBadge[state="JSON_SELECTED"], QLabel#StatusBadge[state="VALIDATING"],
        QLabel#StatusBadge[state="NO_JSON"] {
            background: #3A2A10;
            color: #F1D08A;
            border-color: #D6A84F;
        }
        QLabel#GlobalStatus {
            color: #B8AEA2;
            font-size: 11px;
            font-weight: 650;
            padding-top: 5px;
        }
        QLabel#StatusBadge[state="BLOCKED"], QLabel#StatusBadge[state="FAILED"] {
            background: #3A1515;
            color: #F0A0A0;
            border-color: #B94747;
        }
        QLabel#RowStatusBadge {
            background: #123326;
            color: #8FE0B6;
            border: 1px solid #2E8B63;
            border-radius: 12px;
            padding: 5px 10px;
            font-size: 11px;
            font-weight: 800;
        }
        QLabel#RowStatusBadge[state="WARNING"] {
            background: #3A2A10;
            color: #F1D08A;
            border-color: #D6A84F;
        }
        QLabel#RowStatusBadge[state="MISMATCH"] {
            background: #3A1515;
            color: #F0A0A0;
            border-color: #B94747;
        }
        QLabel#SummaryChip {
            background: #171419;
            color: #D8CBB8;
            border: 1px solid #6C604F;
            border-radius: 11px;
            padding: 5px 9px;
            font-size: 10px;
            font-weight: 800;
        }
        QLabel#SummaryChip[state="PASS"] {
            background: #123326;
            color: #8FE0B6;
            border-color: #2E8B63;
        }
        QLabel#SummaryChip[state="WARNING"] {
            background: #3A2A10;
            color: #F1D08A;
            border-color: #D6A84F;
        }
        QLabel#SummaryChip[state="MISMATCH"] {
            background: #3A1515;
            color: #F0A0A0;
            border-color: #B94747;
        }
        QLabel#FilterLabel {
            color: #7F766E;
            font-size: 10px;
            font-weight: 800;
            padding-right: 4px;
        }
        QPushButton {
            background: #171419;
            color: #F4EFE7;
            border: 1px solid #2A252B;
            border-radius: 7px;
            padding: 10px 13px;
            font-size: 12px;
            font-weight: 650;
            text-align: left;
        }
        QPushButton:hover {
            background: #1F1A20;
            border-color: #8C6A2E;
        }
        QPushButton:disabled {
            background: #111013;
            color: #57504B;
            border-color: #1D1A1F;
        }
        QPushButton#PrimaryButton {
            background: #D6A84F;
            color: #090806;
            border: 1px solid #E3BA68;
        }
        QPushButton#PrimaryButton:hover {
            background: #E3BA68;
            border: 1px solid #E3BA68;
        }
        QPushButton#PrimaryButton:pressed {
            background: #A97826;
            border: 1px solid #A97826;
        }
        QPushButton#PrimaryButton:disabled {
            background: #111013;
            color: #57504B;
            border-color: #1D1A1F;
        }
        QPushButton#SecondaryButton {
            background: #171419;
            color: #F4EFE7;
            border: 1px solid #2A252B;
        }
        QPushButton#SecondaryButton:hover {
            background: #1F1A20;
            border-color: #8C6A2E;
        }
        QPushButton#SecondaryButton:disabled {
            background: #111013;
            color: #57504B;
            border-color: #1D1A1F;
        }
        QPushButton#UtilityButton {
            background: transparent;
            color: #B8AEA2;
            border-color: #2A252B;
            padding: 7px 10px;
            font-size: 11px;
        }
        QPushButton#UtilityButton:hover {
            background: #1F1A20;
            color: #F4EFE7;
            border-color: #8C6A2E;
        }
        QPushButton#FilterButton {
            background: transparent;
            color: #B8AEA2;
            border: 1px solid #2A252B;
            border-radius: 11px;
            padding: 4px 10px;
            font-size: 10px;
            font-weight: 750;
        }
        QPushButton#FilterButton:hover {
            background: #1F1A20;
            color: #F4EFE7;
            border-color: #8C6A2E;
        }
        QPushButton#FilterButton:checked {
            background: #3A2A10;
            color: #F1D08A;
            border-color: #D6A84F;
        }
        QTableWidget#ReviewTable {
            background: #0E0D10;
            alternate-background-color: #0D0C0F;
            color: #F4EFE7;
            border: 1px solid #2A252B;
            gridline-color: #2A252B;
            selection-background-color: #1F1A20;
            selection-color: #F4EFE7;
            font-size: 11px;
        }
        QTableWidget#ReviewTable QHeaderView::section {
            background: #171419;
            color: #B8AEA2;
            border: none;
            border-right: 1px solid #2A252B;
            border-bottom: 1px solid #2A252B;
            padding: 9px 7px;
            font-size: 10px;
            font-weight: 700;
        }
        QGroupBox#AdvancedPanel {
            background: #0B0A0C;
            color: #7F766E;
            border: 1px solid #1D1A1F;
            border-radius: 7px;
            margin-top: 8px;
            padding-top: 8px;
            font-size: 12px;
            font-weight: 650;
        }
        QPlainTextEdit#AdvancedText {
            background: #0E0D10;
            color: #B8AEA2;
            border: 1px solid #2A252B;
            border-radius: 6px;
            padding: 8px;
            font-family: Menlo, Monaco, monospace;
            font-size: 11px;
        }
        """
    )


def main() -> int:
    log("Desktop app starting. Build: {}; Backend: {}".format(APP_BUILD, BACKEND_NAME))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    apply_style(app)
    window = RoseberryWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
