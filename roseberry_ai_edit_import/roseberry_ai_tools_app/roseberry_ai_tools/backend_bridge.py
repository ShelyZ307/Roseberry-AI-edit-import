from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .excel_input_adapter import normalize_xlsx_segments


APP_DIR = Path(__file__).resolve().parents[1]
CURRENT_SCRIPTS_DIR = APP_DIR.parent
BACKEND_NAME = "ai_edit_import_utility_timecode_exact_json"
BACKEND_PATH = CURRENT_SCRIPTS_DIR / "{}.py".format(BACKEND_NAME)
RESOLVE_MODULES_PATH = Path(
    os.environ.get(
        "RESOLVE_SCRIPT_API",
        "C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
        if os.name == "nt"
        else "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    )
)


def default_log_path() -> Path:
    override = os.environ.get("ROSEBERRY_AI_EDIT_IMPORT_LOG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Roseberry" / "AI Edit Import" / "logs"
    else:
        root = Path.home() / "Library" / "Logs" / "Roseberry AI Tools"
    return root / "roseberry_ai_tools_desktop_debug.txt"


LOG_PATH = default_log_path()
PREVIEW_MODE = os.environ.get("ROSEBERRY_PREVIEW_MODE") == "1"


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write("[{}] {}\n".format(timestamp, message))
    except Exception:
        pass


def load_backend_module():
    log("Backend path: {}".format(BACKEND_PATH))
    if not BACKEND_PATH.exists():
        raise RuntimeError("Exact-timecode backend not found: {}".format(BACKEND_PATH))
    spec = importlib.util.spec_from_file_location(
        "roseberry_ai_edit_import_exact_json_backend",
        BACKEND_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not create backend module spec: {}".format(BACKEND_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    log("Backend import/load status: succeeded")
    return module


def connect_resolve():
    modules_path = str(RESOLVE_MODULES_PATH)
    if modules_path not in sys.path:
        sys.path.insert(0, modules_path)
    import DaVinciResolveScript as bmd  # type: ignore

    resolve = bmd.scriptapp("Resolve")
    log("Resolve object found: {}".format(bool(resolve)))
    if not resolve:
        raise RuntimeError("Resolve scripting connection was not available.")
    return resolve


@dataclass
class ResolveContext:
    project_name: str = "Unavailable"
    timeline_name: str = "Unavailable"
    timeline_fps: str = "Unavailable"
    timeline_start_tc: str = "Unavailable"
    timeline_start_frame: str = "Unavailable"
    timeline_duration: str = "Unavailable"
    timeline_duration_seconds: Optional[float] = None
    video_tracks: str = "Unavailable"
    audio_tracks: str = "Unavailable"
    active_timeline: bool = False
    safe_to_validate: bool = False
    safe_to_create_cut_timeline: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fps_info: Optional[Dict[str, Any]] = None


@dataclass
class JsonValidation:
    status: str = "BLOCKED"
    file_name: str = ""
    file_path: str = ""
    detected_shape: str = "not loaded"
    detected_type: str = "not loaded"
    segment_count: str = "Unavailable"
    json_timeline_fps: str = "not provided"
    json_source_fps: str = "not provided"
    timecode_base: str = "not provided"
    source_timecode: str = "not provided"
    compatibility: str = "not checked"
    first_segment: str = "Unavailable"
    last_segment: str = "Unavailable"
    covered_duration: str = "Unavailable"
    valid_marker_items: str = "Unavailable"
    valid_edit_segments: str = "Unavailable"
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    payload: Any = None


@dataclass
class SegmentValidationRow:
    segment_number: int
    segment_id: str
    title: str
    json_start: str
    json_end: str
    json_duration_seconds: float
    timeline_start: str
    timeline_end: str
    timeline_duration_seconds: float
    timeline_start_frame: int
    timeline_end_frame_exclusive: int
    timeline_duration_frames: int
    drift_frames: int
    status: str
    reason_for_cut: str
    notes: str

    def as_csv_row(self) -> Dict[str, Any]:
        return {
            "segment_number": self.segment_number,
            "segment_id": self.segment_id,
            "title": self.title,
            "json_start": self.json_start,
            "json_end": self.json_end,
            "json_duration_seconds": "{:.3f}".format(self.json_duration_seconds),
            "timeline_start": self.timeline_start,
            "timeline_end_exclusive": self.timeline_end,
            "timeline_duration_seconds": "{:.3f}".format(
                self.timeline_duration_seconds
            ),
            "timeline_start_frame": self.timeline_start_frame,
            "timeline_end_frame_exclusive": self.timeline_end_frame_exclusive,
            "timeline_duration_frames": self.timeline_duration_frames,
            "drift_frames": self.drift_frames,
            "status": self.status,
            "reason_for_cut": self.reason_for_cut,
            "notes": self.notes,
        }


@dataclass
class TimelineActionResult:
    summary: str
    timeline_name: str
    mapping_path: str
    report_folder: str
    fps_label: str
    segment_count: int
    rows: List[SegmentValidationRow] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class BackendBridge:
    """Desktop-safe adapter around the exact-json backend."""

    def __init__(self) -> None:
        log("BackendBridge init")
        self.backend_module = load_backend_module()
        self.resolve = None

    def get_backend_identity(self) -> Dict[str, str]:
        modified = datetime.fromtimestamp(BACKEND_PATH.stat().st_mtime).astimezone()
        return {
            "name": BACKEND_NAME,
            "path": str(BACKEND_PATH),
            "modified_at": modified.isoformat(timespec="seconds"),
            "bridge_path": str(Path(__file__).resolve()),
            "startup_log": str(LOG_PATH),
        }

    def get_context(self) -> ResolveContext:
        if PREVIEW_MODE:
            return ResolveContext(
                project_name="Roseberry Editorial Demo",
                timeline_name="Episode 04 - Source Timeline",
                timeline_fps="23.976 (24000/1001)",
                timeline_start_tc="01:00:00:00",
                timeline_start_frame="86400",
                timeline_duration="52:18",
                timeline_duration_seconds=3138.0,
                video_tracks="3",
                audio_tracks="6",
                active_timeline=True,
                safe_to_validate=True,
                safe_to_create_cut_timeline=True,
                fps_info=self.backend_module.AIEditImportApp._parse_fps_info("23.976"),
            )

        context = ResolveContext()
        try:
            self.resolve = connect_resolve()
            project_manager = self.resolve.GetProjectManager()
            if not project_manager:
                context.errors.append("Resolve Project Manager is not available.")
                return context
            project = project_manager.GetCurrentProject()
            if not project:
                context.errors.append("No project is currently open in Resolve.")
                return context
            context.project_name = str(project.GetName() or "Untitled Project")
            log("Project found: {}".format(context.project_name))
            timeline = project.GetCurrentTimeline()
            if not timeline:
                context.errors.append("No active timeline is selected.")
                return context

            context.active_timeline = True
            context.timeline_name = str(timeline.GetName() or "Untitled Timeline")
            log("Timeline found: {}".format(context.timeline_name))
            context.fps_info = self.backend_module.AIEditImportApp._get_timeline_fps_info(
                project,
                timeline,
            )
            context.timeline_fps = self.backend_module.AIEditImportApp._format_fps_info(
                context.fps_info
            )
            log("Detected FPS: {}".format(context.timeline_fps))
            start_frame = int(timeline.GetStartFrame() or 0)
            context.timeline_start_frame = str(start_frame)
            if hasattr(timeline, "GetStartTimecode"):
                context.timeline_start_tc = str(timeline.GetStartTimecode() or "Unavailable")
            else:
                context.timeline_start_tc = "Frame {}".format(start_frame)
            log("Detected start timecode: {}".format(context.timeline_start_tc))
            context.timeline_duration = self._safe_duration(timeline, context.fps_info)
            context.timeline_duration_seconds = self._timeline_frame_span_seconds(
                timeline,
                context.fps_info,
            )
            context.video_tracks = str(self._safe_track_count(timeline, "video"))
            context.audio_tracks = str(self._safe_track_count(timeline, "audio"))

            if start_frame != 0:
                context.warnings.append(
                    "Timeline starts after 00:00:00:00. Segment lookup will use the source timeline start safely."
                )
            if context.timeline_name.startswith(self.backend_module.EDITED_TIMELINE_BASE_NAME):
                context.warnings.append(
                    "Open the original source timeline before creating an edited timeline."
                )
            if self._count_items(timeline, "video") == 0:
                context.warnings.append("Current timeline has no usable video items.")

            context.safe_to_validate = not context.errors and context.fps_info is not None
            context.safe_to_create_cut_timeline = (
                context.safe_to_validate
                and not context.timeline_name.startswith(
                    self.backend_module.EDITED_TIMELINE_BASE_NAME
                )
                and self._count_items(timeline, "video") > 0
            )
        except Exception as exc:
            context.errors.append(str(exc))
            log("Context error: {}".format(exc))
            log(traceback.format_exc())
        return context

    def validate_json(self, json_path: str, context: ResolveContext) -> JsonValidation:
        result = JsonValidation(file_path=json_path, file_name=Path(json_path).name)
        log("Selected JSON path: {}".format(json_path))
        try:
            path = Path(json_path).expanduser()
            if not path.exists():
                raise FileNotFoundError("File does not exist: {}".format(path))
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix.lower() == ".xlsx":
                excel_result = normalize_xlsx_segments(
                    path,
                    timeline_duration_seconds=context.timeline_duration_seconds,
                )
                payload = excel_result.payload
                result.warnings.append(
                    "Temporary Excel compatibility mode: parsed the first worksheet only."
                )
                result.warnings.extend(excel_result.warnings)
            else:
                raise ValueError("Selected file must be a .json or .xlsx file.")
            result.payload = payload

            json_type, detected_shape = self.backend_module.detect_loaded_json_type(payload)
            result.detected_type = json_type
            result.detected_shape = detected_shape
            if json_type != "edit":
                result.status = "BLOCKED"
                result.errors.append("Choose an AI Edit Import segments JSON.")
                return result

            _shape_name, items = self.backend_module.extract_moments(payload)
            result.segment_count = str(len(items))
            self._fill_metadata_summary(result, payload)
            self._fill_segment_summary(result, items, context.fps_info)

            inspection = self.backend_module.AIEditImportApp._inspect_json_fps_metadata(
                payload,
                context.fps_info,
            )
            result.json_timeline_fps = self.backend_module.AIEditImportApp._format_fps_info(
                inspection["timeline_fps_info"]
            )
            result.json_source_fps = self.backend_module.AIEditImportApp._format_fps_info(
                inspection["source_fps_info"]
            )
            result.errors.extend(inspection["errors"])
            result.errors.extend(inspection["mismatches"])
            if inspection["source_fps_info"] or inspection["timeline_fps_info"]:
                result.compatibility = "Compatible" if not result.errors else "Mismatch"
            else:
                result.compatibility = "Not checked - JSON metadata missing"
                result.warnings.append("JSON has no FPS metadata. Review before running an action.")

            helper = self._validation_helper()
            fps = float(context.fps_info["float"]) if context.fps_info else None
            marker_entries, marker_skipped, marker_notes = helper._prepare_marker_entries(
                items,
                fps=fps,
            )
            result.valid_marker_items = "{} ready, {} skipped".format(
                len(marker_entries),
                marker_skipped,
            )
            result.warnings.extend(marker_notes[:8])
            if fps is not None:
                cut_entries, cut_skipped, cut_notes = helper._prepare_cut_entries(
                    items,
                    fps=fps,
                    timeline_start_frame=int(context.timeline_start_frame or 0),
                )
                result.valid_edit_segments = "{} ready, {} skipped".format(
                    len(cut_entries),
                    cut_skipped,
                )
                result.warnings.extend(cut_notes[:8])
                if not cut_entries:
                    result.errors.append("No valid edited-timeline segments were found.")

            if context.errors:
                result.status = "BLOCKED"
                result.errors.extend(context.errors)
            elif result.errors:
                result.status = "FAILED"
            elif context.warnings or result.warnings:
                result.status = "PASS_WITH_WARNINGS"
            else:
                result.status = "PASS"
        except Exception as exc:
            result.status = "FAILED"
            result.errors.append(str(exc))
            log("JSON validation error: {}".format(exc))
            log(traceback.format_exc())
        log("Validation result: {}".format(result.status))
        return result

    def run_markers(self, json_path: str) -> str:
        return self._run_backend_action(json_path, "markers")

    def run_create_timeline(self, json_path: str) -> TimelineActionResult:
        summary = self._run_backend_action(json_path, "timeline")
        return self._load_timeline_action_result(json_path, summary)

    def get_report_paths(self, json_path: str = "") -> Dict[str, str]:
        selected = Path(json_path).expanduser() if json_path else Path.home() / "Desktop"
        directory = selected.parent if json_path else selected
        return {
            "startup_log": str(LOG_PATH),
            "append_debug": str(selected.with_suffix(".append_debug.txt")) if json_path else "",
            "audio_debug": str(selected.with_suffix(".audio_debug.txt")) if json_path else "",
            "edit_mapping": str(directory / "edit_mapping.json"),
            "timing_report_csv": str(directory / "ai_edit_timing_report.csv"),
            "timing_report_markdown": str(directory / "ai_edit_timing_report.md"),
            "timing_overlay_srt": str(directory / "ai_edit_timing_overlay.srt"),
            "timing_overlay_vtt": str(directory / "ai_edit_timing_overlay.vtt"),
        }

    def _run_backend_action(self, json_path: str, action: str) -> str:
        app = self.backend_module.AIEditImportApp()
        app.selected_path = json_path
        path = Path(json_path).expanduser()
        if path.suffix.lower() == ".json":
            app.loaded_json = app._load_selected_json()
        elif path.suffix.lower() == ".xlsx":
            app.loaded_json = normalize_xlsx_segments(
                path,
                timeline_duration_seconds=self._timeline_duration_seconds_for_app(app),
            ).payload
        else:
            raise ValueError("Selected file must be a .json or .xlsx file.")
        if action == "markers":
            return app._add_markers_from_loaded_json()
        if action == "timeline":
            return app._build_edited_timeline_from_loaded_json()
        raise RuntimeError("Unsupported backend action: {}".format(action))

    def _timeline_duration_seconds_for_app(self, app: Any) -> Optional[float]:
        try:
            project = app._get_current_project()
            timeline = project.GetCurrentTimeline()
            if not timeline:
                return None
            fps_info = app._get_timeline_fps_info(project, timeline)
            return self._timeline_frame_span_seconds(timeline, fps_info)
        except Exception as exc:
            log("Could not read action timeline duration for Excel End resolution: {}".format(exc))
            return None

    @staticmethod
    def _timeline_frame_span_seconds(
        timeline: Any,
        fps_info: Dict[str, Any],
    ) -> Optional[float]:
        start_frame = int(timeline.GetStartFrame() or 0)
        end_frame = int(timeline.GetEndFrame() or 0)
        fps = float(fps_info["float"])
        if end_frame <= start_frame or fps <= 0:
            return None
        return float(end_frame - start_frame) / fps

    def _load_timeline_action_result(
        self,
        json_path: str,
        summary: str,
    ) -> TimelineActionResult:
        mapping_path = self._find_mapping_path(json_path, summary)
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
        selected_json = str(payload.get("selected_json_path") or "")
        if selected_json and Path(selected_json).expanduser().resolve() != Path(
            json_path
        ).expanduser().resolve():
            raise RuntimeError(
                "Edit mapping belongs to a different JSON file: {}".format(selected_json)
            )

        fps_payload = payload.get("fps") or {}
        fps_raw = fps_payload.get("raw")
        if fps_raw in (None, ""):
            fps_raw = fps_payload.get("value")
        fps_info = self.backend_module.AIEditImportApp._parse_fps_info(fps_raw)
        fps = float(fps_info["float"])
        fps_label = self.backend_module.AIEditImportApp._format_fps_info(fps_info)
        rows = [
            self._build_segment_validation_row(segment, row_number, fps)
            for row_number, segment in enumerate(payload.get("segments") or [], start=1)
        ]
        edited_timeline = payload.get("edited_timeline") or {}
        warnings = [str(value) for value in payload.get("warnings") or []]
        skipped_segments = payload.get("skipped_segments") or []
        if skipped_segments:
            warnings.append(
                "{} JSON segment(s) were skipped during timeline creation.".format(
                    len(skipped_segments)
                )
            )
        log(
            "Loaded post-import mapping: path={}; timeline={}; rows={}; warnings={}".format(
                mapping_path,
                edited_timeline.get("name") or "Unavailable",
                len(rows),
                len(warnings),
            )
        )
        return TimelineActionResult(
            summary=summary,
            timeline_name=str(edited_timeline.get("name") or "Unavailable"),
            mapping_path=str(mapping_path),
            report_folder=str(mapping_path.parent),
            fps_label=fps_label,
            segment_count=len(rows),
            rows=rows,
            warnings=warnings,
        )

    def _find_mapping_path(self, json_path: str, summary: str) -> Path:
        candidates: List[Path] = []
        match = re.search(r"^Edit mapping path:\s*(.+)$", summary, flags=re.MULTILINE)
        if match and match.group(1).strip() != "not written":
            candidates.append(Path(match.group(1).strip()).expanduser())
        candidates.extend(
            [
                Path(json_path).expanduser().parent / "edit_mapping.json",
                Path.home() / "Desktop" / "edit_mapping.json",
            ]
        )
        for path in candidates:
            if path.exists():
                return path
        raise RuntimeError(
            "Edited timeline was created, but edit_mapping.json could not be found."
        )

    def _build_segment_validation_row(
        self,
        segment: Dict[str, Any],
        row_number: int,
        fps: float,
    ) -> SegmentValidationRow:
        json_start_seconds = float(segment.get("source_start_seconds") or 0.0)
        json_end_seconds = float(segment.get("source_end_seconds") or 0.0)
        json_duration_seconds = max(0.0, json_end_seconds - json_start_seconds)
        timeline_duration_frames = int(segment.get("duration_frames") or 0)
        timeline_duration_seconds = (
            float(timeline_duration_frames) / float(fps) if fps else 0.0
        )
        source_start_frame = int(segment.get("source_start_frame") or 0)
        expected_source_end_frame = int(
            segment.get("original_source_end_frame_exclusive")
            or segment.get("source_end_frame_exclusive")
            or source_start_frame
        )
        expected_duration_frames = max(0, expected_source_end_frame - source_start_frame)
        drift_frames = timeline_duration_frames - expected_duration_frames
        notes: List[str] = []
        if drift_frames == 0:
            status = "PASS"
        elif abs(drift_frames) <= 1:
            status = "WARNING"
            notes.append("Timeline duration differs by one frame after frame rounding.")
        else:
            status = "MISMATCH"
            notes.append(
                "Timeline duration differs from the requested source span by {} frames.".format(
                    drift_frames
                )
            )
        if segment.get("was_trimmed"):
            notes.append(str(segment.get("trim_warning") or "Source tail was trimmed."))
            status = "WARNING" if abs(drift_frames) <= 1 else "MISMATCH"
        if segment.get("was_fragmented"):
            notes.append("Built from multiple source clip fragments.")
        seconds_delta = timeline_duration_seconds - json_duration_seconds
        if abs(seconds_delta) > 0.0005:
            notes.append(
                "Frame-boundary duration: {:+.3f}s versus JSON.".format(seconds_delta)
            )
        reason_for_cut = (
            str(segment.get("editorial_reason") or segment.get("qa_notes") or "").strip()
            or "-"
        )
        return SegmentValidationRow(
            segment_number=row_number,
            segment_id=str(segment.get("segment_id") or "Segment {:02d}".format(row_number)),
            title=self._segment_title(segment, row_number),
            json_start=str(segment.get("source_start_value") or ""),
            json_end=str(segment.get("source_end_value") or ""),
            json_duration_seconds=json_duration_seconds,
            timeline_start=str(segment.get("edited_timeline_in") or ""),
            timeline_end=str(segment.get("edited_timeline_out_exclusive") or ""),
            timeline_duration_seconds=timeline_duration_seconds,
            timeline_start_frame=int(segment.get("edited_start_frame") or 0),
            timeline_end_frame_exclusive=int(
                segment.get("edited_end_frame_exclusive") or 0
            ),
            timeline_duration_frames=timeline_duration_frames,
            drift_frames=drift_frames,
            status=status,
            reason_for_cut=reason_for_cut,
            notes=" ".join(notes),
        )

    @staticmethod
    def _segment_title(segment: Dict[str, Any], row_number: int) -> str:
        for key in ("title", "episode_name"):
            value = str(segment.get(key) or "").strip()
            if value:
                return value
        description = str(segment.get("description") or "").strip()
        if description:
            return description if len(description) <= 52 else description[:49].rstrip() + "..."
        return "Segment {:02d}".format(row_number)

    def _validation_helper(self):
        return object.__new__(self.backend_module.AIEditImportApp)

    @staticmethod
    def _safe_duration(timeline: Any, fps_info: Optional[Dict[str, Any]]) -> str:
        try:
            start = int(timeline.GetStartFrame() or 0)
            end = int(timeline.GetEndFrame() or 0)
            if end > start and fps_info:
                seconds = float(end - start) / float(fps_info["float"])
                return "{:02d}:{:02d}:{:02d}".format(
                    int(seconds // 3600),
                    int((seconds % 3600) // 60),
                    int(seconds % 60),
                )
        except Exception:
            pass
        return "Unavailable"

    @staticmethod
    def _safe_track_count(timeline: Any, track_type: str) -> int:
        try:
            return int(timeline.GetTrackCount(track_type) or 0)
        except Exception:
            return 0

    @staticmethod
    def _count_items(timeline: Any, track_type: str) -> int:
        total = 0
        try:
            for track_index in range(1, int(timeline.GetTrackCount(track_type) or 0) + 1):
                total += len(timeline.GetItemListInTrack(track_type, track_index) or [])
        except Exception:
            return 0
        return total

    def _fill_segment_summary(
        self,
        result: JsonValidation,
        items: List[Any],
        fps_info: Optional[Dict[str, Any]],
    ) -> None:
        ranges: List[Tuple[float, float, Any, Any]] = []
        fps = float(fps_info["float"]) if fps_info else None
        helper = self._validation_helper()
        for item in items:
            if not isinstance(item, dict):
                continue
            start_value = helper._first_present_value(item, self.backend_module.START_FIELDS)
            end_value = helper._first_present_value(item, self.backend_module.END_FIELDS)
            duration_value = helper._first_present_value(item, ("duration_seconds",))
            start_info = helper._coerce_json_time_value(start_value, fps=fps)
            end_info = helper._coerce_json_time_value(end_value, fps=fps)
            if not start_info:
                continue
            end_seconds = end_info["seconds"] if end_info else None
            if end_seconds is None and duration_value is not None:
                duration_seconds = helper._coerce_seconds(duration_value, fps=fps)
                if duration_seconds is not None:
                    end_seconds = start_info["seconds"] + duration_seconds
                    end_value = end_seconds
            if end_seconds is not None and end_seconds > start_info["seconds"]:
                ranges.append((start_info["seconds"], end_seconds, start_value, end_value))
        if ranges:
            result.first_segment = "{} -> {}".format(ranges[0][2], ranges[0][3])
            result.last_segment = "{} -> {}".format(ranges[-1][2], ranges[-1][3])
            result.covered_duration = "{:.3f} seconds".format(
                sum(end - start for start, end, _start_value, _end_value in ranges)
            )

    @staticmethod
    def _fill_metadata_summary(result: JsonValidation, payload: Any) -> None:
        candidates: List[Dict[str, Any]] = []
        if isinstance(payload, dict):
            candidates.append(payload)
            if isinstance(payload.get("metadata"), dict):
                candidates.append(payload["metadata"])
            if isinstance(payload.get("data"), dict):
                candidates.append(payload["data"])
                if isinstance(payload["data"].get("metadata"), dict):
                    candidates.append(payload["data"]["metadata"])

        def first_value(*keys: str) -> str:
            for candidate in candidates:
                for key in keys:
                    value = candidate.get(key)
                    if value is not None and str(value).strip():
                        return str(value)
            return "not provided"

        result.timecode_base = first_value("timecode_base", "timecodeBase", "timecode_format")
        result.source_timecode = first_value(
            "source_timecode",
            "source_start_timecode",
            "sourceTimecode",
        )
