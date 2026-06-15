"""Small XLSX reader for import adapters.

This intentionally uses only the Python standard library so the importer can run
before we choose a SaaS dependency stack. It reads values, not formulas,
formatting, charts, or rich workbook features.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable
from xml.etree import ElementTree
from zipfile import ZipFile


NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class XlsxRow:
    row_number: int
    values: dict[int, str]


def read_xlsx_sheet(path: str | Path, sheet_name: str) -> list[dict[str, str]]:
    rows = _read_sheet_rows(path, sheet_name)
    if not rows:
        return []

    header = rows[0].values
    headers = {col: value.strip() for col, value in header.items() if value and value.strip()}
    output: list[dict[str, str]] = []
    for row in rows[1:]:
        item = {name: row.values.get(col, "").strip() for col, name in headers.items()}
        if any(value for value in item.values()):
            output.append(item)
    return output


def read_xlsx_sheet_raw(path: str | Path, sheet_name: str) -> list[XlsxRow]:
    return _read_sheet_rows(path, sheet_name)


def _read_sheet_rows(path: str | Path, sheet_name: str) -> list[XlsxRow]:
    path = Path(path)
    with ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_targets = _read_sheet_targets(archive)
        target = sheet_targets.get(sheet_name)
        if not target:
            return []
        root = ElementTree.fromstring(archive.read(target))

    rows: list[XlsxRow] = []
    sheet_data = root.find(f"{NS_MAIN}sheetData")
    if sheet_data is None:
        return rows

    for row in sheet_data.findall(f"{NS_MAIN}row"):
        row_number = int(row.attrib.get("r", "0") or 0)
        values: dict[int, str] = {}
        for cell in row.findall(f"{NS_MAIN}c"):
            reference = cell.attrib.get("r", "A1")
            column = _column_number(reference)
            values[column] = _cell_text(cell, shared_strings)
        rows.append(XlsxRow(row_number=row_number, values=values))
    return rows


def _read_shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    strings: list[str] = []
    for item in root.findall(f"{NS_MAIN}si"):
        strings.append("".join(item.itertext()).replace("\r", " ").replace("\n", " ").strip())
    return strings


def _read_sheet_targets(archive: ZipFile) -> dict[str, str]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationship_targets = {
        rel.attrib["Id"]: rel.attrib["Target"].lstrip("/")
        for rel in rels
        if rel.attrib.get("Id") and rel.attrib.get("Target")
    }

    sheet_targets: dict[str, str] = {}
    sheets = workbook.find(f"{NS_MAIN}sheets")
    if sheets is None:
        return sheet_targets

    for sheet in sheets.findall(f"{NS_MAIN}sheet"):
        name = sheet.attrib.get("name")
        relationship_id = sheet.attrib.get(f"{NS_REL}id")
        if not name or not relationship_id:
            continue
        target = relationship_targets.get(relationship_id)
        if not target:
            continue
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        sheet_targets[name] = target
    return sheet_targets


def _cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find(f"{NS_MAIN}v")
    if cell_type == "s" and value is not None and value.text not in (None, ""):
        return shared_strings[int(value.text)]
    if cell_type == "inlineStr":
        inline = cell.find(f"{NS_MAIN}is")
        return "" if inline is None else "".join(inline.itertext()).strip()
    return "" if value is None or value.text is None else value.text.strip()


def _column_number(reference: str) -> int:
    match = re.match(r"([A-Z]+)", reference.upper())
    if not match:
        return 1
    number = 0
    for char in match.group(1):
        number = (number * 26) + (ord(char) - ord("A") + 1)
    return number


def normalize_header_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [{_normalize_key(key): value for key, value in row.items()} for row in rows]


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())

