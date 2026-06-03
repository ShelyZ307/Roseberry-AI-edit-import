#!/usr/bin/env python3
"""
AI Edit Import for DaVinci Resolve on macOS.

Run from:
Workspace -> Scripts -> Utility -> ai_edit_import

This version intentionally uses one edit workflow only:
source timeline -> new empty output timeline -> MediaPool.AppendToTimeline([{clipInfo}]).

It does not create subclips, media pool folders, black media, image sequences,
titles, generators, or destructive edits on the source timeline.
"""

import csv
import copy
import json
import re
import subprocess
import sys
import traceback
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


WINDOW_ID = "com.openai.ai_edit_import"
WINDOW_TITLE = "AI Edit Import"
PATH_FIELD_ID = "SelectedPath"
BROWSE_BUTTON_ID = "BrowseButton"
LOAD_BUTTON_ID = "LoadButton"
ADD_MARKERS_BUTTON_ID = "AddMarkersButton"
CREATE_EDIT_TIMELINE_BUTTON_ID = "CreateCutTimelineButton"
ADD_SUPERS_MARKERS_BUTTON_ID = "AddSupersMarkersButton"
STATUS_FIELD_ID = "StatusField"

RESOLVE_MODULES_PATH = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
)

MARKER_COLOR = "Yellow"
SEGMENT_RANGE_MARKER_COLOR = "Blue"
MARKER_CUSTOM_PREFIX = "ai_edit_import_moment:"
EDIT_MARKER_CUSTOM_PREFIX = "ai_edit_episode:"
MARKER_NAME_MAX_LENGTH = 72
EDITED_TIMELINE_BASE_NAME = "AI_Edit_Episodes"
GAP_SECONDS = 5.0
RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS = 3600.0
RESOLVE_TIMELINE_LABEL_OFFSET_TEXT = "01:00:00.000"
TIMELINE_LABEL_NORMALIZATION_MESSAGE = (
    "Create Edited Timeline: input timecodes appear to use Resolve 01:00:00 "
    "timeline labels and were normalized to 00:00:00 source-relative time for cutting."
)
COMMON_FPS_RATES: Tuple[Tuple[str, Fraction, Tuple[str, ...]], ...] = (
    ("23.976", Fraction(24000, 1001), ("23.976", "23.98")),
    ("29.97", Fraction(30000, 1001), ("29.97",)),
    ("47.952", Fraction(48000, 1001), ("47.952", "47.95")),
    ("59.94", Fraction(60000, 1001), ("59.94",)),
    ("119.88", Fraction(120000, 1001), ("119.88",)),
)
FPS_PARSE_TOLERANCE = 0.005

START_FIELDS = ("start_time", "start", "in", "in_time", "start_time_seconds")
END_FIELDS = ("end_time", "end", "out", "out_time", "end_time_seconds")
DESCRIPTION_FIELDS = ("description", "summary", "notes", "what_happens", "editor_notes")
MARKER_DESCRIPTION_FIELDS = DESCRIPTION_FIELDS + ("title", "name")
TITLE_FIELDS = ("on_screen_title", "episode_title", "title", "name")
NUMBER_FIELDS = ("episode_number", "segment_number", "segment_id", "index", "id")
EDITORIAL_REASON_FIELDS = ("editorial_reason", "qa_notes", "reason_for_cut", "reason")


def extract_moments(payload: Any) -> Tuple[str, List[Any]]:
    """Extract segment/moment items from supported JSON root shapes."""
    if isinstance(payload, dict):
        moments = payload.get("moments")
        if isinstance(moments, list):
            return "object.moments", moments

        segments = payload.get("segments")
        if isinstance(segments, list):
            return "object.segments", segments

        data = payload.get("data")
        if isinstance(data, dict):
            nested_moments = data.get("moments")
            if isinstance(nested_moments, list):
                return "object.data.moments", nested_moments

            nested_segments = data.get("segments")
            if isinstance(nested_segments, list):
                return "object.data.segments", nested_segments

        raise ValueError(
            "Unsupported JSON shape. Expected object.moments, object.segments, "
            "object.data.moments, object.data.segments, or a root array."
        )

    if isinstance(payload, list):
        return "root array", payload

    raise ValueError("Unsupported JSON root type. Expected an object or array.")


def extract_supers(payload: Any) -> Tuple[str, List[Any]]:
    """Extract supers items from the supported Supers JSON root shape."""
    if isinstance(payload, dict) and isinstance(payload.get("supers"), list):
        return "object.supers", payload["supers"]
    raise ValueError("Unsupported Supers JSON shape. Expected object.supers.")


def detect_loaded_json_type(payload: Any) -> Tuple[str, str]:
    """Route loaded JSON to the correct action family without changing legacy extraction."""
    if isinstance(payload, dict):
        if isinstance(payload.get("moments"), list):
            return "edit", "object.moments"
        if isinstance(payload.get("segments"), list):
            return "edit", "object.segments"

        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("moments"), list):
                return "edit", "object.data.moments"
            if isinstance(data.get("segments"), list):
                return "edit", "object.data.segments"

        if isinstance(payload.get("supers"), list):
            return "supers", "object.supers"

        return "unsupported", "unsupported"

    if isinstance(payload, list):
        return "edit", "root array"

    return "unsupported", "unsupported"


def json_seconds_to_source_timeline_frame(
    seconds: float,
    fps: float,
    timeline_start_frame: int,
    conversion_method: str = "numeric-seconds",
) -> int:
    """Map 0-based JSON time values to absolute source timeline frames."""
    return int(timeline_start_frame) + json_seconds_to_timeline_offset(
        seconds,
        fps,
        conversion_method=conversion_method,
    )


def json_seconds_to_timeline_offset(
    seconds: float,
    fps: float,
    conversion_method: str = "numeric-seconds",
) -> int:
    """Map 0-based JSON time values to timeline-relative frames."""
    # JSON string timestamps like HH:MM:SS.mmm are Resolve/display-timecode-style values.
    # Numeric timestamps remain elapsed seconds for backward compatibility.
    if conversion_method == "timecode-string":
        effective_fps = AIEditImportApp._nominal_timecode_fps_from_fps(fps)
    else:
        effective_fps = float(fps)
    return int((float(seconds) * float(effective_fps)) + 0.5)


class AIEditImportApp:
    """Resolve UI app for JSON validation, marker import, and clean edit assembly."""

    def __init__(self) -> None:
        self.bmd = self._import_resolve_module()
        self.resolve = self._connect_resolve()
        self.fusion = self.resolve.Fusion()
        self.ui = getattr(self.fusion, "UIManager", None)
        if not self.ui:
            raise RuntimeError("Resolve UIManager is not available in this environment.")

        self.dispatcher = self.bmd.UIDispatcher(self.ui)
        self.window = None
        self.selected_path = ""
        self.loaded_json: Any = None

    def run(self) -> None:
        existing = self.ui.FindWindow(WINDOW_ID)
        if existing:
            existing.Show()
            existing.Raise()
            return

        self.window = self._build_window()
        self._bind_events()
        self._set_path_text("")
        self._set_status("Ready. Click Browse to choose a JSON file.")

        self.window.Show()
        self.dispatcher.RunLoop()
        self.window.Hide()

    def _import_resolve_module(self):
        modules_path = str(RESOLVE_MODULES_PATH)
        if modules_path not in sys.path:
            sys.path.insert(0, modules_path)

        try:
            import DaVinciResolveScript as bmd  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                f"Could not import DaVinciResolveScript from {RESOLVE_MODULES_PATH}: {exc}"
            ) from exc

        return bmd

    def _connect_resolve(self):
        try:
            resolve = self.bmd.scriptapp("Resolve")
        except Exception as exc:
            raise RuntimeError(f"Resolve connection failed: {exc}") from exc

        if not resolve:
            raise RuntimeError(
                "Resolve returned no scripting connection. Run this from "
                "Workspace -> Scripts -> Utility."
            )
        return resolve

    def _build_window(self):
        status_widget = self.ui.TextEdit(
            {"ID": STATUS_FIELD_ID, "ReadOnly": True, "AcceptRichText": False, "Weight": 1}
        )

        return self.dispatcher.AddWindow(
            {
                "ID": WINDOW_ID,
                "WindowTitle": WINDOW_TITLE,
                "Geometry": [100, 100, 780, 280],
            },
            self.ui.VGroup(
                [
                    self.ui.Label(
                        {
                            "Text": "Select a JSON file, validate it, then add markers or create an edited timeline.",
                            "Weight": 0,
                        }
                    ),
                    self.ui.HGroup(
                        {"Weight": 0},
                        [
                            self._make_path_widget(),
                            self.ui.Button(
                                {"ID": BROWSE_BUTTON_ID, "Text": "Browse", "Weight": 0}
                            ),
                        ],
                    ),
                    self.ui.HGroup(
                        {"Weight": 0},
                        [
                            self.ui.Button(
                                {
                                    "ID": LOAD_BUTTON_ID,
                                    "Text": "Load JSON",
                                    "Default": True,
                                    "Weight": 0,
                                }
                            ),
                            self.ui.Button(
                                {
                                    "ID": ADD_MARKERS_BUTTON_ID,
                                    "Text": "Add Markers Only",
                                    "Weight": 0,
                                }
                            ),
                            self.ui.Button(
                                {
                                    "ID": CREATE_EDIT_TIMELINE_BUTTON_ID,
                                    "Text": "Create Edited Timeline",
                                    "Weight": 0,
                                }
                            ),
                            self.ui.Button(
                                {
                                    "ID": ADD_SUPERS_MARKERS_BUTTON_ID,
                                    "Text": "Add Supers Markers",
                                    "Weight": 0,
                                }
                            ),
                        ],
                    ),
                    self.ui.Label({"Text": "Status", "Weight": 0}),
                    status_widget,
                ]
            ),
        )

    def _make_path_widget(self):
        if hasattr(self.ui, "LineEdit"):
            return self.ui.LineEdit({"ID": PATH_FIELD_ID, "ReadOnly": True, "Weight": 1})
        return self.ui.TextEdit(
            {"ID": PATH_FIELD_ID, "ReadOnly": True, "AcceptRichText": False, "Weight": 1}
        )

    def _bind_events(self) -> None:
        assert self.window is not None
        self.window.On[WINDOW_ID].Close = self._on_close
        self.window.On[BROWSE_BUTTON_ID].Clicked = self._on_browse_clicked
        self.window.On[LOAD_BUTTON_ID].Clicked = self._on_load_clicked
        self.window.On[ADD_MARKERS_BUTTON_ID].Clicked = self._on_add_markers_clicked
        self.window.On[CREATE_EDIT_TIMELINE_BUTTON_ID].Clicked = (
            self._on_create_edited_timeline_clicked
        )
        self.window.On[ADD_SUPERS_MARKERS_BUTTON_ID].Clicked = (
            self._on_add_supers_markers_clicked
        )

    def _on_close(self, _event) -> None:
        self.dispatcher.ExitLoop()

    def _on_browse_clicked(self, _event) -> None:
        try:
            chosen_path = self._choose_json_file()
        except Exception as exc:
            err_str = str(exc)
            if "'ascii'" in err_str and "codec" in err_str:
                self._set_status(
                    f"Could not open file picker:\n\n{exc}\n\n"
                    "HINT: The file path contains non-ASCII characters (e.g. em dash —).\n"
                    "This was an encoding issue in the file picker — now fixed.\n"
                    "If this error persists, rename the file to use only ASCII characters."
                )
            else:
                self._set_status(f"Could not open file picker:\n\n{exc}")
            print(f"[AI Edit Import] File picker error: {exc}")
            return

        if not chosen_path:
            self._set_status("Browse cancelled.")
            return

        self.selected_path = chosen_path
        self._set_path_text(chosen_path)
        self._set_status(f"Selected file:\n{chosen_path}\n\nClick 'Load JSON'.")

    def _on_load_clicked(self, _event) -> None:
        try:
            payload = self._load_selected_json()
        except Exception as exc:
            self.loaded_json = None
            self._set_status(f"Error loading JSON:\n\n{exc}")
            print(f"[AI Edit Import] Load error: {exc}")
            return

        self.loaded_json = payload
        summary = self._build_json_summary(payload)
        self._set_status(summary)
        print("[AI Edit Import] JSON loaded successfully")
        print(summary)

    def _on_add_markers_clicked(self, _event) -> None:
        if self._loaded_json_is_supers():
            self._set_status("This JSON was detected as Supers. Use Add Supers Markers.")
            return

        try:
            summary = self._add_markers_from_loaded_json()
        except Exception as exc:
            self._set_status(f"Error adding markers:\n\n{exc}")
            print(f"[AI Edit Import] Marker error: {exc}")
            return

        self._set_status(summary)
        print("[AI Edit Import] Markers complete")
        print(summary)

    def _on_create_edited_timeline_clicked(self, _event) -> None:
        if self._loaded_json_is_supers():
            self._set_status("This JSON was detected as Supers. Use Add Supers Markers.")
            return

        try:
            summary = self._build_edited_timeline_from_loaded_json()
        except Exception as exc:
            self._set_status(f"Error creating edited timeline:\n\n{exc}")
            print(f"[AI Edit Import] Edited timeline error: {exc}")
            return

        self._set_status(summary)
        print("[AI Edit Import] Edited timeline complete")
        print(summary)

    def _on_add_supers_markers_clicked(self, _event) -> None:
        if self.loaded_json is None:
            self._set_status("No JSON is loaded yet. Click 'Load JSON' first.")
            return

        json_type, _shape_name = detect_loaded_json_type(self.loaded_json)
        if json_type == "edit":
            self._set_status(
                "This JSON was detected as Edit Moments. Use Add Markers Only or Create Edited Timeline."
            )
            return
        if json_type == "unsupported":
            self._set_status(
                "Detected JSON type: Unsupported\n"
                "Supported types:\n"
                "- Edit Moments: moments, segments, data.moments, data.segments, root array\n"
                "- Supers: supers"
            )
            return

        self._set_status("Supers markers are not implemented yet.")
        print("[AI Edit Import] Supers markers are not implemented yet.")

    def _choose_json_file(self) -> Optional[str]:
        apple_script = 'POSIX path of (choose file with prompt "Select a JSON file")'
        result = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0:
            selected = (result.stdout or "").strip()
            if selected:
                import unicodedata
                selected = unicodedata.normalize("NFC", selected)
            return selected or None

        stderr = (result.stderr or "").strip()
        if "User canceled" in stderr or "(-128)" in stderr:
            return None

        return self._fallback_path_prompt(
            f"Native picker failed ({stderr or 'unknown error'}). Enter a JSON path:"
        )

    def _fallback_path_prompt(self, prompt_text: str) -> Optional[str]:
        try:
            response = self.fusion.AskUser(
                WINDOW_TITLE,
                {
                    1: {
                        "ID": "json_path",
                        "Name": prompt_text,
                        "Type": "Text",
                        "Default": self.selected_path or str(Path.home()),
                    }
                },
            )
        except Exception as exc:
            raise RuntimeError(f"Fallback path prompt failed: {exc}") from exc

        if not response:
            return None
        value = str(response.get("json_path") or "").strip()
        return value or None

    def _load_selected_json(self) -> Any:
        raw_path = (self.selected_path or "").strip()
        if not raw_path:
            raise ValueError("No file has been selected yet.")

        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"Selected path is not a file: {path}")
        if path.suffix.lower() != ".json":
            raise ValueError(f"Selected file is not a .json file: {path.name}")

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Could not read file as UTF-8 text: {exc}") from exc
        except OSError as exc:
            raise OSError(f"Could not read file: {exc}") from exc

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc

    def _loaded_json_is_supers(self) -> bool:
        if self.loaded_json is None:
            return False
        json_type, _shape_name = detect_loaded_json_type(self.loaded_json)
        return json_type == "supers"

    def _build_json_summary(self, payload: Any) -> str:
        lines = ["JSON loaded successfully."]
        if isinstance(payload, dict):
            keys = sorted(payload.keys())
            lines.append("Root type: object")
            lines.append("Top-level keys: {}".format(", ".join(keys) if keys else "(none)"))
        elif isinstance(payload, list):
            lines.append("Root type: array")
            lines.append(f"Top-level item count: {len(payload)}")
        else:
            lines.append(f"Root type: {type(payload).__name__}")

        json_type, detected_shape = detect_loaded_json_type(payload)
        if json_type == "supers":
            _shape_name, supers_items = extract_supers(payload)
            lines.append("Detected JSON type: Supers")
            lines.append(f"Detected JSON shape: {detected_shape}")
            lines.append(f"Supers found: {len(supers_items)}")
            lines.append("Valid supers: validation not implemented yet")
            lines.append("Skipped supers: validation not implemented yet")
            return "\n".join(lines)

        if json_type == "unsupported":
            lines.append("Detected JSON type: Unsupported")
            lines.append("Supported types:")
            lines.append("- Edit Moments: moments, segments, data.moments, data.segments, root array")
            lines.append("- Supers: supers")
            return "\n".join(lines)

        lines.append("Detected JSON type: Edit Moments")
        lines.append(f"Detected JSON shape: {detected_shape}")

        try:
            project = self._get_current_project()
            timeline = project.GetCurrentTimeline()
            fps = None
            fps_info = None
            timeline_start_frame = 0
            if timeline:
                fps_info = self._get_timeline_fps_info(project, timeline)
                fps = float(fps_info["float"])
                timeline_start_frame = int(timeline.GetStartFrame() or 0)
            lines.extend(self._fps_status_lines(payload, fps_info))
            if fps_info:
                lines.append(
                    "JSON timecode nominal FPS: {}".format(
                        self._nominal_timecode_fps_from_fps_info(fps_info)
                    )
                )

            shape_name, source_items = extract_moments(payload)
            marker_entries, marker_skipped, _ = self._prepare_marker_entries(
                source_items,
                fps=fps,
            )
            lines.append(f"Source items found: {len(source_items)}")
            lines.append(f"Valid marker items: {len(marker_entries)}")
            lines.append(f"Marker items that would be skipped: {marker_skipped}")
            if fps is None:
                lines.append("Valid edit segments: not checked (no active timeline FPS)")
                lines.append("Edit segments that would be skipped: not checked")
            else:
                cut_entries, cut_skipped, _ = self._prepare_cut_entries(
                    source_items,
                    fps=fps,
                    timeline_start_frame=timeline_start_frame,
                )
                lines.append(f"Valid edit segments: {len(cut_entries)}")
                lines.append(f"Edit segments that would be skipped: {cut_skipped}")
            warning = self._timeline_start_warning(timeline_start_frame)
            if warning:
                lines.append(warning)
        except Exception as exc:
            lines.append(f"JSON shape/detail check failed: {exc}")

        return "\n".join(lines)

    def _build_edited_timeline_from_loaded_json(self) -> str:
        if self.loaded_json is None:
            raise ValueError("No JSON is loaded yet. Click 'Load JSON' first.")

        shape_name, source_items = extract_moments(self.loaded_json)
        if not source_items:
            raise ValueError(f"Detected JSON shape '{shape_name}' but it contains no items.")

        project = self._get_current_project()
        source_timeline = project.GetCurrentTimeline()
        if not source_timeline:
            raise RuntimeError("No active source timeline is open in Resolve.")

        source_timeline_name = str(source_timeline.GetName() or "")
        source_video_item_count = self._count_timeline_items(source_timeline, "video")
        source_audio_item_count = self._count_timeline_items(source_timeline, "audio")
        if source_timeline_name.startswith(EDITED_TIMELINE_BASE_NAME) or source_video_item_count == 0:
            raise RuntimeError(
                "Please open the original source timeline, not AI_Edit_Episodes, before running Create Edited Timeline.\n\n"
                "Source timeline name: {}\n"
                "Source video item count: {}\n"
                "Source audio item count: {}".format(
                    source_timeline_name,
                    source_video_item_count,
                    source_audio_item_count,
                )
            )

        fps_info = self._get_timeline_fps_info(project, source_timeline)
        fps = float(fps_info["float"])
        fps_raw = fps_info["raw"]
        nominal_timecode_fps = self._nominal_timecode_fps_from_fps_info(fps_info)
        fps_inspection = self._validate_json_fps_or_raise(self.loaded_json, fps_info)
        source_start_frame = int(source_timeline.GetStartFrame() or 0)
        source_end_frame = int(source_timeline.GetEndFrame() or 0)
        source_duration_seconds = None
        if source_end_frame > source_start_frame:
            source_duration_seconds = float(source_end_frame - source_start_frame) / fps
        source_start_timecode = ""
        try:
            if hasattr(source_timeline, "GetStartTimecode"):
                source_start_timecode = str(source_timeline.GetStartTimecode() or "")
        except Exception:
            source_start_timecode = ""
        gap_frames = int(round(GAP_SECONDS * fps))
        debug_lines: List[str] = [
            "AI Edit Import append debug report",
            "Selected JSON: {}".format(self.selected_path or "(none)"),
            "Source timeline name: {}".format(source_timeline_name),
            "Source video item count: {}".format(source_video_item_count),
            "Source audio item count: {}".format(source_audio_item_count),
            "Source timeline start frame: {}".format(source_start_frame),
            "Source timeline start timecode: {}".format(source_start_timecode or "Unavailable"),
            "Source timeline duration seconds: {}".format(
                "{:.3f}".format(source_duration_seconds)
                if source_duration_seconds is not None
                else "Unavailable"
            ),
            "Source timeline FPS: {}".format(fps_raw),
            "JSON timecode nominal FPS: {}".format(nominal_timecode_fps),
            "Gap frames requested: {}".format(gap_frames),
            "",
            "FPS validation",
            "==============",
            *self._fps_status_lines(self.loaded_json, fps_info, fps_inspection),
            "",
        ]
        debug_report_path = self._debug_report_path()
        audio_debug_report_path = self._audio_debug_report_path()
        audio_debug_lines: List[str] = [
            "AI Edit Import audio debug report",
            "Selected JSON: {}".format(self.selected_path or "(none)"),
            "Source timeline name: {}".format(source_timeline_name),
            "Source video item count: {}".format(source_video_item_count),
            "Source audio item count: {}".format(source_audio_item_count),
            "Source audio track count: {}".format(
                self._safe_track_count(source_timeline, "audio")
            ),
            "Source timeline start frame: {}".format(source_start_frame),
            "Source timeline start timecode: {}".format(source_start_timecode or "Unavailable"),
            "Source timeline duration seconds: {}".format(
                "{:.3f}".format(source_duration_seconds)
                if source_duration_seconds is not None
                else "Unavailable"
            ),
            "Source timeline FPS: {}".format(fps_raw),
            "JSON timecode nominal FPS: {}".format(nominal_timecode_fps),
            "",
            "FPS validation",
            "==============",
            *self._fps_status_lines(self.loaded_json, fps_info, fps_inspection),
            "",
        ]

        cut_source_items, timecode_normalization = (
            self._normalize_timeline_label_times_for_cutting(
                source_items,
                payload=self.loaded_json,
                fps=fps,
                source_timeline_start_frame=source_start_frame,
                source_timeline_start_timecode=source_start_timecode,
                source_timeline_duration_seconds=source_duration_seconds,
            )
        )
        normalization_debug_lines = [
            "",
            "Create Edited Timeline timecode normalization",
            "=============================================",
            "Normalization applied: {}".format(timecode_normalization.get("applied")),
            "Offset subtracted: {}".format(
                timecode_normalization.get("offset_subtracted") or ""
            ),
            "Earliest original seconds: {}".format(
                timecode_normalization.get("earliest_original_seconds")
            ),
            "Earliest normalized seconds: {}".format(
                timecode_normalization.get("earliest_normalized_seconds")
            ),
            "Max original end seconds: {}".format(
                timecode_normalization.get("max_original_end_seconds")
            ),
            "Max normalized end seconds: {}".format(
                timecode_normalization.get("max_normalized_end_seconds")
            ),
            "Reason: {}".format(timecode_normalization.get("reason") or ""),
        ]
        if timecode_normalization.get("message"):
            normalization_debug_lines.append(
                "Message: {}".format(timecode_normalization["message"])
            )
        for normalization_warning in timecode_normalization.get("warnings") or []:
            normalization_debug_lines.append(
                "Normalization warning: {}".format(normalization_warning)
            )
        debug_lines.extend(normalization_debug_lines)
        audio_debug_lines.extend(normalization_debug_lines)

        cut_entries, skipped_count, errors = self._prepare_cut_entries(
            cut_source_items,
            fps=fps,
            timeline_start_frame=source_start_frame,
        )
        errors.extend(str(warning) for warning in timecode_normalization.get("warnings") or [])
        if not cut_entries:
            raise ValueError(
                f"Detected JSON shape '{shape_name}', but no valid edit segments were found."
            )

        video_tracks = self._collect_video_track_items(source_timeline)
        resolved_entries, resolve_warnings = self._resolve_cut_fragments_safely(
            cut_entries,
            video_tracks,
        )
        errors.extend(resolve_warnings)
        skipped_segment_errors = list(errors)
        if not resolved_entries:
            raise RuntimeError(
                "No valid JSON segments were fully covered by source clips on the active timeline."
            )

        planned_fragments, planned_segments = self._build_append_plan(
            resolved_entries,
            gap_frames,
            debug_lines,
        )
        if not planned_fragments:
            self._write_debug_report(debug_report_path, debug_lines)
            raise RuntimeError(
                "No appendable clipInfo entries were prepared. Debug file saved here:\n{}".format(
                    debug_report_path
                )
            )

        destination_name = self._unique_timeline_name(project, EDITED_TIMELINE_BASE_NAME)
        destination_timeline, placed_segments, append_warnings, audio_stats = (
            self._create_clean_append_timeline(
                project,
                destination_name,
                planned_fragments,
                planned_segments,
                debug_lines,
                debug_report_path,
                audio_debug_lines,
                audio_debug_report_path,
            )
        )
        errors.extend(append_warnings)

        range_markers_added, marker_warnings = (
            self._add_episode_markers_to_edited_timeline(
                destination_timeline,
                placed_segments,
                fps,
            )
        )
        errors.extend(marker_warnings)
        debug_lines.extend(
            [
                "",
                "Timeline marker timing labels",
                "=============================",
                "Segment range markers added: {}".format(range_markers_added),
                "Marker DVR times show source timeline timing.",
                "Edited timeline positions include gaps; marker DVR times show source timing.",
            ]
        )
        for marker_warning in marker_warnings:
            debug_lines.append("Marker warning: {}".format(marker_warning))

        video_item_count = self._count_timeline_items(destination_timeline, "video")
        audio_item_count = self._count_timeline_items(destination_timeline, "audio")
        if video_item_count == 0:
            raise RuntimeError(
                "Resolve created the output timeline, but it contains zero video items. "
                "No subclips, folders, or black media were created."
            )

        mapping_payload = self._build_edit_mapping_payload(
            source_timeline_name=source_timeline_name,
            source_start_frame=source_start_frame,
            source_video_item_count=source_video_item_count,
            source_audio_item_count=source_audio_item_count,
            destination_name=destination_name,
            destination_timeline=destination_timeline,
            video_item_count=video_item_count,
            audio_item_count=audio_item_count,
            fps=fps,
            fps_raw=fps_raw,
            gap_frames=gap_frames,
            placed_segments=placed_segments,
            planned_fragments=planned_fragments,
            skipped_errors=skipped_segment_errors,
            timecode_normalization=timecode_normalization,
        )
        timing_report_result = self._write_timing_report_files(mapping_payload, fps)
        mapping_payload["timing_reports"] = {
            "csv_path": timing_report_result.get("csv_path") or "",
            "markdown_path": timing_report_result.get("markdown_path") or "",
            "warnings": timing_report_result.get("warnings", []),
        }
        timing_overlay_result = self._write_timing_overlay_files(mapping_payload, fps)
        mapping_payload["timing_overlay"] = {
            "srt_path": timing_overlay_result.get("srt_path") or "",
            "vtt_path": timing_overlay_result.get("vtt_path") or "",
            "cue_count": timing_overlay_result.get("cue_count", 0),
            "warnings": timing_overlay_result.get("warnings", []),
            "import_note": "Import ai_edit_timing_overlay.srt into DaVinci as a subtitle track.",
        }
        mapping_result = self._write_edit_mapping_files(mapping_payload)
        debug_lines.extend(
            [
                "",
                "Timing report export",
                "====================",
                "Timing report CSV path: {}".format(
                    timing_report_result.get("csv_path") or "not written"
                ),
                "Timing report Markdown path: {}".format(
                    timing_report_result.get("markdown_path") or "not written"
                ),
                "Timing overlay SRT path: {}".format(
                    timing_overlay_result.get("srt_path") or "not written"
                ),
                "Timing overlay VTT path: {}".format(
                    timing_overlay_result.get("vtt_path") or "not written"
                ),
                "Timing overlay cues: {}".format(
                    timing_overlay_result.get("cue_count", 0)
                ),
                "",
                "Edit mapping export",
                "===================",
                "Edit mapping path: {}".format(mapping_result.get("stable_path") or "not written"),
                "Edit mapping timestamped path: {}".format(
                    mapping_result.get("timestamped_path") or "not written"
                ),
                "Mapped segments: {}".format(mapping_result.get("mapped_segments", 0)),
                "Mapped fragments: {}".format(mapping_result.get("mapped_fragments", 0)),
            ]
        )
        for mapping_warning in mapping_result.get("warnings", []):
            debug_lines.append("Mapping warning: {}".format(mapping_warning))
        for report_warning in timing_report_result.get("warnings", []):
            debug_lines.append("Timing report warning: {}".format(report_warning))
        for overlay_warning in timing_overlay_result.get("warnings", []):
            debug_lines.append("Timing overlay warning: {}".format(overlay_warning))
        self._write_debug_report(debug_report_path, debug_lines)

        lines = [
            "Edited timeline created.",
            f"Detected JSON shape: {shape_name}",
            f"Source timeline: {source_timeline_name}",
            f"Output timeline: {destination_name}",
            f"Timeline FPS: {fps_raw}",
            f"JSON timecode nominal FPS: {nominal_timecode_fps}",
            "Resolve FPS canonical: {}".format(self._format_fps_info(fps_info)),
            "JSON timeline FPS: {}".format(
                self._format_fps_info(fps_inspection["timeline_fps_info"])
            ),
            "JSON source FPS: {}".format(
                self._format_fps_info(fps_inspection["source_fps_info"])
            ),
            "FPS match: {}".format(
                "yes"
                if (
                    fps_inspection["source_fps_info"]
                    or fps_inspection["timeline_fps_info"]
                )
                else "not checked (JSON has no FPS metadata)"
            ),
            f"Source timeline start frame: {source_start_frame}",
            f"Source video item count: {source_video_item_count}",
            f"Source audio item count: {source_audio_item_count}",
            f"JSON items found: {len(source_items)}",
            f"Valid segments: {len(cut_entries)}",
            f"Segments added: {len(placed_segments)}",
            "Gap duration: {} seconds ({} frames)".format(GAP_SECONDS, gap_frames),
            "Gap method: recordFrame empty gap request",
            "Create Edited Timeline timecode normalization: {}".format(
                "applied" if timecode_normalization.get("applied") else "not applied"
            ),
            f"Output video items: {video_item_count}",
            f"Output audio items: {audio_item_count}",
            "Audio append attempts: {}".format(audio_stats["attempts"]),
            "Audio append successes: {}".format(audio_stats["successes"]),
            "Audio append failures: {}".format(audio_stats["failures"]),
            f"Segment range markers added: {range_markers_added}",
            "Marker DVR times show source timeline timing.",
            "Edited timeline positions include gaps; marker DVR times show source timing.",
            f"Skipped items: {skipped_count}",
            "Subclips created: 0",
            "Media pool folders created: 0",
            "Black media clips created: 0",
            "Visible titles created: no",
            "Timing report CSV path: {}".format(
                timing_report_result.get("csv_path") or "not written"
            ),
            "Timing report Markdown path: {}".format(
                timing_report_result.get("markdown_path") or "not written"
            ),
            "Timing overlay SRT path: {}".format(
                timing_overlay_result.get("srt_path") or "not written"
            ),
            "Timing overlay VTT path: {}".format(
                timing_overlay_result.get("vtt_path") or "not written"
            ),
            "Timing overlay cues: {}".format(timing_overlay_result.get("cue_count", 0)),
            "Import timing overlay: import ai_edit_timing_overlay.srt into DaVinci as a subtitle track.",
            "Edit mapping path: {}".format(mapping_result.get("stable_path") or "not written"),
            "Edit mapping timestamped path: {}".format(
                mapping_result.get("timestamped_path") or "not written"
            ),
            "Mapped segments: {}".format(mapping_result.get("mapped_segments", 0)),
            "Mapped fragments: {}".format(mapping_result.get("mapped_fragments", 0)),
            "Mapping warnings: {}".format(len(mapping_result.get("warnings", []))),
            f"Append debug report: {debug_report_path}",
            f"Audio debug report: {audio_debug_report_path}",
        ]

        warning = self._timeline_start_warning(source_start_frame)
        if warning:
            lines.append(warning)
        if timecode_normalization.get("message"):
            lines.append(str(timecode_normalization["message"]))
        if timecode_normalization.get("warnings"):
            lines.append("Timecode normalization warnings:")
            lines.extend(str(warning) for warning in timecode_normalization["warnings"])
        if audio_item_count == 0:
            lines.append(
                "Warning: Video was created, but no audio items were added. This likely means the source MediaPoolItem does not expose embedded audio through mediaType 2, or Resolve rejected the audio AppendToTimeline clipInfo."
            )
        if mapping_result.get("warnings"):
            lines.append("Mapping warnings:")
            lines.extend(str(warning) for warning in mapping_result["warnings"])
        if timing_report_result.get("warnings"):
            lines.append("Timing report warnings:")
            lines.extend(str(warning) for warning in timing_report_result["warnings"])
        if timing_overlay_result.get("warnings"):
            lines.append("Timing overlay warnings:")
            lines.extend(str(warning) for warning in timing_overlay_result["warnings"])
        if errors:
            lines.append("Warnings/errors:")
            lines.extend(errors[:8])
            if len(errors) > 8:
                lines.append(f"...and {len(errors) - 8} more.")

        return "\n".join(lines)

    def _build_edit_mapping_payload(
        self,
        source_timeline_name: str,
        source_start_frame: int,
        source_video_item_count: int,
        source_audio_item_count: int,
        destination_name: str,
        destination_timeline,
        video_item_count: int,
        audio_item_count: int,
        fps: float,
        fps_raw: Any,
        gap_frames: int,
        placed_segments: List[Dict[str, Any]],
        planned_fragments: List[Dict[str, Any]],
        skipped_errors: List[str],
        timecode_normalization: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        timecode_normalization = timecode_normalization or {"applied": False}
        fragments_by_cut_index: Dict[int, List[Dict[str, Any]]] = {}
        for planned in planned_fragments:
            try:
                cut_index = int(planned["cut_entry"]["index"])
            except Exception:
                continue
            fragments_by_cut_index.setdefault(cut_index, []).append(planned)

        mapped_segments: List[Dict[str, Any]] = []
        edited_timeline_start_frame = int(destination_timeline.GetStartFrame() or 0)
        for placed in placed_segments:
            cut_entry = placed["cut_entry"]
            cut_index = int(cut_entry["index"])
            segment_fragments = fragments_by_cut_index.get(cut_index, [])
            edited_start_frame = int(placed["record_start_frame"])
            duration_frames = int(placed["duration_frames"])
            edited_end_frame_exclusive = edited_start_frame + duration_frames
            source_start_offset_real_fps = int(
                cut_entry.get("source_start_offset_real_fps", 0)
            )
            source_end_offset_real_fps = int(
                cut_entry.get("source_end_offset_real_fps", 0)
            )
            source_start_offset_dvr = source_start_offset_real_fps
            source_end_offset_dvr = source_end_offset_real_fps
            source_last_offset_dvr = max(source_start_offset_dvr, source_end_offset_dvr - 1)
            edited_last_frame_inclusive = max(
                edited_start_frame,
                edited_end_frame_exclusive - 1,
            )

            mapped_fragments: List[Dict[str, Any]] = []
            for planned_fragment in segment_fragments:
                fragment = planned_fragment["fragment"]
                fragment_duration = int(planned_fragment["duration_frames"])
                fragment_edited_start = int(planned_fragment["record_frame"])
                fragment_edited_end_exclusive = fragment_edited_start + fragment_duration
                fragment_source_start = int(
                    fragment.get(
                        "timeline_overlap_start_frame",
                        cut_entry["timeline_start_frame"],
                    )
                )
                fragment_source_end_exclusive = int(
                    fragment.get(
                        "timeline_overlap_end_frame_exclusive",
                        fragment_source_start + fragment_duration,
                    )
                )

                mapped_fragments.append(
                    {
                        "fragment_index": int(planned_fragment["fragment_index"]),
                        "source_track_index": int(fragment.get("track_index") or 1),
                        "source_item_name": fragment.get("source_item_name") or "Unknown Clip",
                        "source_media_name": self._safe_media_pool_item_name(
                            planned_fragment["media_pool_item"]
                        ),
                        "source_start_frame": fragment_source_start,
                        "source_end_frame_exclusive": fragment_source_end_exclusive,
                        "source_end_frame_inclusive": fragment_source_end_exclusive - 1,
                        "edited_start_frame": fragment_edited_start,
                        "edited_end_frame_exclusive": fragment_edited_end_exclusive,
                        "edited_end_frame_inclusive": fragment_edited_end_exclusive - 1,
                        "duration_frames": fragment_duration,
                    }
                )

            source_start_frame_value = int(cut_entry["timeline_start_frame"])
            source_end_frame_exclusive = int(cut_entry["timeline_end_frame_exclusive"])

            mapped_segments.append(
                {
                    "segment_index": int(cut_entry["index"]),
                    "episode_number": int(cut_entry["episode_number"]),
                    "segment_id": cut_entry.get("segment_id") or "EP{:02d}".format(
                        int(cut_entry["episode_number"])
                    ),
                    "title": cut_entry["title_text"],
                    "editorial_reason": cut_entry.get("editorial_reason") or "",
                    "qa_notes": cut_entry.get("qa_notes") or "",
                    "source_start_value": self._display_value(cut_entry["start_value"]),
                    "source_end_value": self._display_value(cut_entry["end_value"]),
                    "source_start_seconds": float(cut_entry["start_seconds"]),
                    "source_end_seconds": float(cut_entry["end_seconds"]),
                    "append_source_frame_method": "elapsed seconds * real fps",
                    "source_start_frame": source_start_frame_value,
                    "source_end_frame_exclusive": source_end_frame_exclusive,
                    "source_end_frame_inclusive": source_end_frame_exclusive - 1,
                    "was_trimmed": bool(cut_entry.get("was_trimmed")),
                    "trim_warning": cut_entry.get("trim_warning") or "",
                    "trimmed_tail_frames": int(cut_entry.get("trimmed_tail_frames") or 0),
                    "original_source_end_frame_exclusive": int(
                        cut_entry.get("original_timeline_end_frame_exclusive")
                        or source_end_frame_exclusive
                    ),
                    "source_start_offset_real_fps": int(
                        source_start_offset_real_fps
                    ),
                    "source_end_offset_real_fps": int(
                        source_end_offset_real_fps
                    ),
                    "source_dvr_in": self._frame_to_timecode(
                        source_start_offset_dvr,
                        fps,
                    ),
                    "source_dvr_out_exclusive": self._frame_to_timecode(
                        source_end_offset_dvr,
                        fps,
                    ),
                    "source_dvr_last_frame_inclusive": self._frame_to_timecode(
                        source_last_offset_dvr,
                        fps,
                    ),
                    "display_start_offset_nominal_fps": int(
                        cut_entry.get("display_start_offset_nominal_fps", 0)
                    ),
                    "display_end_offset_nominal_fps": int(
                        cut_entry.get("display_end_offset_nominal_fps", 0)
                    ),
                    "edited_start_frame": edited_start_frame,
                    "edited_end_frame_exclusive": edited_end_frame_exclusive,
                    "edited_end_frame_inclusive": edited_last_frame_inclusive,
                    "edited_timeline_in": self._frame_to_timecode(
                        edited_timeline_start_frame + edited_start_frame,
                        fps,
                    ),
                    "edited_timeline_out_exclusive": self._frame_to_timecode(
                        edited_timeline_start_frame + edited_end_frame_exclusive,
                        fps,
                    ),
                    "edited_timeline_last_frame_inclusive": self._frame_to_timecode(
                        edited_timeline_start_frame + edited_last_frame_inclusive,
                        fps,
                    ),
                    "duration_frames": duration_frames,
                    "duration_seconds": float(cut_entry["end_seconds"])
                    - float(cut_entry["start_seconds"]),
                    "was_fragmented": len(mapped_fragments) > 1,
                    "fragments": mapped_fragments,
                }
            )

        warnings: List[str] = []
        if len(mapped_segments) != len(placed_segments):
            warnings.append(
                "Mapped segment count does not match placed segment count (mapped={}, placed={}).".format(
                    len(mapped_segments),
                    len(placed_segments),
                )
            )

        return {
            "schema_version": "1.0",
            "created_by": "ai_edit_import_utility_timecode_exact_json.py",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mapping_type": "source_to_edited_timeline",
            "selected_json_path": self.selected_path or "",
            "json_time_model": {
                "input_times_are": "string timestamps are timeline_timecode_labels; numeric timestamps are elapsed_seconds",
                "append_source_frames": "source_timeline_start_frame + int(parsed_seconds * real_timeline_fps + 0.5)",
                "create_edited_timeline_normalization": (
                    "Resolve 01:00:00 timeline-label timecodes may be normalized to source-relative 00:00:00 before this formula is applied."
                ),
                "marker_dvr_labels": "source timeline DVR timecode derived from source_start_offset_real_fps/source_end_offset_real_fps, not edited output positions after gaps",
                "edited_timeline_labels": "edited output timeline positions after recordFrame gaps; explicitly labeled edited_timeline_*",
                "frame_intervals": "start_inclusive_end_exclusive",
            },
            "timecode_normalization": {
                "applied": bool(timecode_normalization.get("applied")),
                "message": timecode_normalization.get("message") or "",
                "offset_subtracted": timecode_normalization.get("offset_subtracted") or "",
                "offset_seconds": timecode_normalization.get("offset_seconds"),
                "earliest_original_seconds": timecode_normalization.get(
                    "earliest_original_seconds"
                ),
                "earliest_normalized_seconds": timecode_normalization.get(
                    "earliest_normalized_seconds"
                ),
                "max_original_end_seconds": timecode_normalization.get(
                    "max_original_end_seconds"
                ),
                "max_normalized_end_seconds": timecode_normalization.get(
                    "max_normalized_end_seconds"
                ),
                "reason": timecode_normalization.get("reason") or "",
                "warnings": [
                    str(warning)
                    for warning in timecode_normalization.get("warnings") or []
                ],
            },
            "source_timeline": {
                "name": source_timeline_name,
                "start_frame": int(source_start_frame),
                "video_item_count": int(source_video_item_count),
                "audio_item_count": int(source_audio_item_count),
            },
            "edited_timeline": {
                "name": destination_name,
                "start_frame": int(destination_timeline.GetStartFrame() or 0),
                "video_item_count": int(video_item_count),
                "audio_item_count": int(audio_item_count),
            },
            "fps": {
                "value": float(fps),
                "raw": fps_raw,
                "nominal_timecode_fps": self._nominal_timecode_fps_from_fps(fps),
            },
            "gap": {
                "seconds": float(GAP_SECONDS),
                "frames": int(gap_frames),
                "method": "recordFrame empty gap request",
            },
            "segments": mapped_segments,
            "skipped_segments": [
                {"reason": str(error)}
                for error in skipped_errors
            ],
            "trimmed_segments": [
                {
                    "episode_number": int(segment["episode_number"]),
                    "segment_id": segment.get("segment_id") or "",
                    "warning": segment.get("trim_warning"),
                    "trimmed_tail_frames": int(segment.get("trimmed_tail_frames") or 0),
                    "original_source_end_frame_exclusive": int(
                        segment.get("original_timeline_end_frame_exclusive") or 0
                    ),
                    "trimmed_source_end_frame_exclusive": int(
                        segment.get("source_end_frame_exclusive") or 0
                    ),
                }
                for segment in mapped_segments
                if segment.get("was_trimmed")
            ],
            "warnings": warnings,
        }

    def _write_edit_mapping_files(self, mapping_payload: Dict[str, Any]) -> Dict[str, Any]:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        warnings: List[str] = []
        payload_text = json.dumps(mapping_payload, ensure_ascii=False, indent=2) + "\n"

        candidate_directories: List[Path] = []
        if self.selected_path:
            candidate_directories.append(Path(self.selected_path).expanduser().parent)
        candidate_directories.append(Path.home() / "Desktop")

        last_error = None
        stable_path = None
        timestamped_path = None

        for directory_index, directory in enumerate(candidate_directories):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                candidate_stable = directory / "edit_mapping.json"
                candidate_timestamped = directory / f"edit_mapping_{timestamp}.json"
                candidate_stable.write_text(payload_text, encoding="utf-8")
                candidate_timestamped.write_text(payload_text, encoding="utf-8")
                stable_path = candidate_stable
                timestamped_path = candidate_timestamped
                if directory_index > 0:
                    warnings.append(
                        "Could not write mapping next to selected JSON; wrote mapping to Desktop instead."
                    )
                break
            except Exception as exc:
                last_error = exc
                if directory_index == 0:
                    warnings.append(
                        "Could not write mapping next to selected JSON ({}).".format(exc)
                    )

        if stable_path is None or timestamped_path is None:
            warnings.append("Could not write edit mapping files: {}".format(last_error))

        mapped_segments = len(mapping_payload.get("segments") or [])
        mapped_fragments = sum(
            len(segment.get("fragments") or [])
            for segment in mapping_payload.get("segments") or []
        )

        return {
            "stable_path": str(stable_path) if stable_path else "",
            "timestamped_path": str(timestamped_path) if timestamped_path else "",
            "mapped_segments": mapped_segments,
            "mapped_fragments": mapped_fragments,
            "warnings": warnings,
        }

    def _write_timing_report_files(
        self,
        mapping_payload: Dict[str, Any],
        fps: float,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        csv_path = None
        markdown_path = None

        candidate_directories: List[Path] = []
        if self.selected_path:
            candidate_directories.append(Path(self.selected_path).expanduser().parent)
        candidate_directories.append(Path.home() / "Desktop")

        rows = self._build_timing_report_rows(mapping_payload, fps)
        for directory_index, directory in enumerate(candidate_directories):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                candidate_csv = directory / "ai_edit_timing_report.csv"
                candidate_markdown = directory / "ai_edit_timing_report.md"
                self._write_timing_report_csv(candidate_csv, rows)
                self._write_timing_report_markdown(candidate_markdown, rows, mapping_payload)
                csv_path = candidate_csv
                markdown_path = candidate_markdown
                if directory_index > 0:
                    warnings.append(
                        "Could not write timing report next to selected JSON; wrote report to Desktop instead."
                    )
                break
            except Exception as exc:
                if directory_index == 0:
                    warnings.append(
                        "Could not write timing report next to selected JSON ({}).".format(exc)
                    )
                else:
                    warnings.append("Could not write timing report files: {}".format(exc))

        return {
            "csv_path": str(csv_path) if csv_path else "",
            "markdown_path": str(markdown_path) if markdown_path else "",
            "rows": len(rows),
            "warnings": warnings,
        }

    def _build_timing_report_rows(
        self,
        mapping_payload: Dict[str, Any],
        fps: float,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for segment in mapping_payload.get("segments") or []:
            edited_start = int(segment.get("edited_start_frame") or 0)
            edited_end_exclusive = int(segment.get("edited_end_frame_exclusive") or 0)
            duration_frames = int(segment.get("duration_frames") or 0)
            source_start_frame = int(segment.get("source_start_frame") or 0)
            source_end_frame_exclusive = int(segment.get("source_end_frame_exclusive") or 0)
            rows.append(
                {
                    "episode_number": int(segment.get("episode_number") or 0),
                    "segment_id": segment.get("segment_id") or "",
                    "title": segment.get("title") or "",
                    "json_in": segment.get("source_start_value") or "",
                    "json_out": segment.get("source_end_value") or "",
                    "json_duration_seconds": "{:.3f}".format(
                        float(segment.get("duration_seconds") or 0.0)
                    ),
                    "source_dvr_in": segment.get("source_dvr_in") or "",
                    "source_dvr_out_exclusive": segment.get("source_dvr_out_exclusive") or "",
                    "source_dvr_last_frame_inclusive": (
                        segment.get("source_dvr_last_frame_inclusive") or ""
                    ),
                    "edited_timeline_in": segment.get("edited_timeline_in") or "",
                    "edited_timeline_out_exclusive": (
                        segment.get("edited_timeline_out_exclusive") or ""
                    ),
                    "resolved_source_in_frame": source_start_frame,
                    "resolved_source_out_frame_exclusive": source_end_frame_exclusive,
                    "edited_start_frame": edited_start,
                    "edited_end_frame_exclusive": edited_end_exclusive,
                    "duration_frames": duration_frames,
                    "actual_duration_seconds": "{:.3f}".format(
                        float(duration_frames) / float(fps) if fps else 0.0
                    ),
                    "was_trimmed": "yes" if segment.get("was_trimmed") else "no",
                    "trimmed_tail_frames": int(segment.get("trimmed_tail_frames") or 0),
                    "trim_warning": segment.get("trim_warning") or "",
                }
            )
        return rows

    @staticmethod
    def _write_timing_report_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
        fieldnames = [
            "episode_number",
            "segment_id",
            "title",
            "json_in",
            "json_out",
            "json_duration_seconds",
            "source_dvr_in",
            "source_dvr_out_exclusive",
            "source_dvr_last_frame_inclusive",
            "edited_timeline_in",
            "edited_timeline_out_exclusive",
            "resolved_source_in_frame",
            "resolved_source_out_frame_exclusive",
            "edited_start_frame",
            "edited_end_frame_exclusive",
            "duration_frames",
            "actual_duration_seconds",
            "was_trimmed",
            "trimmed_tail_frames",
            "trim_warning",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _write_timing_report_markdown(
        self,
        path: Path,
        rows: List[Dict[str, Any]],
        mapping_payload: Dict[str, Any],
    ) -> None:
        lines = [
            "# AI Edit Timing Report",
            "",
            "Generated: {}".format(mapping_payload.get("created_at") or ""),
            "Selected JSON: {}".format(mapping_payload.get("selected_json_path") or ""),
            "Output timeline: {}".format(
                (mapping_payload.get("edited_timeline") or {}).get("name") or ""
            ),
            "",
            "Frame intervals are start-inclusive/end-exclusive. Source DVR columns show original source timeline timing from the JSON/source offsets. Edited timeline columns show placement after inserted gaps.",
            "",
            "| EP | Segment ID | JSON In | JSON Out | Source DVR In | Source DVR Out Exclusive | Source Last Frame | Edited Timeline In | Edited Timeline Out Exclusive | Frames | Trimmed |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
        for row in rows:
            lines.append(
                "| {episode_number} | {segment_id} | {json_in} | {json_out} | {source_dvr_in} | {source_dvr_out_exclusive} | {source_dvr_last_frame_inclusive} | {edited_timeline_in} | {edited_timeline_out_exclusive} | {duration_frames} | {was_trimmed} |".format(
                    **row
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_timing_overlay_files(
        self,
        mapping_payload: Dict[str, Any],
        fps: float,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        srt_path = None
        vtt_path = None

        candidate_directories: List[Path] = []
        if self.selected_path:
            candidate_directories.append(Path(self.selected_path).expanduser().parent)
        candidate_directories.append(Path.home() / "Desktop")

        cues = self._build_timing_overlay_cues(mapping_payload, fps)
        for directory_index, directory in enumerate(candidate_directories):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                candidate_srt = directory / "ai_edit_timing_overlay.srt"
                candidate_vtt = directory / "ai_edit_timing_overlay.vtt"
                self._write_timing_overlay_srt(candidate_srt, cues, fps)
                self._write_timing_overlay_vtt(candidate_vtt, cues, fps)
                srt_path = candidate_srt
                vtt_path = candidate_vtt
                if directory_index > 0:
                    warnings.append(
                        "Could not write timing overlay next to selected JSON; wrote overlay to Desktop instead."
                    )
                break
            except Exception as exc:
                if directory_index == 0:
                    warnings.append(
                        "Could not write timing overlay next to selected JSON ({}).".format(exc)
                    )
                else:
                    warnings.append("Could not write timing overlay files: {}".format(exc))

        return {
            "srt_path": str(srt_path) if srt_path else "",
            "vtt_path": str(vtt_path) if vtt_path else "",
            "cue_count": len(cues),
            "warnings": warnings,
        }

    def _build_timing_overlay_cues(
        self,
        mapping_payload: Dict[str, Any],
        fps: float,
    ) -> List[Dict[str, Any]]:
        cues: List[Dict[str, Any]] = []
        cue_duration_frames = max(1, int((float(fps) * 3.0) + 0.5)) if fps else 72
        for segment in mapping_payload.get("segments") or []:
            edited_start = int(segment.get("edited_start_frame") or 0)
            edited_end_exclusive = int(segment.get("edited_end_frame_exclusive") or 0)
            if edited_end_exclusive <= edited_start:
                continue
            cue_end = min(edited_start + cue_duration_frames, edited_end_exclusive)
            if cue_end <= edited_start:
                cue_end = edited_start + 1
            cues.append(
                {
                    "start_frame": edited_start,
                    "end_frame_exclusive": cue_end,
                    "text": self._build_timing_overlay_text(segment, fps),
                }
            )
        return cues

    def _build_timing_overlay_text(self, segment: Dict[str, Any], fps: float) -> str:
        episode_number = int(segment.get("episode_number") or 0)
        title = self._compact_single_line(segment.get("title") or "", 48)
        json_out = segment.get("source_end_value") or ""
        duration_frames = int(segment.get("duration_frames") or 0)
        duration_seconds = float(duration_frames) / float(fps) if fps else 0.0
        lines = [
            "EP{:02d} | {}".format(episode_number, title).rstrip(),
            "JSON OUT {}".format(json_out),
            "SOURCE DVR OUT {} | LAST {}".format(
                segment.get("source_dvr_out_exclusive") or "",
                segment.get("source_dvr_last_frame_inclusive") or "",
            ),
            "DUR {:.3f}s / {}f".format(duration_seconds, duration_frames),
        ]
        trimmed_tail_frames = int(segment.get("trimmed_tail_frames") or 0)
        if segment.get("was_trimmed") and trimmed_tail_frames:
            lines.append("TRIMMED -{}f".format(trimmed_tail_frames))
        return "\n".join(lines)

    @staticmethod
    def _compact_single_line(value: Any, max_length: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if max_length > 3 and len(text) > max_length:
            return text[: max_length - 3].rstrip() + "..."
        return text

    def _write_timing_overlay_srt(
        self,
        path: Path,
        cues: List[Dict[str, Any]],
        fps: float,
    ) -> None:
        lines: List[str] = []
        for index, cue in enumerate(cues, start=1):
            lines.extend(
                [
                    str(index),
                    "{} --> {}".format(
                        self._frame_to_subtitle_timestamp(
                            int(cue["start_frame"]),
                            fps,
                            decimal_separator=",",
                        ),
                        self._frame_to_subtitle_timestamp(
                            int(cue["end_frame_exclusive"]),
                            fps,
                            decimal_separator=",",
                        ),
                    ),
                    str(cue["text"]),
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")

    def _write_timing_overlay_vtt(
        self,
        path: Path,
        cues: List[Dict[str, Any]],
        fps: float,
    ) -> None:
        lines = ["WEBVTT", ""]
        for cue in cues:
            lines.extend(
                [
                    "{} --> {}".format(
                        self._frame_to_subtitle_timestamp(
                            int(cue["start_frame"]),
                            fps,
                            decimal_separator=".",
                        ),
                        self._frame_to_subtitle_timestamp(
                            int(cue["end_frame_exclusive"]),
                            fps,
                            decimal_separator=".",
                        ),
                    ),
                    str(cue["text"]),
                    "",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _frame_to_subtitle_timestamp(
        frame: int,
        fps: float,
        decimal_separator: str,
    ) -> str:
        safe_fps = float(fps) if fps else 24.0
        total_milliseconds = int(round((max(0, int(frame)) / safe_fps) * 1000.0))
        milliseconds = total_milliseconds % 1000
        total_seconds = total_milliseconds // 1000
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60
        return "{:02d}:{:02d}:{:02d}{}{:03d}".format(
            hours,
            minutes,
            seconds,
            decimal_separator,
            milliseconds,
        )

    @classmethod
    def _frame_to_timecode(cls, frame: int, fps: float) -> str:
        nominal_fps = cls._nominal_timecode_fps_from_fps(fps)
        frame_value = max(0, int(frame))
        frames = frame_value % nominal_fps
        total_seconds = frame_value // nominal_fps
        seconds = total_seconds % 60
        total_minutes = total_seconds // 60
        minutes = total_minutes % 60
        hours = total_minutes // 60
        return "{:02d}:{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds, frames)

    def _build_append_plan(
        self,
        resolved_entries: List[Dict[str, Any]],
        gap_frames: int,
        debug_lines: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        planned_fragments: List[Dict[str, Any]] = []
        planned_segments: List[Dict[str, Any]] = []
        current_record_frame = 0

        debug_lines.append("Prepared append plan")
        debug_lines.append("====================")

        for segment_index, cut_entry in enumerate(resolved_entries, start=1):
            segment_start_frame = current_record_frame
            segment_duration_frames = 0
            segment_fragment_count = 0

            for fragment_index, fragment in enumerate(cut_entry.get("fragments") or [], start=1):
                duration_frames = int(fragment["duration_frames"])
                if duration_frames <= 0:
                    debug_lines.append(
                        "EP{:02d} fragment {} skipped: duration was zero.".format(
                            cut_entry["episode_number"],
                            fragment_index,
                        )
                    )
                    continue

                media_pool_item = fragment["media_pool_item"]
                clip_info_a = {
                    "mediaPoolItem": media_pool_item,
                    "startFrame": int(fragment["source_start_frame"]),
                    "endFrame": int(fragment["source_end_frame_inclusive"]),
                    "recordFrame": int(current_record_frame),
                    "mediaType": 1,
                    "trackIndex": 1,
                }
                clip_info_b = {
                    "mediaPoolItem": media_pool_item,
                    "startFrame": int(fragment["source_start_frame"]),
                    "endFrame": int(fragment["source_end_frame_inclusive"]),
                    "mediaType": 1,
                    "trackIndex": 1,
                }
                clip_info_legacy_a = {
                    "mediaPoolItem": media_pool_item,
                    "startFrame": int(fragment["source_start_frame"]),
                    "endFrame": int(fragment["source_end_frame_inclusive"]),
                    "recordFrame": int(current_record_frame),
                }
                clip_info_legacy_b = {
                    "mediaPoolItem": media_pool_item,
                    "startFrame": int(fragment["source_start_frame"]),
                    "endFrame": int(fragment["source_end_frame_inclusive"]),
                }
                media_name = self._safe_media_pool_item_name(media_pool_item)
                media_path = self._get_media_file_path_or_unknown(media_pool_item)
                source_item_name = fragment.get("source_item_name") or "Unknown source item"
                timing_diagnostics = self._append_timing_diagnostic_lines(
                    cut_entry,
                    fragment,
                    clip_info_a,
                )

                debug_lines.extend(
                    [
                        "",
                        "PLAN EP{:02d} fragment {}".format(cut_entry["episode_number"], fragment_index),
                        "Segment title: {}".format(cut_entry["title_text"]),
                        "Source timeline item name: {}".format(source_item_name),
                        "MediaPoolItem name: {}".format(media_name),
                        "Source media file path: {}".format(media_path),
                        "Original JSON start: {}".format(self._display_value(cut_entry["start_value"])),
                        "Original JSON end: {}".format(self._display_value(cut_entry["end_value"])),
                        "Parsed JSON start timecode: {}".format(
                            self._format_json_time_debug(cut_entry.get("start_time_debug") or {})
                        ),
                        "Parsed JSON end timecode: {}".format(
                            self._format_json_time_debug(cut_entry.get("end_time_debug") or {})
                        ),
                        "Append/source frame conversion: elapsed seconds * real timeline FPS",
                        "Nominal display/timecode FPS for marker/debug labels: {}".format(
                            cut_entry.get("nominal_timecode_fps") or "unknown"
                        ),
                        "Calculated source timeline start frame: {}".format(cut_entry["timeline_start_frame"]),
                        "Calculated source timeline end frame exclusive: {}".format(cut_entry["timeline_end_frame_exclusive"]),
                        "Calculated source timeline end frame inclusive: {}".format(cut_entry["timeline_end_frame_inclusive"]),
                        "Source coverage trim: {}".format(
                            cut_entry.get("trim_warning") or "none"
                        ),
                        "Timeline item start/end: {}..{}".format(
                            self._safe_timeline_item_start(fragment.get("timeline_item")),
                            self._safe_timeline_item_end(fragment.get("timeline_item")),
                        ),
                        "Timeline item source start frame: {}".format(
                            self._safe_timeline_item_source_start(fragment.get("timeline_item"))
                        ),
                        "MediaPoolItem duration property: {}".format(
                            self._safe_media_pool_item_duration(media_pool_item)
                        ),
                        "Append startFrame: {}".format(clip_info_a["startFrame"]),
                        "Append endFrame: {}".format(clip_info_a["endFrame"]),
                        "Append recordFrame: {}".format(clip_info_a["recordFrame"]),
                        "Duration frames: {}".format(duration_frames),
                        *timing_diagnostics,
                        "Attempt A clipInfo: {}".format(self._clip_info_for_debug(clip_info_a)),
                        "Attempt B clipInfo: {}".format(self._clip_info_for_debug(clip_info_b)),
                        "Legacy ranged with recordFrame clipInfo: {}".format(
                            self._clip_info_for_debug(clip_info_legacy_a)
                        ),
                        "Legacy ranged without recordFrame clipInfo: {}".format(
                            self._clip_info_for_debug(clip_info_legacy_b)
                        ),
                    ]
                )

                planned_fragments.append(
                    {
                        "cut_entry": cut_entry,
                        "fragment_index": fragment_index,
                        "fragment": fragment,
                        "duration_frames": duration_frames,
                        "record_frame": int(current_record_frame),
                        "clip_info_a": clip_info_a,
                        "clip_info_b": clip_info_b,
                        "clip_info_legacy_a": clip_info_legacy_a,
                        "clip_info_legacy_b": clip_info_legacy_b,
                        "media_pool_item": media_pool_item,
                    }
                )
                current_record_frame += duration_frames
                segment_duration_frames += duration_frames
                segment_fragment_count += 1

            if segment_fragment_count > 0 and segment_duration_frames > 0:
                planned_segments.append(
                    {
                        "cut_entry": cut_entry,
                        "record_start_frame": int(segment_start_frame),
                        "duration_frames": int(segment_duration_frames),
                    }
                )

            if segment_index < len(resolved_entries):
                current_record_frame += int(gap_frames)

        debug_lines.append("")
        debug_lines.append("Prepared fragments: {}".format(len(planned_fragments)))
        debug_lines.append("Prepared segments: {}".format(len(planned_segments)))
        debug_lines.append("")
        return planned_fragments, planned_segments

    def _create_clean_append_timeline(
        self,
        project,
        destination_name: str,
        planned_fragments: List[Dict[str, Any]],
        planned_segments: List[Dict[str, Any]],
        debug_lines: List[str],
        debug_report_path: Path,
        audio_debug_lines: List[str],
        audio_debug_report_path: Path,
    ) -> Tuple[Any, List[Dict[str, Any]], List[str], Dict[str, int]]:
        media_pool = project.GetMediaPool()
        if not media_pool:
            raise RuntimeError("Could not access Resolve Media Pool.")
        if not hasattr(media_pool, "CreateEmptyTimeline"):
            raise RuntimeError(
                "CreateEmptyTimeline is not available. Clean AppendToTimeline workflow cannot continue."
            )

        destination_timeline = media_pool.CreateEmptyTimeline(destination_name)
        if not destination_timeline:
            raise RuntimeError(f"Resolve could not create output timeline '{destination_name}'.")
        self._set_current_timeline_or_raise(project, destination_timeline, destination_name)
        self._ensure_destination_tracks(destination_timeline, debug_lines)
        destination_start_frame = int(destination_timeline.GetStartFrame() or 0)
        debug_lines.append("")
        debug_lines.append("Output timeline created")
        debug_lines.append("Output timeline name: {}".format(destination_name))
        debug_lines.append("Output timeline start frame: {}".format(destination_start_frame))
        debug_lines.append(
            "Output track counts after track ensure: video={}, audio={}".format(
                destination_timeline.GetTrackCount("video") or 0,
                destination_timeline.GetTrackCount("audio") or 0,
            )
        )
        audio_debug_lines.extend(
            [
                "Output timeline name: {}".format(destination_name),
                "Output timeline start frame: {}".format(destination_start_frame),
                "Output audio track count after track ensure: {}".format(
                    self._safe_track_count(destination_timeline, "audio")
                ),
                "Output video item count before appends: {}".format(
                    self._count_timeline_items(destination_timeline, "video")
                ),
                "Output audio item count before appends: {}".format(
                    self._count_timeline_items(destination_timeline, "audio")
                ),
                "",
                "Audio append attempts",
                "=====================",
            ]
        )

        warnings: List[str] = []
        placed_segments: List[Dict[str, Any]] = []
        expected_fragment_starts: List[int] = []
        append_modes_used = set()
        audio_stats = {"attempts": 0, "successes": 0, "failures": 0}
        audio_modes_used = set()

        debug_lines.append("Append attempts")
        debug_lines.append("===============")

        successful_cut_indexes = set()
        for planned in planned_fragments:
            cut_entry = planned["cut_entry"]
            fragment_index = planned["fragment_index"]
            duration_frames = planned["duration_frames"]
            record_frame = planned["record_frame"]
            output_count_before = self._count_timeline_items(destination_timeline, "video")

            debug_lines.extend(
                [
                    "",
                    "APPEND EP{:02d} fragment {}".format(cut_entry["episode_number"], fragment_index),
                    "Output video count before: {}".format(output_count_before),
                ]
            )

            current_append_mode = None
            accepted_video_clip_info = None
            attempt_a_clip_info = self._with_destination_record_frame(
                planned["clip_info_a"],
                destination_start_frame,
            )
            appended_items = self._append_with_debug(
                media_pool,
                destination_timeline,
                attempt_a_clip_info,
                "Attempt A: ranged clipInfo with recordFrame, mediaType=1, trackIndex=1",
                debug_lines,
            )
            if appended_items and self._count_timeline_items(destination_timeline, "video") <= output_count_before:
                debug_lines.append(
                    "Attempt A returned items but video count did not increase; treating as failed append."
                )
                appended_items = None
            if appended_items:
                current_append_mode = "ranged clipInfo with recordFrame"
                accepted_video_clip_info = attempt_a_clip_info

            if not appended_items:
                appended_items = self._append_with_debug(
                    media_pool,
                    destination_timeline,
                    planned["clip_info_b"],
                    "Attempt B: ranged clipInfo without recordFrame, mediaType=1, trackIndex=1",
                    debug_lines,
                )
                if appended_items and self._count_timeline_items(destination_timeline, "video") <= output_count_before:
                    debug_lines.append(
                        "Attempt B returned items but video count did not increase; treating as failed append."
                    )
                    appended_items = None
                if appended_items:
                    warnings.append(
                        "EP{:02d} fragment {}: Attempt B worked, so Resolve may be rejecting recordFrame gaps.".format(
                            cut_entry["episode_number"],
                            fragment_index,
                        )
                    )
                if appended_items:
                    append_modes_used.add("ranged clipInfo without recordFrame")
                    current_append_mode = "ranged clipInfo without recordFrame"
                    accepted_video_clip_info = planned["clip_info_b"]

            if not appended_items:
                legacy_a_clip_info = self._with_destination_record_frame(
                    planned["clip_info_legacy_a"],
                    destination_start_frame,
                )
                appended_items = self._append_with_debug(
                    media_pool,
                    destination_timeline,
                    legacy_a_clip_info,
                    "Legacy diagnostic: ranged clipInfo with recordFrame, no mediaType/trackIndex",
                    debug_lines,
                )
                if appended_items and self._count_timeline_items(destination_timeline, "video") <= output_count_before:
                    debug_lines.append(
                        "Legacy with recordFrame returned items but video count did not increase; treating as failed append."
                    )
                    appended_items = None
                if appended_items:
                    current_append_mode = "legacy ranged clipInfo with recordFrame"
                    accepted_video_clip_info = legacy_a_clip_info

            if not appended_items:
                appended_items = self._append_with_debug(
                    media_pool,
                    destination_timeline,
                    planned["clip_info_legacy_b"],
                    "Legacy diagnostic: ranged clipInfo without recordFrame, no mediaType/trackIndex",
                    debug_lines,
                )
                if appended_items and self._count_timeline_items(destination_timeline, "video") <= output_count_before:
                    debug_lines.append(
                        "Legacy without recordFrame returned items but video count did not increase; treating as failed append."
                    )
                    appended_items = None
                if appended_items:
                    current_append_mode = "legacy ranged clipInfo without recordFrame"
                    accepted_video_clip_info = planned["clip_info_legacy_b"]

            if not appended_items:
                whole_item_result = self._append_whole_item_for_diagnostics(
                    media_pool,
                    destination_timeline,
                    planned["media_pool_item"],
                    cut_entry,
                    fragment_index,
                    debug_lines,
                )
                if whole_item_result:
                    warnings.append(
                        "EP{:02d} fragment {}: whole MediaPoolItem append works, but ranged clipInfo failed. Check start/end frame math or ranged clipInfo support.".format(
                            cut_entry["episode_number"],
                            fragment_index,
                        )
                    )
                else:
                    warnings.append(
                        "EP{:02d} fragment {}: whole MediaPoolItem append also failed. Check MediaPoolItem/timeline/API context.".format(
                            cut_entry["episode_number"],
                            fragment_index,
                        )
                )
                continue

            if current_append_mode:
                append_modes_used.add(current_append_mode)
            if accepted_video_clip_info:
                debug_lines.extend(
                    self._post_append_timeline_item_diagnostic_lines(
                        appended_items,
                        accepted_video_clip_info,
                        cut_entry,
                        fragment_index,
                    )
                )
            if accepted_video_clip_info:
                audio_result = self._append_matching_audio_for_video(
                    media_pool,
                    destination_timeline,
                    accepted_video_clip_info,
                    current_append_mode or "unknown video append mode",
                    cut_entry,
                    fragment_index,
                    duration_frames,
                    audio_debug_lines,
                )
                audio_stats["attempts"] += 1
                if audio_result["success"]:
                    audio_stats["successes"] += 1
                    audio_modes_used.add(str(audio_result["mode"]))
                else:
                    audio_stats["failures"] += 1
                    warnings.append(
                        "EP{:02d} fragment {}: audio append failed ({}).".format(
                            cut_entry["episode_number"],
                            fragment_index,
                            audio_result["reason"],
                        )
                    )
            expected_fragment_starts.append(int(destination_start_frame) + int(record_frame))
            successful_cut_indexes.add(int(cut_entry["index"]))
            output_count_after = self._count_timeline_items(destination_timeline, "video")
            debug_lines.append("Accepted append duration frames: {}".format(duration_frames))
            debug_lines.append("Output video count after accepted append: {}".format(output_count_after))

        for planned_segment in planned_segments:
            if int(planned_segment["cut_entry"]["index"]) in successful_cut_indexes:
                placed_segments.append(planned_segment)

        if not placed_segments:
            audio_debug_lines.append("")
            audio_debug_lines.append("No placed video segments; audio append did not produce a usable edit.")
            self._write_debug_report(debug_report_path, debug_lines)
            self._write_debug_report(audio_debug_report_path, audio_debug_lines)
            self._delete_empty_timeline_if_possible(media_pool, destination_timeline, debug_lines)
            raise RuntimeError(
                "No clips were appended to the output timeline.\n\n"
                "Possible causes:\n"
                "1. You ran the script while AI_Edit_Episodes was the active timeline instead of the original source timeline.\n"
                "2. The calculated source start/end frames are invalid.\n"
                "3. Resolve rejected the AppendToTimeline clipInfo format.\n"
                "4. The selected JSON times do not overlap clips on the source timeline.\n\n"
                "A debug file was saved here:\n{}".format(debug_report_path)
            )

        self._set_current_timeline_or_raise(project, destination_timeline, destination_name)
        video_item_count = self._count_timeline_items(destination_timeline, "video")
        if video_item_count == 0:
            audio_debug_lines.append("")
            audio_debug_lines.append("Output timeline had zero video items after append attempts.")
            self._write_debug_report(debug_report_path, debug_lines)
            self._write_debug_report(audio_debug_report_path, audio_debug_lines)
            self._delete_empty_timeline_if_possible(media_pool, destination_timeline, debug_lines)
            raise RuntimeError(
                "No clips were appended to the output timeline.\n\n"
                "Possible causes:\n"
                "1. You ran the script while AI_Edit_Episodes was the active timeline instead of the original source timeline.\n"
                "2. The calculated source start/end frames are invalid.\n"
                "3. Resolve rejected the AppendToTimeline clipInfo format.\n"
                "4. The selected JSON times do not overlap clips on the source timeline.\n\n"
                "A debug file was saved here:\n{}".format(debug_report_path)
            )

        gap_warning = self._verify_expected_record_frames(
            destination_timeline,
            expected_fragment_starts,
        )
        if gap_warning:
            warnings.append(gap_warning)

        if append_modes_used:
            warnings.append(
                "Append mode used: {}".format(", ".join(sorted(append_modes_used)))
            )
        if audio_modes_used:
            warnings.append(
                "Audio append mode used: {}".format(", ".join(sorted(audio_modes_used)))
            )
        warnings.append(
            "Output video items after append: {}".format(
                self._count_timeline_items(destination_timeline, "video")
            )
        )
        warnings.append(
            "Output audio items after append: {}".format(
                self._count_timeline_items(destination_timeline, "audio")
            )
        )
        warnings.append("Audio append attempts: {}".format(audio_stats["attempts"]))
        warnings.append("Audio append successes: {}".format(audio_stats["successes"]))
        warnings.append("Audio append failures: {}".format(audio_stats["failures"]))

        audio_debug_lines.extend(
            [
                "",
                "Audio append summary",
                "====================",
                "Audio append attempts: {}".format(audio_stats["attempts"]),
                "Audio append successes: {}".format(audio_stats["successes"]),
                "Audio append failures: {}".format(audio_stats["failures"]),
                "Output video item count after appends: {}".format(
                    self._count_timeline_items(destination_timeline, "video")
                ),
                "Output audio item count after appends: {}".format(
                    self._count_timeline_items(destination_timeline, "audio")
                ),
            ]
        )

        self._write_debug_report(debug_report_path, debug_lines)
        self._write_debug_report(audio_debug_report_path, audio_debug_lines)
        return destination_timeline, placed_segments, warnings, audio_stats

    def _append_matching_audio_for_video(
        self,
        media_pool,
        timeline,
        video_clip_info: Dict[str, Any],
        video_append_mode: str,
        cut_entry: Dict[str, Any],
        fragment_index: int,
        duration_frames: int,
        audio_debug_lines: List[str],
    ) -> Dict[str, Any]:
        audio_clip_info = self._build_audio_clip_info(video_clip_info)
        audio_count_before = self._count_timeline_items(timeline, "audio")
        video_count_before = self._count_timeline_items(timeline, "video")
        video_record_frame = video_clip_info.get("recordFrame")
        audio_record_frame = audio_clip_info.get("recordFrame")
        video_duration = int(video_clip_info["endFrame"]) - int(video_clip_info["startFrame"]) + 1
        audio_duration = int(audio_clip_info["endFrame"]) - int(audio_clip_info["startFrame"]) + 1

        audio_debug_lines.extend(
            [
                "",
                "AUDIO EP{:02d} fragment {}".format(cut_entry["episode_number"], fragment_index),
                "Video append mode used: {}".format(video_append_mode),
                "Audio append mode used: mediaType=2, trackIndex=1",
                "MediaPoolItem: {}".format(
                    self._safe_media_pool_item_name(audio_clip_info["mediaPoolItem"])
                ),
                "Audio clipInfo: {}".format(self._clip_info_for_debug(audio_clip_info)),
                "Output video item count before audio append: {}".format(video_count_before),
                "Output audio item count before audio append: {}".format(audio_count_before),
                "Video/audio recordFrames match: {}".format(video_record_frame == audio_record_frame),
                "Video recordFrame: {}".format(video_record_frame),
                "Audio recordFrame: {}".format(audio_record_frame),
                "Video/audio durations match: {}".format(video_duration == audio_duration),
                "Video duration frames: {}".format(video_duration),
                "Audio duration frames: {}".format(audio_duration),
                "Expected segment duration frames: {}".format(duration_frames),
            ]
        )

        try:
            result = media_pool.AppendToTimeline([audio_clip_info])
        except Exception as exc:
            audio_debug_lines.append("Audio AppendToTimeline exception: {}".format(exc))
            return {"success": False, "mode": "mediaType=2 trackIndex=1", "reason": str(exc)}

        audio_count_after = self._count_timeline_items(timeline, "audio")
        video_count_after = self._count_timeline_items(timeline, "video")
        audio_debug_lines.append(
            "Audio AppendToTimeline return: {}".format(self._append_result_for_debug(result))
        )
        audio_debug_lines.append(
            "Output video item count after audio append: {}".format(video_count_after)
        )
        audio_debug_lines.append(
            "Output audio item count immediately after audio append: {}".format(audio_count_after)
        )

        if not result:
            return {
                "success": False,
                "mode": "mediaType=2 trackIndex=1",
                "reason": "AppendToTimeline returned no result",
            }
        if audio_count_after <= audio_count_before:
            return {
                "success": False,
                "mode": "mediaType=2 trackIndex=1",
                "reason": "audio item count did not increase",
            }
        return {"success": True, "mode": "mediaType=2 trackIndex=1", "reason": ""}

    @staticmethod
    def _build_audio_clip_info(video_clip_info: Dict[str, Any]) -> Dict[str, Any]:
        audio_clip_info = dict(video_clip_info)
        audio_clip_info["mediaType"] = 2
        audio_clip_info["trackIndex"] = 1
        return audio_clip_info

    @staticmethod
    def _with_destination_record_frame(
        clip_info: Dict[str, Any],
        destination_start_frame: int,
    ) -> Dict[str, Any]:
        adjusted = dict(clip_info)
        if "recordFrame" in adjusted:
            adjusted["recordFrame"] = int(destination_start_frame) + int(adjusted["recordFrame"])
        return adjusted

    @staticmethod
    def _ensure_destination_tracks(timeline, debug_lines: List[str]) -> None:
        """Create base tracks if Resolve made a truly empty timeline."""
        for track_type in ("video", "audio"):
            try:
                track_count = int(timeline.GetTrackCount(track_type) or 0)
            except Exception as exc:
                debug_lines.append(
                    "Could not read {} track count on output timeline: {}".format(track_type, exc)
                )
                continue

            if track_count > 0:
                debug_lines.append(
                    "Output timeline already has {} {} track(s).".format(track_count, track_type)
                )
                continue

            try:
                created = timeline.AddTrack(track_type)
            except Exception as exc:
                debug_lines.append(
                    "Could not add {} track to output timeline: {}".format(track_type, exc)
                )
                continue

            debug_lines.append(
                "Added base {} track to output timeline: {}".format(track_type, created)
            )

    def _append_with_debug(
        self,
        media_pool,
        timeline,
        clip_info: Dict[str, Any],
        label: str,
        debug_lines: List[str],
    ):
        debug_lines.append(label)
        debug_lines.append("clipInfo: {}".format(self._clip_info_for_debug(clip_info)))
        try:
            result = media_pool.AppendToTimeline([clip_info])
        except Exception as exc:
            debug_lines.append("AppendToTimeline exception: {}".format(exc))
            result = None

        debug_lines.append("AppendToTimeline return: {}".format(self._append_result_for_debug(result)))
        debug_lines.append(
            "Output video item count immediately after append: {}".format(
                self._count_timeline_items(timeline, "video")
            )
        )
        return result

    def _append_whole_item_for_diagnostics(
        self,
        media_pool,
        timeline,
        media_pool_item,
        cut_entry: Dict[str, Any],
        fragment_index: int,
        debug_lines: List[str],
    ) -> bool:
        debug_lines.append("Attempt C1: whole MediaPoolItem diagnostic append as list")
        debug_lines.append(
            "MediaPoolItem: {}".format(self._safe_media_pool_item_name(media_pool_item))
        )
        try:
            result = media_pool.AppendToTimeline([media_pool_item])
        except Exception as exc:
            debug_lines.append("Attempt C1 exception: {}".format(exc))
            result = None

        debug_lines.append("Attempt C1 return: {}".format(self._append_result_for_debug(result)))
        debug_lines.append(
            "Output video item count immediately after Attempt C1: {}".format(
                self._count_timeline_items(timeline, "video")
            )
        )

        if result and self._count_timeline_items(timeline, "video") > 0:
            return self._cleanup_whole_item_diagnostic_clip(timeline, result, debug_lines, "Attempt C1")

        if result:
            debug_lines.append(
                "Attempt C1 returned items but output video count is still zero; trying direct whole-item call."
            )

        debug_lines.append("Attempt C2: whole MediaPoolItem diagnostic append as direct argument")
        try:
            direct_result = media_pool.AppendToTimeline(media_pool_item)
        except Exception as exc:
            debug_lines.append("Attempt C2 exception: {}".format(exc))
            direct_result = None

        debug_lines.append("Attempt C2 return: {}".format(self._append_result_for_debug(direct_result)))
        debug_lines.append(
            "Output video item count immediately after Attempt C2: {}".format(
                self._count_timeline_items(timeline, "video")
            )
        )

        if not direct_result:
            return False
        if self._count_timeline_items(timeline, "video") == 0:
            debug_lines.append(
                "Attempt C2 returned items but output video count is still zero; treating as failed."
            )
            return False

        return self._cleanup_whole_item_diagnostic_clip(timeline, direct_result, debug_lines, "Attempt C2")

    def _cleanup_whole_item_diagnostic_clip(
        self,
        timeline,
        result,
        debug_lines: List[str],
        label: str,
    ) -> bool:
        try:
            deleted = timeline.DeleteClips(result, False)
            debug_lines.append("{} diagnostic clip delete result: {}".format(label, deleted))
        except Exception as exc:
            debug_lines.append("{} diagnostic clip delete exception: {}".format(label, exc))

        debug_lines.append(
            "Output video item count after {} cleanup: {}".format(
                label,
                self._count_timeline_items(timeline, "video")
            )
        )
        return True

    def _append_timing_diagnostic_lines(
        self,
        cut_entry: Dict[str, Any],
        fragment: Dict[str, Any],
        clip_info: Dict[str, Any],
    ) -> List[str]:
        fps = float(cut_entry.get("timeline_fps") or 0.0)
        nominal_fps = int(cut_entry.get("nominal_timecode_fps") or 0)
        lines = [
            "Timing diagnostics",
            "  JSON start raw: {}".format(self._display_value(cut_entry.get("start_value"))),
            "  JSON end raw: {}".format(self._display_value(cut_entry.get("end_value"))),
            "  Start conversion method used: {}".format(
                cut_entry.get("start_conversion_method") or "unknown"
            ),
            "  End conversion method used: {}".format(
                cut_entry.get("end_conversion_method") or "unknown"
            ),
            "  Parsed start: {}".format(
                self._format_json_time_debug(cut_entry.get("start_time_debug") or {})
            ),
            "  Parsed end: {}".format(
                self._format_json_time_debug(cut_entry.get("end_time_debug") or {})
            ),
            "  Resolve real FPS used for numeric-seconds candidate: {}".format(
                "{:.6f}".format(fps) if fps > 0 else "unknown"
            ),
            "  Nominal display/timecode FPS candidate: {}".format(
                nominal_fps if nominal_fps > 0 else "unknown"
            ),
        ]

        if fps > 0:
            start_seconds = float(cut_entry.get("start_seconds") or 0.0)
            end_seconds = float(cut_entry.get("end_seconds") or 0.0)
            numeric_start = json_seconds_to_timeline_offset(
                start_seconds,
                fps,
                conversion_method="numeric-seconds",
            )
            numeric_end = json_seconds_to_timeline_offset(
                end_seconds,
                fps,
                conversion_method="numeric-seconds",
            )
            display_start = json_seconds_to_timeline_offset(
                start_seconds,
                fps,
                conversion_method="timecode-string",
            )
            display_end = json_seconds_to_timeline_offset(
                end_seconds,
                fps,
                conversion_method="timecode-string",
            )
            lines.extend(
                [
                    "  Numeric source-frame candidate [elapsed seconds * real fps): {}..{} duration={}".format(
                        numeric_start,
                        numeric_end,
                        numeric_end - numeric_start,
                    ),
                    "  Timecode/display-frame candidate [nominal fps): {}..{} duration={}".format(
                        display_start,
                        display_end,
                        display_end - display_start,
                    ),
                ]
            )

        timeline_overlap_start = int(fragment.get("timeline_overlap_start_frame", 0))
        source_start = int(fragment.get("source_start_frame", 0))
        source_timeline_to_media_delta = source_start - timeline_overlap_start
        lines.extend(
            [
                "  Source timeline range used for overlap [exclusive): {}..{}".format(
                    fragment.get("timeline_overlap_start_frame", "unknown"),
                    fragment.get("timeline_overlap_end_frame_exclusive", "unknown"),
                ),
                "  Source media frame delta from source timeline frame: {}".format(
                    source_timeline_to_media_delta
                ),
                "  Calculated source append startFrame/endFrame inclusive: {}..{}".format(
                    clip_info.get("startFrame"),
                    clip_info.get("endFrame"),
                ),
                "  Calculated source append duration from clipInfo: {}".format(
                    int(clip_info.get("endFrame", 0))
                    - int(clip_info.get("startFrame", 0))
                    + 1
                ),
            ]
        )
        return lines

    def _post_append_timeline_item_diagnostic_lines(
        self,
        appended_items,
        accepted_video_clip_info: Dict[str, Any],
        cut_entry: Dict[str, Any],
        fragment_index: int,
    ) -> List[str]:
        lines = [
            "Post-append Resolve TimelineItem diagnostics for EP{:02d} fragment {}".format(
                int(cut_entry.get("episode_number") or 0),
                int(fragment_index),
            )
        ]
        if not appended_items:
            lines.append("  No appended TimelineItems returned by Resolve.")
            return lines

        try:
            items = list(appended_items)
        except Exception:
            items = [appended_items]

        expected_duration = (
            int(accepted_video_clip_info.get("endFrame", 0))
            - int(accepted_video_clip_info.get("startFrame", 0))
            + 1
        )
        lines.append("  Expected duration from accepted clipInfo: {}".format(expected_duration))
        for item_index, item in enumerate(items, start=1):
            lines.extend(
                [
                    "  Returned item {} name: {}".format(
                        item_index,
                        self._safe_timeline_item_name(item),
                    ),
                    "  Returned item {} record start: {}".format(
                        item_index,
                        self._safe_timeline_item_start(item),
                    ),
                    "  Returned item {} record end inclusive: {}".format(
                        item_index,
                        self._safe_timeline_item_end(item),
                    ),
                    "  Returned item {} duration frames: {}".format(
                        item_index,
                        self._safe_timeline_item_duration(item),
                    ),
                    "  Returned item {} source start frame: {}".format(
                        item_index,
                        self._safe_timeline_item_source_start(item),
                    ),
                    "  Returned item {} source end frame: {}".format(
                        item_index,
                        self._safe_timeline_item_source_end(item),
                    ),
                ]
            )
        return lines

    def _debug_report_path(self) -> Path:
        if self.selected_path:
            selected = Path(self.selected_path).expanduser()
            return selected.with_suffix(".append_debug.txt")
        return Path.home() / "Desktop" / "ai_edit_import.append_debug.txt"

    def _audio_debug_report_path(self) -> Path:
        if self.selected_path:
            selected = Path(self.selected_path).expanduser()
            return selected.with_suffix(".audio_debug.txt")
        return Path.home() / "Desktop" / "ai_edit_import.audio_debug.txt"

    @staticmethod
    def _write_debug_report(path: Path, debug_lines: List[str]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(str(line) for line in debug_lines) + "\n", encoding="utf-8")
            print("\n".join(str(line) for line in debug_lines))
            print("[AI Edit Import] Append debug report saved: {}".format(path))
        except Exception as exc:
            print("[AI Edit Import] Could not write append debug report: {}".format(exc))

    @staticmethod
    def _safe_track_count(timeline, track_type: str) -> int:
        try:
            return int(timeline.GetTrackCount(track_type) or 0)
        except Exception:
            return 0

    @staticmethod
    def _clip_info_for_debug(clip_info: Dict[str, Any]) -> Dict[str, Any]:
        debug_copy: Dict[str, Any] = {}
        for key, value in clip_info.items():
            if key == "mediaPoolItem":
                debug_copy[key] = AIEditImportApp._safe_media_pool_item_name(value)
            else:
                debug_copy[key] = value
        return debug_copy

    @staticmethod
    def _append_result_for_debug(result) -> str:
        if not result:
            return repr(result)
        try:
            names = []
            for item in result:
                try:
                    names.append(item.GetName() or "Unnamed TimelineItem")
                except Exception:
                    names.append(repr(item))
            return "count={} items={}".format(len(result), names)
        except Exception:
            return repr(result)

    @staticmethod
    def _safe_media_pool_item_name(media_pool_item) -> str:
        try:
            return media_pool_item.GetName() or "Unnamed MediaPoolItem"
        except Exception:
            return "Unknown MediaPoolItem"

    @staticmethod
    def _safe_media_pool_item_duration(media_pool_item) -> str:
        try:
            return str(media_pool_item.GetClipProperty("Duration"))
        except Exception as exc:
            return "unavailable ({})".format(exc)

    @staticmethod
    def _get_media_file_path_or_unknown(media_pool_item) -> str:
        try:
            direct_value = media_pool_item.GetClipProperty("File Path")
            if isinstance(direct_value, str) and direct_value.strip():
                return direct_value.strip()
        except Exception:
            pass
        try:
            all_properties = media_pool_item.GetClipProperty() or {}
        except Exception:
            all_properties = {}
        if isinstance(all_properties, dict):
            for key in ("File Path", "FilePath", "Source File Path", "Source Path"):
                value = all_properties.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return "unknown"

    @staticmethod
    def _safe_timeline_item_name(timeline_item) -> str:
        try:
            return str(timeline_item.GetName() or "Unnamed TimelineItem")
        except Exception as exc:
            return "unknown ({})".format(exc)

    @staticmethod
    def _safe_timeline_item_start(timeline_item) -> str:
        try:
            return str(int(round(timeline_item.GetStart(False))))
        except Exception:
            return "unknown"

    @staticmethod
    def _safe_timeline_item_end(timeline_item) -> str:
        try:
            start = int(round(timeline_item.GetStart(False)))
            duration = int(round(timeline_item.GetDuration(False)))
            return str(start + duration - 1)
        except Exception:
            return "unknown"

    @staticmethod
    def _safe_timeline_item_duration(timeline_item) -> str:
        try:
            return str(int(round(timeline_item.GetDuration(False))))
        except Exception as exc:
            return "unknown ({})".format(exc)

    @staticmethod
    def _safe_timeline_item_source_start(timeline_item) -> str:
        try:
            return str(int(timeline_item.GetSourceStartFrame()))
        except Exception as exc:
            return "unknown ({})".format(exc)

    @staticmethod
    def _safe_timeline_item_source_end(timeline_item) -> str:
        for method_name in ("GetSourceEndFrame", "GetSourceEnd"):
            try:
                method = getattr(timeline_item, method_name, None)
                if method:
                    return str(int(method()))
            except Exception:
                pass
        return "unknown"

    @staticmethod
    def _delete_empty_timeline_if_possible(media_pool, timeline, debug_lines: List[str]) -> None:
        if not timeline or not hasattr(media_pool, "DeleteTimelines"):
            debug_lines.append("Could not delete failed output timeline: DeleteTimelines unavailable.")
            return
        try:
            deleted = media_pool.DeleteTimelines([timeline])
            debug_lines.append("Delete failed output timeline result: {}".format(deleted))
        except Exception as exc:
            debug_lines.append("Delete failed output timeline exception: {}".format(exc))

    def _verify_expected_record_frames(
        self,
        timeline,
        expected_starts: List[int],
    ) -> str:
        actual_starts: List[int] = []
        track_count = int(timeline.GetTrackCount("video") or 0)
        for track_index in range(1, track_count + 1):
            for item in timeline.GetItemListInTrack("video", track_index) or []:
                try:
                    actual_starts.append(int(round(item.GetStart(False))))
                except Exception:
                    pass

        if not actual_starts or not expected_starts:
            return "Warning: Could not verify whether Resolve preserved recordFrame gaps."

        actual_set = set(actual_starts)
        missing = [frame for frame in expected_starts if frame not in actual_set]
        if missing:
            preview = ", ".join(str(frame) for frame in missing[:5])
            return (
                "Warning: Resolve did not preserve requested 5-second empty gaps via recordFrame. "
                "Missing expected clip start frame(s): {}.".format(preview)
            )
        return ""

    def _add_markers_from_loaded_json(self) -> str:
        if self.loaded_json is None:
            raise ValueError("No JSON is loaded yet. Click 'Load JSON' first.")

        shape_name, source_items = extract_moments(self.loaded_json)
        if not source_items:
            raise ValueError(f"Detected JSON shape '{shape_name}' but it contains no items.")

        project = self._get_current_project()
        timeline = project.GetCurrentTimeline()
        if not timeline:
            raise RuntimeError("No active timeline is open in Resolve.")

        fps_info = self._get_timeline_fps_info(project, timeline)
        fps = float(fps_info["float"])
        fps_raw = fps_info["raw"]
        nominal_timecode_fps = self._nominal_timecode_fps_from_fps_info(fps_info)
        fps_inspection = self._validate_json_fps_or_raise(self.loaded_json, fps_info)
        timeline_start_frame = int(timeline.GetStartFrame() or 0)
        valid_entries, skipped_count, errors = self._prepare_marker_entries(
            source_items,
            fps=fps,
        )
        if not valid_entries:
            raise ValueError(
                f"Detected JSON shape '{shape_name}', but no valid marker items were found."
            )

        added_count = 0
        for marker_number, entry in enumerate(valid_entries, start=1):
            start_seconds = entry["start_seconds"]
            end_seconds = entry["end_seconds"]
            marker_frame = json_seconds_to_timeline_offset(
                start_seconds,
                fps,
                conversion_method=entry.get("start_conversion_method") or "numeric-seconds",
            )
            marker_duration = 1
            if end_seconds is not None and end_seconds > start_seconds:
                marker_end_frame = json_seconds_to_timeline_offset(
                    end_seconds,
                    fps,
                    conversion_method=entry.get("end_conversion_method") or "numeric-seconds",
                )
                marker_duration = max(1, marker_end_frame - marker_frame)

            marker_name = self._build_marker_name(marker_number, entry["description"])
            marker_note = "\n".join(
                [
                    f"Description: {entry['description']}",
                    f"Start: {self._display_value(entry['start_value'])}",
                    f"End: {self._display_value(entry['end_value'])}",
                ]
            )
            try:
                success = timeline.AddMarker(
                    marker_frame,
                    MARKER_COLOR,
                    marker_name,
                    marker_note,
                    marker_duration,
                    f"{MARKER_CUSTOM_PREFIX}{marker_number}",
                )
            except Exception as exc:
                success = False
                errors.append(
                    f"Moment {entry['source_index']}: Resolve error while adding marker ({exc})."
                )

            if success:
                added_count += 1
            else:
                skipped_count += 1
                errors.append(f"Moment {entry['source_index']}: Resolve failed to add marker.")

        lines = [
            "Markers finished.",
            f"Detected JSON shape: {shape_name}",
            f"Timeline: {timeline.GetName()}",
            f"Timeline FPS: {fps_raw}",
            f"JSON timecode nominal FPS: {nominal_timecode_fps}",
            "Resolve FPS canonical: {}".format(self._format_fps_info(fps_info)),
            "JSON timeline FPS: {}".format(
                self._format_fps_info(fps_inspection["timeline_fps_info"])
            ),
            "JSON source FPS: {}".format(
                self._format_fps_info(fps_inspection["source_fps_info"])
            ),
            "FPS match: {}".format(
                "yes"
                if (
                    fps_inspection["source_fps_info"]
                    or fps_inspection["timeline_fps_info"]
                )
                else "not checked (JSON has no FPS metadata)"
            ),
            f"Timeline start frame: {timeline_start_frame}",
            "Marker frame mode: timeline-relative offsets",
            f"Source items found: {len(source_items)}",
            f"Valid marker items: {len(valid_entries)}",
            f"Markers added: {added_count}",
            f"Skipped items: {skipped_count}",
        ]
        warning = self._timeline_start_warning(timeline_start_frame)
        if warning:
            lines.append(warning)
        if errors:
            lines.append("Warnings/errors:")
            lines.extend(errors[:8])
            if len(errors) > 8:
                lines.append(f"...and {len(errors) - 8} more.")
        return "\n".join(lines)

    def _get_current_project(self):
        project_manager = self.resolve.GetProjectManager()
        if not project_manager:
            raise RuntimeError("Could not access Resolve Project Manager.")
        project = project_manager.GetCurrentProject()
        if not project:
            raise RuntimeError("No project is currently open in Resolve.")
        return project

    @staticmethod
    def _parse_fps_info(
        raw_fps: Any,
        fps_num: Any = None,
        fps_den: Any = None,
    ) -> Dict[str, Any]:
        raw_text = "" if raw_fps is None else str(raw_fps).strip()

        if fps_num is not None or fps_den is not None:
            try:
                numerator = int(fps_num)
                denominator = int(fps_den)
            except Exception as exc:
                raise RuntimeError(
                    "Invalid FPS numerator/denominator metadata: {}/{}".format(
                        fps_num,
                        fps_den,
                    )
                ) from exc
            if numerator <= 0 or denominator <= 0:
                raise RuntimeError(
                    "FPS numerator/denominator must be positive. Got {}/{}.".format(
                        fps_num,
                        fps_den,
                    )
                )
            fps_fraction = Fraction(numerator, denominator)
        else:
            fps_text = raw_text.upper().replace("DF", "").replace("NDF", "").strip()
            fps_text = fps_text.replace("FPS", "").strip()
            fraction_match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", fps_text)
            if fraction_match:
                numerator = int(fraction_match.group(1))
                denominator = int(fraction_match.group(2))
                if numerator <= 0 or denominator <= 0:
                    raise RuntimeError(f"Unrecognized FPS value: {raw_fps}")
                fps_fraction = Fraction(numerator, denominator)
            else:
                numeric_match = re.search(r"\d+(?:\.\d+)?", fps_text)
                if not numeric_match:
                    raise RuntimeError(f"Unrecognized FPS value: {raw_fps}")
                numeric_text = numeric_match.group(0)
                try:
                    numeric_value = float(numeric_text)
                except Exception as exc:
                    raise RuntimeError(f"Unrecognized FPS value: {raw_fps}") from exc
                fps_fraction = None
                for _label, common_fraction, aliases in COMMON_FPS_RATES:
                    alias_values = [float(alias) for alias in aliases]
                    alias_values.append(float(common_fraction))
                    if any(
                        abs(numeric_value - alias_value) <= FPS_PARSE_TOLERANCE
                        for alias_value in alias_values
                    ):
                        fps_fraction = common_fraction
                        break
                if fps_fraction is None:
                    fps_fraction = Fraction(numeric_text)

        if fps_fraction <= 0:
            raise RuntimeError(f"Timeline FPS must be greater than zero. Got {raw_fps}")

        canonical_label = "{:.6f}".format(float(fps_fraction)).rstrip("0").rstrip(".")
        for label, common_fraction, _aliases in COMMON_FPS_RATES:
            if fps_fraction == common_fraction:
                canonical_label = label
                break

        return {
            "raw": raw_fps,
            "label": canonical_label,
            "num": int(fps_fraction.numerator),
            "den": int(fps_fraction.denominator),
            "fraction": fps_fraction,
            "float": float(fps_fraction),
        }

    @classmethod
    def _get_timeline_fps_info(cls, project, timeline) -> Dict[str, Any]:
        fps_raw = timeline.GetSetting("timelineFrameRate") or project.GetSetting(
            "timelineFrameRate"
        )
        if not fps_raw:
            raise RuntimeError("Could not read timeline frame rate.")

        return cls._parse_fps_info(fps_raw)

    @classmethod
    def _get_timeline_fps(cls, project, timeline) -> Tuple[float, Any]:
        fps_info = cls._get_timeline_fps_info(project, timeline)
        return float(fps_info["float"]), fps_info["raw"]

    @staticmethod
    def _nominal_timecode_fps_from_fps(fps: Any) -> int:
        fps_value = float(fps)
        if fps_value <= 0:
            raise RuntimeError(f"Timeline FPS must be greater than zero. Got {fps}")

        nominal_pairs = (
            (Fraction(24000, 1001), 24),
            (Fraction(30000, 1001), 30),
            (Fraction(48000, 1001), 48),
            (Fraction(60000, 1001), 60),
            (Fraction(120000, 1001), 120),
        )
        for fractional_fps, nominal_fps in nominal_pairs:
            if abs(fps_value - float(fractional_fps)) <= FPS_PARSE_TOLERANCE:
                return nominal_fps

        return max(1, int(round(fps_value)))

    @classmethod
    def _nominal_timecode_fps_from_fps_info(cls, fps_info: Dict[str, Any]) -> int:
        return cls._nominal_timecode_fps_from_fps(fps_info["float"])

    @classmethod
    def _json_time_debug_info(
        cls,
        value: Any,
        fps: Optional[float] = None,
        conversion_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "raw": value,
            "kind": type(value).__name__,
            "components": "",
            "conversion_method": conversion_method or cls._json_time_conversion_method(value),
            "nominal_timecode_fps": (
                cls._nominal_timecode_fps_from_fps(fps) if fps is not None else None
            ),
        }
        if value is None:
            return info
        if isinstance(value, (int, float)):
            info["kind"] = "numeric-seconds"
            return info

        text = str(value).strip()
        if cls._json_time_conversion_method(value) == "numeric-seconds":
            info["kind"] = "numeric-seconds"
            return info

        short_match = re.fullmatch(r"(\d{1,2}):(\d{2})(?:([.,])(\d+))?", text)
        if short_match:
            minutes = int(short_match.group(1))
            seconds = int(short_match.group(2))
            separator = short_match.group(3)
            fraction_text = short_match.group(4)
            info["kind"] = "MM:SS.mmm" if separator else "MM:SS"
            if fraction_text is not None:
                info["components"] = "minutes={}, seconds={}, fraction={}".format(
                    minutes,
                    seconds,
                    fraction_text,
                )
            else:
                info["components"] = "minutes={}, seconds={}".format(
                    minutes,
                    seconds,
                )
            return info

        match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})(?:([:.,])(\d+))?", text)
        if not match:
            info["kind"] = "unrecognized"
            return info

        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        separator = match.group(4)
        fraction_text = match.group(5)
        if separator == ":":
            info["kind"] = "HH:MM:SS:FF"
            info["components"] = "hours={}, minutes={}, seconds={}, frames={}".format(
                hours,
                minutes,
                seconds,
                int(fraction_text),
            )
        elif separator in (".", ","):
            info["kind"] = "HH:MM:SS.mmm"
            info["components"] = "hours={}, minutes={}, seconds={}, fraction={}".format(
                hours,
                minutes,
                seconds,
                fraction_text,
            )
        else:
            info["kind"] = "HH:MM:SS"
            info["components"] = "hours={}, minutes={}, seconds={}".format(
                hours,
                minutes,
                seconds,
            )
        return info

    @staticmethod
    def _json_time_conversion_method(value: Any) -> str:
        if isinstance(value, str) and ":" in value.strip():
            return "timecode-string"
        return "numeric-seconds"

    @classmethod
    def _coerce_json_time_value(
        cls,
        value: Any,
        fps: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        seconds = cls._coerce_seconds(value, fps=fps)
        if seconds is None:
            return None
        conversion_method = cls._json_time_conversion_method(value)
        return {
            "seconds": seconds,
            "conversion_method": conversion_method,
            "debug": cls._json_time_debug_info(
                value,
                fps=fps,
                conversion_method=conversion_method,
            ),
        }

    @staticmethod
    def _format_json_time_debug(info: Dict[str, Any]) -> str:
        if not info:
            return ""
        parts = ["kind={}".format(info.get("kind") or "unknown")]
        if info.get("conversion_method"):
            parts.append("method={}".format(info["conversion_method"]))
        if info.get("components"):
            parts.append(str(info["components"]))
        if (
            info.get("nominal_timecode_fps")
            and info.get("conversion_method") == "timecode-string"
        ):
            parts.append("nominal_fps={}".format(info["nominal_timecode_fps"]))
        return "; ".join(parts)

    @staticmethod
    def _extract_json_metadata(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            return metadata

        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("metadata"), dict):
            return data["metadata"]

        return {}

    @classmethod
    def _metadata_fps_info(
        cls,
        metadata: Dict[str, Any],
        prefix: str,
    ) -> Optional[Dict[str, Any]]:
        fps_value = metadata.get(f"{prefix}_fps")
        fps_num = metadata.get(f"{prefix}_fps_num")
        fps_den = metadata.get(f"{prefix}_fps_den")
        if fps_value is None and fps_num is None and fps_den is None:
            return None
        return cls._parse_fps_info(fps_value, fps_num=fps_num, fps_den=fps_den)

    @staticmethod
    def _fps_infos_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return Fraction(int(left["num"]), int(left["den"])) == Fraction(
            int(right["num"]),
            int(right["den"]),
        )

    @staticmethod
    def _format_fps_info(fps_info: Optional[Dict[str, Any]]) -> str:
        if not fps_info:
            return "not provided"
        raw_value = fps_info.get("raw")
        raw_text = "" if raw_value is None else str(raw_value)
        base = "{} ({}/{}, {:.6f})".format(
            fps_info["label"],
            fps_info["num"],
            fps_info["den"],
            fps_info["float"],
        )
        if raw_text and raw_text != str(fps_info["label"]):
            return "{} raw={}".format(base, raw_text)
        return base

    @classmethod
    def _inspect_json_fps_metadata(
        cls,
        payload: Any,
        resolve_fps_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metadata = cls._extract_json_metadata(payload)
        errors: List[str] = []
        mismatches: List[str] = []

        source_fps_info = None
        timeline_fps_info = None
        try:
            source_fps_info = cls._metadata_fps_info(metadata, "source")
        except Exception as exc:
            errors.append("Invalid JSON source_fps metadata: {}".format(exc))
        try:
            timeline_fps_info = cls._metadata_fps_info(metadata, "timeline")
        except Exception as exc:
            errors.append("Invalid JSON timeline_fps metadata: {}".format(exc))

        if source_fps_info and timeline_fps_info and not cls._fps_infos_match(
            source_fps_info,
            timeline_fps_info,
        ):
            mismatches.append(
                "JSON source_fps ({}) does not match JSON timeline_fps ({}).".format(
                    cls._format_fps_info(source_fps_info),
                    cls._format_fps_info(timeline_fps_info),
                )
            )

        if resolve_fps_info:
            for label, fps_info in (
                ("timeline_fps", timeline_fps_info),
                ("source_fps", source_fps_info),
            ):
                if fps_info and not cls._fps_infos_match(resolve_fps_info, fps_info):
                    mismatches.append(
                        "JSON {} ({}) does not match active Resolve timeline FPS ({}).".format(
                            label,
                            cls._format_fps_info(fps_info),
                            cls._format_fps_info(resolve_fps_info),
                        )
                    )

        return {
            "metadata_present": bool(metadata),
            "source_fps_info": source_fps_info,
            "timeline_fps_info": timeline_fps_info,
            "errors": errors,
            "mismatches": mismatches,
            "fps_match": not errors and not mismatches,
        }

    @classmethod
    def _validate_json_fps_or_raise(
        cls,
        payload: Any,
        resolve_fps_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        inspection = cls._inspect_json_fps_metadata(payload, resolve_fps_info)
        if inspection["errors"] or inspection["mismatches"]:
            details = list(inspection["errors"]) + list(inspection["mismatches"])
            raise RuntimeError(
                "JSON FPS metadata validation failed before modifying the timeline.\n\n"
                + "\n".join(details)
            )
        return inspection

    @classmethod
    def _fps_status_lines(
        cls,
        payload: Any,
        resolve_fps_info: Optional[Dict[str, Any]] = None,
        inspection: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        if inspection is None:
            inspection = cls._inspect_json_fps_metadata(payload, resolve_fps_info)

        lines = [
            "Resolve timeline FPS: {}".format(cls._format_fps_info(resolve_fps_info)),
            "JSON metadata: {}".format(
                "present" if inspection["metadata_present"] else "not present"
            ),
            "JSON timeline FPS: {}".format(
                cls._format_fps_info(inspection["timeline_fps_info"])
            ),
            "JSON source FPS: {}".format(
                cls._format_fps_info(inspection["source_fps_info"])
            ),
        ]

        if inspection["errors"]:
            lines.append("FPS metadata errors: {}".format(len(inspection["errors"])))
            lines.extend(inspection["errors"][:4])
        elif inspection["mismatches"]:
            lines.append("FPS match: no")
            lines.extend(inspection["mismatches"][:4])
        elif inspection["source_fps_info"] or inspection["timeline_fps_info"]:
            lines.append("FPS match: yes")
        else:
            lines.append("FPS match: not checked (JSON has no FPS metadata)")

        return lines

    @staticmethod
    def _timeline_start_warning(timeline_start_frame: int) -> str:
        if int(timeline_start_frame) == 0:
            return ""
        return (
            "Warning: Source timeline does not start at 00:00:00:00.\n"
            "JSON timecode labels are interpreted as offsets from the source timeline start for source lookup."
        )

    def _collect_video_track_items(self, source_timeline) -> List[Dict[str, Any]]:
        video_tracks: List[Dict[str, Any]] = []
        video_track_count = int(source_timeline.GetTrackCount("video") or 0)
        if video_track_count <= 0:
            raise RuntimeError("The current source timeline has no video tracks.")

        for track_index in range(1, video_track_count + 1):
            try:
                if hasattr(source_timeline, "GetIsTrackEnabled") and not source_timeline.GetIsTrackEnabled("video", track_index):
                    continue
            except Exception:
                pass

            prepared_items: List[Dict[str, Any]] = []
            for item in source_timeline.GetItemListInTrack("video", track_index) or []:
                media_pool_item = item.GetMediaPoolItem()
                if not media_pool_item:
                    continue
                try:
                    timeline_item_start = int(round(item.GetStart(False)))
                    timeline_item_duration = int(round(item.GetDuration(False)))
                    source_start_frame = int(item.GetSourceStartFrame())
                except Exception:
                    continue
                if timeline_item_duration <= 0:
                    continue

                prepared_items.append(
                    {
                        "timeline_item": item,
                        "media_pool_item": media_pool_item,
                        "timeline_start_frame": timeline_item_start,
                        "timeline_end_frame_exclusive": timeline_item_start
                        + timeline_item_duration,
                        "source_start_frame": source_start_frame,
                        "item_name": item.GetName() or "Unnamed Clip",
                    }
                )

            prepared_items.sort(
                key=lambda clip: (
                    clip["timeline_start_frame"],
                    clip["timeline_end_frame_exclusive"],
                )
            )
            if prepared_items:
                video_tracks.append({"track_index": track_index, "items": prepared_items})

        if not video_tracks:
            raise RuntimeError("No usable media-backed video clips were found.")
        return video_tracks

    @staticmethod
    def _compute_track_fragments_for_cut(
        cut_entry: Dict[str, Any],
        track: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], int]:
        fragments: List[Dict[str, Any]] = []
        total_covered_frames = 0
        cut_start = cut_entry["timeline_start_frame"]
        cut_end_exclusive = cut_entry["timeline_end_frame_exclusive"]

        for item in track["items"]:
            overlap_start = max(cut_start, item["timeline_start_frame"])
            overlap_end_exclusive = min(
                cut_end_exclusive,
                item["timeline_end_frame_exclusive"],
            )
            if overlap_end_exclusive <= overlap_start:
                continue

            overlap_duration = overlap_end_exclusive - overlap_start
            source_overlap_start = item["source_start_frame"] + (
                overlap_start - item["timeline_start_frame"]
            )
            source_overlap_end_inclusive = source_overlap_start + overlap_duration - 1
            fragments.append(
                {
                    "media_pool_item": item["media_pool_item"],
                    "timeline_item": item["timeline_item"],
                    "timeline_overlap_start_frame": int(overlap_start),
                    "timeline_overlap_end_frame_exclusive": int(overlap_end_exclusive),
                    "source_start_frame": int(source_overlap_start),
                    "source_end_frame_inclusive": int(source_overlap_end_inclusive),
                    "duration_frames": int(overlap_duration),
                    "source_item_name": item["item_name"],
                    "track_index": track["track_index"],
                }
            )
            total_covered_frames += overlap_duration
        return fragments, total_covered_frames

    def _choose_track_for_cut(
        self,
        cut_entry: Dict[str, Any],
        video_tracks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        best_choice: Optional[Dict[str, Any]] = None
        for track in video_tracks:
            fragments, coverage = self._compute_track_fragments_for_cut(cut_entry, track)
            if coverage <= 0:
                continue
            candidate = {
                "track_index": track["track_index"],
                "fragments": fragments,
                "coverage": coverage,
            }
            if best_choice is None or coverage > best_choice["coverage"]:
                best_choice = candidate
            elif coverage == best_choice["coverage"] and track["track_index"] < best_choice["track_index"]:
                best_choice = candidate

        if best_choice is None:
            raise RuntimeError(
                f"Cut {cut_entry['index']:02d} is not covered by any usable video clip."
            )
        if best_choice["coverage"] < cut_entry["duration_frames"]:
            trimmed_entry = self._trim_small_missing_tail_if_safe(cut_entry, best_choice)
            if trimmed_entry is None:
                raise RuntimeError(
                    "Cut {:02d} is only partially covered (coverage={} frames, needed={} frames).".format(
                        cut_entry["index"],
                        best_choice["coverage"],
                        cut_entry["duration_frames"],
                    )
                )
            best_choice["cut_entry"] = trimmed_entry
        return best_choice

    @staticmethod
    def _trim_small_missing_tail_if_safe(
        cut_entry: Dict[str, Any],
        chosen_track: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        fragments = chosen_track.get("fragments") or []
        coverage = int(chosen_track.get("coverage") or 0)
        needed = int(cut_entry["duration_frames"])
        missing_tail_frames = needed - coverage
        if missing_tail_frames <= 0 or not fragments:
            return None

        tolerance_frames = max(1, int(round(float(cut_entry.get("timeline_fps") or 0.0))))
        if missing_tail_frames > tolerance_frames:
            return None

        first_start = min(int(fragment["timeline_overlap_start_frame"]) for fragment in fragments)
        last_end = max(int(fragment["timeline_overlap_end_frame_exclusive"]) for fragment in fragments)
        requested_start = int(cut_entry["timeline_start_frame"])
        requested_end = int(cut_entry["timeline_end_frame_exclusive"])
        if first_start != requested_start:
            return None
        if last_end >= requested_end:
            return None
        if requested_end - last_end != missing_tail_frames:
            return None
        if coverage != last_end - requested_start:
            return None

        episode_number = int(cut_entry["episode_number"])
        warning = (
            "EP{:02d} trimmed by {} frames because JSON end exceeded available source coverage.".format(
                episode_number,
                missing_tail_frames,
            )
        )
        trimmed_entry = dict(cut_entry)
        trimmed_entry["was_trimmed"] = True
        trimmed_entry["trimmed_tail_frames"] = int(missing_tail_frames)
        trimmed_entry["trim_warning"] = warning
        trimmed_entry["original_timeline_end_frame_exclusive"] = int(requested_end)
        trimmed_entry["original_timeline_end_frame_inclusive"] = int(requested_end - 1)
        trimmed_entry["timeline_end_frame_exclusive"] = int(last_end)
        trimmed_entry["timeline_end_frame_inclusive"] = int(last_end - 1)
        trimmed_entry["duration_frames"] = int(coverage)
        trimmed_entry["source_end_offset_real_fps"] = int(
            max(
                0,
                int(trimmed_entry.get("source_end_offset_real_fps") or 0)
                - missing_tail_frames,
            )
        )
        trimmed_entry["display_end_offset_nominal_fps"] = int(
            max(
                0,
                int(trimmed_entry.get("display_end_offset_nominal_fps") or 0)
                - missing_tail_frames,
            )
        )
        return trimmed_entry

    def _resolve_cut_fragments_safely(
        self,
        cut_entries: List[Dict[str, Any]],
        video_tracks: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        resolved_entries: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for cut_entry in cut_entries:
            try:
                chosen_track = self._choose_track_for_cut(cut_entry, video_tracks)
            except Exception as exc:
                warnings.append(
                    "EP{:02d}: skipped because source range is not fully covered ({}).".format(
                        cut_entry["episode_number"],
                        exc,
                    )
                )
                continue
            resolved_entries.append(
                {
                    **chosen_track.get("cut_entry", cut_entry),
                    "selected_track_index": chosen_track["track_index"],
                    "fragments": chosen_track["fragments"],
                }
            )
            trim_warning = chosen_track.get("cut_entry", cut_entry).get("trim_warning")
            if trim_warning:
                warnings.append(trim_warning)
        return resolved_entries, warnings

    def _set_current_timeline_or_raise(self, project, timeline, timeline_name: str):
        if not project.SetCurrentTimeline(timeline):
            raise RuntimeError(
                "Resolve created '{}' but could not set it as current.".format(timeline_name)
            )
        try:
            timeline.SetStartTimecode("00:00:00:00")
        except Exception:
            pass
        return timeline

    def _add_episode_markers_to_edited_timeline(
        self,
        timeline,
        placed_segments: List[Dict[str, Any]],
        fps: float,
    ) -> Tuple[int, List[str]]:
        range_markers_added = 0
        warnings: List[str] = []

        for placed in placed_segments:
            cut_entry = placed["cut_entry"]
            episode_number = int(cut_entry["episode_number"])
            start_frame = int(placed["record_start_frame"])
            duration_frames = max(1, int(placed["duration_frames"]))
            end_frame_exclusive = start_frame + duration_frames
            json_in = self._display_value(cut_entry["start_value"])
            json_out = self._display_value(cut_entry["end_value"])
            source_start_offset = int(cut_entry.get("source_start_offset_real_fps") or 0)
            source_end_offset = int(cut_entry.get("source_end_offset_real_fps") or 0)
            source_dvr_in = self._frame_to_timecode(source_start_offset, fps)
            source_dvr_out_exclusive = self._frame_to_timecode(source_end_offset, fps)
            trim_warning = cut_entry.get("trim_warning") or ""

            range_note_lines = [
                "EP{:02d} range".format(episode_number),
                "JSON {} -> {}".format(json_in, json_out),
                "SOURCE DVR {} -> {} excl".format(
                    source_dvr_in,
                    source_dvr_out_exclusive,
                ),
            ]
            if trim_warning:
                range_note_lines.append(
                    "TRIMMED -{}f".format(cut_entry.get("trimmed_tail_frames") or 0)
                )
            range_note = "\n".join(range_note_lines)

            try:
                range_success = timeline.AddMarker(
                    start_frame,
                    SEGMENT_RANGE_MARKER_COLOR,
                    "EP{:02d} | RANGE".format(episode_number),
                    range_note,
                    duration_frames,
                    "{}range:{:02d}".format(EDIT_MARKER_CUSTOM_PREFIX, episode_number),
                )
            except Exception as exc:
                range_success = False
                warnings.append(f"EP{episode_number:02d}: range marker error ({exc}).")
            if range_success:
                range_markers_added += 1
            else:
                warnings.append(f"EP{episode_number:02d}: Resolve failed to add range marker.")

        return range_markers_added, warnings

    @staticmethod
    def _count_timeline_items(timeline, track_type: str) -> int:
        total_items = 0
        track_count = int(timeline.GetTrackCount(track_type) or 0)
        for track_index in range(1, track_count + 1):
            total_items += len(timeline.GetItemListInTrack(track_type, track_index) or [])
        return total_items

    @staticmethod
    def _unique_timeline_name(project, requested_name: str) -> str:
        existing_names = set()
        try:
            timeline_count = int(project.GetTimelineCount() or 0)
        except Exception:
            timeline_count = 0
        for index in range(1, timeline_count + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline:
                existing_names.add(str(timeline.GetName()))
        if requested_name not in existing_names:
            return requested_name
        suffix = 2
        while True:
            candidate = "{}_{}".format(requested_name, suffix)
            if candidate not in existing_names:
                return candidate
            suffix += 1

    @staticmethod
    def _first_present_value(
        item: Dict[str, Any],
        field_names: Tuple[str, ...],
        default: Any = None,
    ) -> Any:
        for field_name in field_names:
            value = item.get(field_name)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return default

    def _prepare_marker_entries(
        self,
        source_items: List[Any],
        fps: Optional[float] = None,
    ) -> Tuple[List[Dict[str, Any]], int, List[str]]:
        valid_entries: List[Dict[str, Any]] = []
        skipped_count = 0
        errors: List[str] = []

        for source_index, item in enumerate(source_items, start=1):
            if not isinstance(item, dict):
                skipped_count += 1
                errors.append(f"Moment {source_index}: item is not an object.")
                continue

            start_value = self._first_present_value(item, START_FIELDS)
            start_time_info = self._coerce_json_time_value(start_value, fps=fps)
            if start_time_info is None:
                skipped_count += 1
                errors.append(f"Moment {source_index}: missing or invalid start time.")
                continue
            start_seconds = start_time_info["seconds"]

            end_value = self._first_present_value(item, END_FIELDS)
            duration_value = self._first_present_value(item, ("duration_seconds",))
            end_time_info = self._coerce_json_time_value(end_value, fps=fps)
            end_seconds = end_time_info["seconds"] if end_time_info else None
            if end_seconds is None and duration_value is not None:
                duration_seconds = self._coerce_seconds(duration_value, fps=fps)
                if duration_seconds is not None:
                    end_seconds = start_seconds + duration_seconds
                    end_value = end_seconds
                    end_time_info = {
                        "seconds": end_seconds,
                        "conversion_method": start_time_info["conversion_method"],
                        "debug": self._json_time_debug_info(
                            end_value,
                            fps=fps,
                            conversion_method=start_time_info["conversion_method"],
                        ),
                    }

            if end_seconds is not None and end_seconds <= start_seconds:
                errors.append(
                    f"Moment {source_index}: end time is not after start time; marker will be 1 frame."
                )
                end_seconds = None

            description_value = self._first_present_value(
                item,
                MARKER_DESCRIPTION_FIELDS,
                default="No description provided",
            )
            valid_entries.append(
                {
                    "source_index": source_index,
                    "start_value": start_value,
                    "start_seconds": start_seconds,
                    "start_conversion_method": start_time_info["conversion_method"],
                    "start_time_debug": start_time_info["debug"],
                    "end_value": end_value,
                    "end_seconds": end_seconds,
                    "end_conversion_method": (
                        end_time_info["conversion_method"] if end_time_info else None
                    ),
                    "end_time_debug": end_time_info["debug"] if end_time_info else {},
                    "description": str(description_value).strip()
                    or "No description provided",
                }
            )
        return valid_entries, skipped_count, errors

    def _prepare_cut_entries(
        self,
        source_items: List[Any],
        fps: Optional[float] = None,
        timeline_start_frame: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int, List[str]]:
        valid_entries: List[Dict[str, Any]] = []
        skipped_count = 0
        errors: List[str] = []

        for source_index, item in enumerate(source_items, start=1):
            if not isinstance(item, dict):
                skipped_count += 1
                errors.append(f"Moment {source_index}: item is not an object.")
                continue

            start_value = self._first_present_value(item, START_FIELDS)
            end_value = self._first_present_value(item, END_FIELDS)
            duration_value = self._first_present_value(item, ("duration_seconds",))
            number_value = self._first_present_value(item, NUMBER_FIELDS)
            segment_id_value = self._first_present_value(
                item,
                ("segment_id", "id", "episode_id"),
            )
            description_value = self._first_present_value(
                item,
                DESCRIPTION_FIELDS,
                default="No description provided",
            )
            description = str(description_value).strip() or "No description provided"
            editorial_reason_value = self._first_present_value(item, EDITORIAL_REASON_FIELDS)
            editorial_reason = (
                str(editorial_reason_value).strip()
                if editorial_reason_value is not None
                else ""
            )
            title_value = self._first_present_value(item, TITLE_FIELDS)
            title_text = self._build_title_text(
                title_value,
                description,
                fallback_number=len(valid_entries) + 1,
            )
            episode_number = self._coerce_episode_number(
                number_value,
                fallback=len(valid_entries) + 1,
            )

            start_time_info = self._coerce_json_time_value(start_value, fps=fps)
            if start_time_info is None:
                skipped_count += 1
                errors.append(f"Moment {source_index}: missing or invalid start time.")
                continue
            start_seconds = start_time_info["seconds"]

            end_time_info = self._coerce_json_time_value(end_value, fps=fps)
            end_seconds = end_time_info["seconds"] if end_time_info else None
            if end_seconds is None and duration_value is not None:
                duration_seconds = self._coerce_seconds(duration_value, fps=fps)
                if duration_seconds is not None:
                    end_seconds = start_seconds + duration_seconds
                    end_value = end_seconds
                    end_time_info = {
                        "seconds": end_seconds,
                        "conversion_method": start_time_info["conversion_method"],
                        "debug": self._json_time_debug_info(
                            end_value,
                            fps=fps,
                            conversion_method=start_time_info["conversion_method"],
                        ),
                    }

            if end_seconds is None:
                skipped_count += 1
                errors.append(
                    f"Moment {source_index}: missing or invalid end time and no valid duration_seconds."
                )
                continue
            if end_seconds <= start_seconds:
                skipped_count += 1
                errors.append(f"Moment {source_index}: end time must be after start time.")
                continue

            if fps is None:
                skipped_count += 1
                errors.append(
                    f"Moment {source_index}: cannot convert seconds to frames without timeline FPS."
                )
                continue
            nominal_timecode_fps = self._nominal_timecode_fps_from_fps(fps)
            source_start_offset_real_fps = json_seconds_to_timeline_offset(
                start_seconds,
                fps,
                conversion_method="numeric-seconds",
            )
            source_end_offset_real_fps = json_seconds_to_timeline_offset(
                end_seconds,
                fps,
                conversion_method="numeric-seconds",
            )
            display_start_offset_nominal_fps = json_seconds_to_timeline_offset(
                start_seconds,
                fps,
                conversion_method="timecode-string",
            )
            display_end_offset_nominal_fps = json_seconds_to_timeline_offset(
                end_seconds,
                fps,
                conversion_method="timecode-string",
            )
            source_start = json_seconds_to_source_timeline_frame(
                start_seconds,
                fps,
                int(timeline_start_frame),
                conversion_method="numeric-seconds",
            )
            source_end_exclusive = json_seconds_to_source_timeline_frame(
                end_seconds,
                fps,
                int(timeline_start_frame),
                conversion_method="numeric-seconds",
            )
            duration_frames = source_end_exclusive - source_start
            if duration_frames <= 0:
                skipped_count += 1
                errors.append(f"Moment {source_index}: converted to zero frames.")
                continue

            cut_number = len(valid_entries) + 1
            valid_entries.append(
                {
                    "index": cut_number,
                    "episode_number": episode_number,
                    "segment_id": str(segment_id_value).strip()
                    if segment_id_value is not None
                    else "EP{:02d}".format(episode_number),
                    "source_index": source_index,
                    "start_value": start_value,
                    "end_value": end_value,
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "start_conversion_method": start_time_info["conversion_method"],
                    "end_conversion_method": (
                        end_time_info["conversion_method"] if end_time_info else None
                    ),
                    "source_frame_conversion_method": "numeric-seconds",
                    "source_start_offset_real_fps": int(source_start_offset_real_fps),
                    "source_end_offset_real_fps": int(source_end_offset_real_fps),
                    "display_start_offset_nominal_fps": int(display_start_offset_nominal_fps),
                    "display_end_offset_nominal_fps": int(display_end_offset_nominal_fps),
                    "description": description,
                    "editorial_reason": editorial_reason,
                    "qa_notes": editorial_reason,
                    "title_text": title_text,
                    "segment_name": self._build_cut_name(episode_number, title_text),
                    "timeline_start_frame": int(source_start),
                    "timeline_end_frame_exclusive": int(source_end_exclusive),
                    "timeline_end_frame_inclusive": int(source_end_exclusive - 1),
                    "duration_frames": int(duration_frames),
                    "timeline_fps": float(fps),
                    "nominal_timecode_fps": int(nominal_timecode_fps),
                    "start_time_debug": start_time_info["debug"],
                    "end_time_debug": end_time_info["debug"] if end_time_info else {},
                }
            )

        return valid_entries, skipped_count, errors

    @classmethod
    def _normalize_timeline_label_times_for_cutting(
        cls,
        source_items: List[Any],
        *,
        payload: Any,
        fps: float,
        source_timeline_start_frame: int,
        source_timeline_start_timecode: str = "",
        source_timeline_duration_seconds: Optional[float] = None,
    ) -> Tuple[List[Any], Dict[str, Any]]:
        info: Dict[str, Any] = {
            "applied": False,
            "message": "",
            "offset_subtracted": RESOLVE_TIMELINE_LABEL_OFFSET_TEXT,
            "offset_seconds": RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS,
            "earliest_original_seconds": None,
            "earliest_normalized_seconds": None,
            "max_original_end_seconds": None,
            "max_normalized_end_seconds": None,
            "reason": "",
            "warnings": [],
        }
        if not source_items:
            info["reason"] = "no edit segments"
            return source_items, info
        if cls._metadata_declares_source_relative(payload):
            info["reason"] = "input metadata declares source-relative elapsed time"
            return source_items, info

        starts: List[float] = []
        ends: List[float] = []
        all_times: List[float] = []
        for item in source_items:
            if not isinstance(item, dict):
                continue
            start_value = cls._first_present_value(item, START_FIELDS)
            end_value = cls._first_present_value(item, END_FIELDS)
            start_info = cls._coerce_json_time_value(start_value, fps=fps)
            end_info = cls._coerce_json_time_value(end_value, fps=fps)
            if start_info is not None:
                starts.append(float(start_info["seconds"]))
                all_times.append(float(start_info["seconds"]))
            if end_info is not None:
                ends.append(float(end_info["seconds"]))
                all_times.append(float(end_info["seconds"]))

        if not starts or not ends:
            info["reason"] = "missing parseable start/end segment times"
            return source_items, info

        earliest = min(starts)
        max_end = max(ends)
        normalized_earliest = earliest - RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS
        normalized_max_end = max_end - RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS
        info["earliest_original_seconds"] = round(earliest, 3)
        info["earliest_normalized_seconds"] = round(normalized_earliest, 3)
        info["max_original_end_seconds"] = round(max_end, 3)
        info["max_normalized_end_seconds"] = round(normalized_max_end, 3)

        if earliest < RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS:
            if max_end >= RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS:
                info["warnings"].append(
                    "Create Edited Timeline: mixed 00:xx and 01:xx input timecodes detected; no automatic normalization applied."
                )
                info["reason"] = "mixed source-relative and timeline-label-looking times"
            else:
                info["reason"] = "input already appears source-relative"
            return source_items, info

        if any(value < RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS for value in all_times):
            info["warnings"].append(
                "Create Edited Timeline: mixed 00:xx and 01:xx input timecodes detected; no automatic normalization applied."
            )
            info["reason"] = "mixed source-relative and timeline-label-looking times"
            return source_items, info

        if normalized_earliest < -0.0005:
            info["warnings"].append(
                "Create Edited Timeline: subtracting 01:00:00 would create negative segment times; no automatic normalization applied."
            )
            info["reason"] = "normalization would create negative times"
            return source_items, info

        if not cls._source_timeline_looks_like_one_hour_start(
            source_timeline_start_frame,
            fps,
            source_timeline_start_timecode,
        ):
            info["warnings"].append(
                "Create Edited Timeline: input starts at 01:00:00, but the active source timeline start could not be confirmed as 01:00:00:00; no automatic normalization applied."
            )
            info["reason"] = "source timeline start is not confirmed as 01:00:00:00"
            return source_items, info

        duration_tolerance = cls._normalization_duration_tolerance(fps)
        if source_timeline_duration_seconds is None or source_timeline_duration_seconds <= 0:
            info["warnings"].append(
                "Create Edited Timeline: input starts at 01:00:00, but source timeline duration could not be read; no automatic normalization applied."
            )
            info["reason"] = "source timeline duration unavailable"
            return source_items, info

        if normalized_max_end > source_timeline_duration_seconds + duration_tolerance:
            info["warnings"].append(
                "Create Edited Timeline: subtracting 01:00:00 still leaves segment times beyond the source timeline duration; no automatic normalization applied."
            )
            info["reason"] = "normalized times exceed source timeline duration"
            return source_items, info

        if max_end <= source_timeline_duration_seconds + duration_tolerance:
            info["reason"] = (
                "original 01:xx input fits source timeline duration, so it may be true elapsed time"
            )
            return source_items, info

        normalized_items = cls._copy_items_with_time_offset(
            source_items,
            -RESOLVE_TIMELINE_LABEL_OFFSET_SECONDS,
            fps=fps,
        )
        info["applied"] = True
        info["message"] = TIMELINE_LABEL_NORMALIZATION_MESSAGE
        info["reason"] = "source timeline starts at 01:00:00:00"
        return normalized_items, info

    @staticmethod
    def _normalization_duration_tolerance(fps: float) -> float:
        if fps <= 0:
            return 1.0
        return max(1.0, 2.0 / float(fps))

    @classmethod
    def _source_timeline_looks_like_one_hour_start(
        cls,
        timeline_start_frame: int,
        fps: float,
        start_timecode: str = "",
    ) -> bool:
        if str(start_timecode or "").strip().startswith("01:00:00"):
            return True
        try:
            nominal_fps = cls._nominal_timecode_fps_from_fps(fps)
            expected_frame = int(nominal_fps) * 3600
            tolerance_frames = max(2, int(round(float(fps))))
            return abs(int(timeline_start_frame) - expected_frame) <= tolerance_frames
        except Exception:
            return False

    @classmethod
    def _metadata_declares_source_relative(cls, payload: Any) -> bool:
        metadata = cls._extract_json_metadata(payload)
        if not metadata:
            return False
        keys_to_check = (
            "time_basis",
            "timecode_basis",
            "timestamp_basis",
            "input_time_basis",
            "input_times_are",
            "time_model",
        )
        for key in keys_to_check:
            value = metadata.get(key)
            if value is None:
                continue
            text = str(value).strip().lower().replace("_", "-")
            if any(token in text for token in ("source-relative", "elapsed-seconds", "elapsed time")):
                return True
        return False

    @classmethod
    def _copy_items_with_time_offset(
        cls,
        source_items: List[Any],
        offset_seconds: float,
        *,
        fps: float,
    ) -> List[Any]:
        copied_items = copy.deepcopy(source_items)
        for item in copied_items:
            if not isinstance(item, dict):
                continue
            for field_name in set(START_FIELDS + END_FIELDS):
                if field_name not in item:
                    continue
                normalized_value = cls._normalized_time_field_value(
                    item[field_name],
                    offset_seconds,
                    fps=fps,
                )
                if normalized_value is not None:
                    item[field_name] = normalized_value
        return copied_items

    @classmethod
    def _normalized_time_field_value(
        cls,
        value: Any,
        offset_seconds: float,
        *,
        fps: float,
    ) -> Any:
        time_info = cls._coerce_json_time_value(value, fps=fps)
        if time_info is None:
            return None
        normalized_seconds = float(time_info["seconds"]) + float(offset_seconds)
        if normalized_seconds < -0.0005:
            return None
        normalized_seconds = max(0.0, normalized_seconds)
        if isinstance(value, (int, float)):
            return normalized_seconds
        if cls._json_time_conversion_method(value) == "numeric-seconds":
            return "{:.3f}".format(normalized_seconds)
        return cls._format_seconds_as_vtt_time(normalized_seconds)

    @staticmethod
    def _format_seconds_as_vtt_time(seconds: float) -> str:
        total_milliseconds = int(round(float(seconds) * 1000.0))
        whole_seconds, milliseconds = divmod(total_milliseconds, 1000)
        hours = whole_seconds // 3600
        minutes = (whole_seconds % 3600) // 60
        secs = whole_seconds % 60
        return "{:02d}:{:02d}:{:02d}.{:03d}".format(
            hours,
            minutes,
            secs,
            milliseconds,
        )

    @staticmethod
    def _coerce_episode_number(value: Any, fallback: int) -> int:
        if isinstance(value, int):
            return max(1, value)
        if isinstance(value, float):
            return max(1, int(value))
        if isinstance(value, str):
            match = re.search(r"\d+", value)
            if match:
                return max(1, int(match.group(0)))
        return int(fallback)

    @staticmethod
    def _build_title_text(value: Any, description: str, fallback_number: int) -> str:
        if value is not None:
            text = str(value).strip()
            if text:
                return text
        clean_description = " ".join(str(description).split())
        if clean_description and clean_description != "No description provided":
            if len(clean_description) > 60:
                return clean_description[:57].rstrip() + "..."
            return clean_description
        return "Episode {:02d}".format(int(fallback_number))

    @staticmethod
    def _coerce_seconds(value: Any, fps: Optional[float] = None) -> Optional[float]:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                number = float(text)
            except ValueError:
                number = AIEditImportApp._parse_timecode_to_seconds(text, fps=fps)
                if number is None:
                    return None
        else:
            return None
        if number < 0:
            return None
        return number

    @staticmethod
    def _parse_timecode_to_seconds(
        timecode_text: str,
        fps: Optional[float] = None,
    ) -> Optional[float]:
        text = timecode_text.strip()
        short_match = re.fullmatch(r"(\d{1,2}):(\d{2})(?:([.,])(\d+))?", text)
        if short_match:
            minutes = int(short_match.group(1))
            seconds = int(short_match.group(2))
            fraction_text = short_match.group(4)
            if seconds >= 60:
                return None
            total_seconds = float((minutes * 60) + seconds)
            if fraction_text is not None:
                return total_seconds + (float("0." + fraction_text))
            return total_seconds

        match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})(?:([:.,])(\d+))?", text)
        if not match:
            return None
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        separator = match.group(4)
        fraction_text = match.group(5)
        if minutes >= 60 or seconds >= 60:
            return None
        total_seconds = float((hours * 3600) + (minutes * 60) + seconds)
        if fraction_text is None:
            return total_seconds

        if separator in (".", ","):
            return total_seconds + (float("0." + fraction_text))

        if fps is None:
            return None
        effective_fps = float(AIEditImportApp._nominal_timecode_fps_from_fps(fps))
        frames = int(fraction_text)
        if effective_fps <= 0 or frames >= effective_fps:
            return None
        return total_seconds + (float(frames) / effective_fps)

    @staticmethod
    def _display_value(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _build_marker_name(marker_number: int, description: str) -> str:
        clean_description = " ".join(str(description).split())
        base_label = "Moment {}".format(marker_number)
        if not clean_description:
            return base_label
        available_chars = max(0, MARKER_NAME_MAX_LENGTH - len(base_label) - 3)
        if len(clean_description) > available_chars:
            clean_description = clean_description[: max(0, available_chars - 1)].rstrip() + "..."
        return "{} | {}".format(base_label, clean_description)

    @staticmethod
    def _build_cut_name(cut_number: int, description: str) -> str:
        clean_description = " ".join(str(description).split())
        base_label = "Cut {}".format(cut_number)
        if not clean_description:
            return base_label
        available_chars = max(0, MARKER_NAME_MAX_LENGTH - len(base_label) - 3)
        if len(clean_description) > available_chars:
            clean_description = clean_description[: max(0, available_chars - 1)].rstrip() + "..."
        return "{} | {}".format(base_label, clean_description)

    def _set_path_text(self, text: str) -> None:
        widget = self.window.Find(PATH_FIELD_ID)
        self._set_widget_text(widget, text or "")

    def _set_status(self, text: str) -> None:
        widget = self.window.Find(STATUS_FIELD_ID)
        self._set_widget_text(widget, text or "")

    @staticmethod
    def _set_widget_text(widget, text: str) -> None:
        if widget is None:
            return
        for attribute in ("Text", "PlainText"):
            try:
                setattr(widget, attribute, text)
                return
            except Exception:
                pass


def main() -> None:
    app = AIEditImportApp()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("[AI Edit Import] Fatal error:")
        print(exc)
        print(traceback.format_exc())
        try:
            script = 'display dialog "{}" with title "{}" buttons {{"OK"}} default button "OK"'.format(
                str(exc).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"),
                WINDOW_TITLE,
            )
            subprocess.run(["osascript", "-e", script], check=False, encoding="utf-8", errors="replace")
        except Exception:
            pass
