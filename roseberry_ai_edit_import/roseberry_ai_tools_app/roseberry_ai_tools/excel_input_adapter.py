from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"main": MAIN_NS, "rel": REL_NS, "pkg": PKG_REL_NS}
REQUIRED_HEADER_ALIASES = (
    ("Timecode (In/Out)",),
    ("Summary",),
    ("Reason for Cut (Hook/Cliffhanger)", "Reason for Cut"),
)
RANGE_PATTERN = re.compile(r"^\s*(.+?)\s*[-\u2013\u2014]\s*(.+?)\s*$")
TIMECODE_PATTERN = re.compile(
    r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d+))?$"
)


@dataclass
class ExcelNormalizationResult:
    payload: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    worksheet_name: str = ""


def normalize_xlsx_segments(
    path: Path,
    *,
    timeline_duration_seconds: Optional[float] = None,
) -> ExcelNormalizationResult:
    """Convert the temporary Gemini worksheet into the canonical segments payload."""
    path = Path(path).expanduser()
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Excel compatibility mode supports .xlsx files only.")
    if not path.exists():
        raise FileNotFoundError("File does not exist: {}".format(path))

    with zipfile.ZipFile(path) as archive:
        worksheet_name, worksheet_root = _read_first_worksheet(archive)
        shared_strings = _read_shared_strings(archive)
        rows = _read_rows(worksheet_root, shared_strings)
        _reject_merged_cells(worksheet_root)

    if not rows:
        raise ValueError("The first worksheet is empty.")
    headers = rows[0]
    header_indexes = _required_header_indexes(headers)
    segments: List[Dict[str, Any]] = []
    warnings: List[str] = []
    previous_end: Optional[float] = None
    previous_physical_row = 1

    for worksheet_row, values, formula_columns in rows[1:]:
        if segments and worksheet_row > previous_physical_row + 1:
            break
        required_values = [values.get(index, "").strip() for index in header_indexes]
        if segments and not any(str(value).strip() for value in values.values()):
            break
        if segments and _is_footer_row(required_values):
            break
        if not any(required_values):
            previous_physical_row = worksheet_row
            continue
        if not all(required_values):
            raise ValueError(
                "Worksheet row {} is partially filled. Timecode, Summary, and Reason for Cut are required.".format(
                    worksheet_row
                )
            )
        if formula_columns.intersection(header_indexes):
            raise ValueError(
                "Worksheet row {} contains a formula in a required column. Replace it with plain text.".format(
                    worksheet_row
                )
            )

        start_text, end_text = _split_time_range(required_values[0], worksheet_row)
        start_seconds = _parse_time_value(start_text, worksheet_row, "start")
        if end_text.lower() == "end":
            if timeline_duration_seconds is None or timeline_duration_seconds <= 0:
                raise ValueError(
                    "Worksheet row {} uses End, but the active Resolve timeline duration could not be read reliably. "
                    "Replace End with an explicit end time.".format(worksheet_row)
                )
            end_seconds = float(timeline_duration_seconds)
            end_value: Any = end_seconds
        else:
            end_seconds = _parse_time_value(end_text, worksheet_row, "end")
            end_value = _normalize_timecode(end_seconds)

        if end_seconds <= start_seconds:
            raise ValueError(
                "Worksheet row {} has an end time that is not after its start time.".format(
                    worksheet_row
                )
            )
        if previous_end is not None:
            if start_seconds < previous_end:
                raise ValueError(
                    "Worksheet row {} overlaps the previous segment.".format(worksheet_row)
                )
            if start_seconds > previous_end:
                warnings.append(
                    "Worksheet row {} starts {:.3f} seconds after the previous segment ends.".format(
                        worksheet_row,
                        start_seconds - previous_end,
                    )
                )

        segment_number = len(segments) + 1
        segments.append(
            {
                "segment_id": "EP{:02d}".format(segment_number),
                "episode_number": segment_number,
                "start_time": _normalize_timecode(start_seconds),
                "end_time": end_value,
                "duration_seconds": end_seconds - start_seconds,
                "description": required_values[1],
                "editorial_reason": required_values[2],
                "qa_notes": required_values[2],
            }
        )
        previous_end = end_seconds
        previous_physical_row = worksheet_row

    if not segments:
        raise ValueError("The first worksheet does not contain any populated segment rows.")
    return ExcelNormalizationResult(
        payload={
            "segments": segments,
            "metadata": {
                "input_adapter": "temporary_xlsx_compatibility",
                "source_file": path.name,
                "worksheet": worksheet_name,
            },
        },
        warnings=warnings,
        worksheet_name=worksheet_name,
    )


def _read_first_worksheet(archive: zipfile.ZipFile) -> Tuple[str, ElementTree.Element]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    first_sheet = workbook.find("main:sheets/main:sheet", NS)
    if first_sheet is None:
        raise ValueError("The workbook does not contain a worksheet.")
    worksheet_name = str(first_sheet.attrib.get("name") or "Sheet1")
    relationship_id = first_sheet.attrib.get("{{{}}}id".format(REL_NS))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = None
    for relationship in relationships.findall("pkg:Relationship", NS):
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib.get("Target")
            break
    if not target:
        raise ValueError("Could not locate the first worksheet XML.")
    worksheet_path = "xl/{}".format(target.lstrip("/").removeprefix("xl/"))
    return worksheet_name, ElementTree.fromstring(archive.read(worksheet_path))


def _read_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings: List[str] = []
    for item in root.findall("main:si", NS):
        strings.append("".join(node.text or "" for node in item.iter("{{{}}}t".format(MAIN_NS))))
    return strings


def _read_rows(
    worksheet_root: ElementTree.Element,
    shared_strings: List[str],
) -> List[Any]:
    parsed_rows: List[Any] = []
    for row in worksheet_root.findall("main:sheetData/main:row", NS):
        worksheet_row = int(row.attrib.get("r") or 0)
        values: Dict[int, str] = {}
        formula_columns = set()
        for cell in row.findall("main:c", NS):
            column_index = _column_index(cell.attrib.get("r", ""))
            if cell.find("main:f", NS) is not None:
                formula_columns.add(column_index)
            value = cell.find("main:v", NS)
            raw_value = value.text if value is not None and value.text is not None else ""
            if cell.attrib.get("t") == "s" and raw_value:
                raw_value = shared_strings[int(raw_value)]
            elif cell.attrib.get("t") == "inlineStr":
                raw_value = "".join(
                    node.text or "" for node in cell.iter("{{{}}}t".format(MAIN_NS))
                )
            values[column_index] = str(raw_value)
        if worksheet_row == 1:
            parsed_rows.append([values.get(index, "").strip() for index in range(3)])
        else:
            parsed_rows.append((worksheet_row, values, formula_columns))
    return parsed_rows


def _reject_merged_cells(worksheet_root: ElementTree.Element) -> None:
    merged = worksheet_root.find("main:mergeCells", NS)
    if merged is not None and list(merged):
        raise ValueError("Merged cells are not supported in temporary Excel compatibility mode.")


def _required_header_indexes(headers: List[str]) -> List[int]:
    indexes = []
    for aliases in REQUIRED_HEADER_ALIASES:
        matches = [
            index
            for index, value in enumerate(headers)
            if value.strip() in aliases
        ]
        if not matches:
            raise ValueError(
                "Missing required Excel column: {}".format(" or ".join(aliases))
            )
        if len(matches) > 1:
            raise ValueError(
                "Duplicate required Excel column: {}".format(" or ".join(aliases))
            )
        indexes.append(matches[0])
    return indexes


def _split_time_range(value: str, worksheet_row: int) -> Tuple[str, str]:
    match = RANGE_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(
            "Worksheet row {} has an invalid Timecode (In/Out) value: {!r}".format(
                worksheet_row,
                value,
            )
        )
    return match.group(1).strip(), match.group(2).strip()


def _is_footer_row(required_values: List[str]) -> bool:
    first_cell = required_values[0].strip()
    if not first_cell:
        return False
    lowered = first_cell.lower()
    return lowered.startswith("sources:") or _looks_like_url(first_cell)


def _looks_like_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("http://", "https://", "www."))


def _parse_time_value(value: str, worksheet_row: int, label: str) -> float:
    match = TIMECODE_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError(
            "Worksheet row {} has an invalid {} time: {!r}".format(
                worksheet_row,
                label,
                value,
            )
        )
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    if seconds >= 60 or (match.group(1) is not None and minutes >= 60):
        raise ValueError(
            "Worksheet row {} has an invalid {} time: {!r}".format(
                worksheet_row,
                label,
                value,
            )
        )
    fraction = float("0." + match.group(4)) if match.group(4) else 0.0
    return float((hours * 3600) + (minutes * 60) + seconds) + fraction


def _normalize_timecode(seconds: float) -> str:
    whole_seconds = int(seconds)
    milliseconds = int(round((seconds - whole_seconds) * 1000))
    if milliseconds == 1000:
        whole_seconds += 1
        milliseconds = 0
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60
    if milliseconds:
        return "{:02d}:{:02d}:{:02d}.{:03d}".format(hours, minutes, secs, milliseconds)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, secs)


def _column_index(cell_reference: str) -> int:
    match = re.match(r"([A-Z]+)", cell_reference.upper())
    if not match:
        raise ValueError("Invalid Excel cell reference: {}".format(cell_reference))
    index = 0
    for character in match.group(1):
        index = (index * 26) + (ord(character) - ord("A") + 1)
    return index - 1
