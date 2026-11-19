"""Prepare downloadable answers in memory and save them on explicit desktop requests."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import unicodedata
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape


_FORMATS = {".txt", ".md", ".json", ".csv", ".docx", ".xlsx"}
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
_RESERVED.update(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(10))
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _desktop_folder() -> Path:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        folder_id = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le)
        get_folder = ctypes.windll.shell32.SHGetKnownFolderPath
        get_folder.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        get_folder.restype = ctypes.c_long
        address = ctypes.c_void_p()
        try:
            if get_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(address)) or not address.value:
                raise OSError("Windows 未返回桌面位置，请确认当前用户的桌面可用。")
            return Path(ctypes.wstring_at(address))
        finally:
            if address.value:
                ctypes.windll.ole32.CoTaskMemFree(address)
    home = Path.home()
    if sys.platform.startswith("linux"):
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
        try:
            settings = config.read_text(encoding="utf-8")
        except FileNotFoundError:
            settings = ""
        entry = re.search(r'^XDG_DESKTOP_DIR="([^"\n]+)"$', settings, re.MULTILINE)
        if entry:
            value = entry.group(1)
            if value == "$HOME" or value.startswith("$HOME/"):
                return home / value[5:].lstrip("/")
            if Path(value).is_absolute():
                return Path(value)
    return home / "Desktop"


def _filename(name: str, allowed: set[str]) -> str:
    if not isinstance(name, str):
        raise ValueError("文件名必须是文本。")
    name = unicodedata.normalize("NFKC", name).strip()
    if (not name or name.startswith(".") or any(c in '<>:"/\\|?*' or unicodedata.category(c).startswith("C") for c in name)
            or len(name.encode("utf-16-le")) > 240):
        raise ValueError("请使用不含路径和特殊符号的简短文件名。")
    stem, separator, suffix = name.rpartition(".")
    extension = "." + suffix.lower()
    if not separator or extension not in allowed:
        raise ValueError("不支持此文件格式。可用格式为 " + "、".join(sorted(allowed)) + "。")
    if not stem.strip(". ") or stem.split(".")[0].rstrip().upper() in _RESERVED:
        raise ValueError("文件名是系统保留名，请换一个名字。")
    return stem.replace(".", "-").rstrip() + extension


def _text_bytes(content: str) -> bytes:
    if not isinstance(content, str) or not content.strip() or "\0" in content:
        raise ValueError("文件需要非空文本内容。")
    raw = content.encode("utf-8")
    if len(raw) > 512 * 1024:
        raise ValueError("单个文件内容超过 512 KiB，请缩短内容。")
    return raw


def _table(content: str) -> list[list[str]]:
    rows = []
    cells = 0
    try:
        for row in csv.reader(io.StringIO(content, newline=""), strict=True):
            cells += len(row)
            if len(row) > 256 or len(rows) >= 10000 or cells > 50000:
                raise ValueError("表格过大，请减少行列数。")
            if any(len(cell) > 32767 for cell in row):
                raise ValueError("单元格文本超过 32767 个字符。")
            rows.append(row)
    except csv.Error as error:
        raise ValueError("表格应使用合法的 CSV 行列格式。") from error
    return rows


def _relationships(entries: dict[str, tuple[str, str]]) -> str:
    links = "".join(f'<Relationship Id="{key}" Type="{_REL}/{kind}" Target="{target}"/>'
                    for key, (kind, target) in entries.items())
    return '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + links + '</Relationships>'


def _office_package(parts: dict[str, str], content_types: dict[str, str]) -> bytes:
    overrides = "".join(f'<Override PartName="/{name}" ContentType="application/vnd.openxmlformats-officedocument.{kind}+xml"/>'
                        for name, kind in content_types.items())
    parts["[Content_Types].xml"] = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>' + overrides + '</Types>'
    )
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, part in parts.items():
            package.writestr(name, (_XML + part).encode("utf-8"))
    return data.getvalue()


def _word(content: str) -> bytes:
    paragraphs = []
    for line in content.splitlines():
        text = '</w:t><w:tab/><w:t xml:space="preserve">'.join(escape(part) for part in line.split("\t"))
        paragraphs.append('<w:p><w:r><w:t xml:space="preserve">' + text + '</w:t></w:r></w:p>')
    document = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>' + "".join(paragraphs) + '<w:sectPr/></w:body></w:document>')
    return _office_package({"_rels/.rels": _relationships({"rId1": ("officeDocument", "word/document.xml")}),
                            "word/document.xml": document}, {"word/document.xml": "wordprocessingml.document.main"})


def _column(index: int) -> str:
    letters = ""
    while index:
        index, last = divmod(index - 1, 26)
        letters = chr(65 + last) + letters
    return letters


def _xlsx(rows: list[list[str]]) -> bytes:
    sheet = [f'<worksheet xmlns="{_SHEET}"><sheetData>']
    for i, row in enumerate(rows, 1):
        sheet.append(f'<row r="{i}">')
        for j, value in enumerate(row, 1):
            ref = _column(j) + str(i)
            if _NUMBER.fullmatch(value) and sum(c.isdigit() for c in value) <= 15:
                sheet.append(f'<c r="{ref}" t="n"><v>{value}</v></c>')
            else:
                sheet.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>')
        sheet.append("</row>")
    sheet.append("</sheetData></worksheet>")
    workbook = (f'<workbook xmlns="{_SHEET}" xmlns:r="{_REL}"><sheets>'
                '<sheet name="工作表" sheetId="1" r:id="rId1"/></sheets></workbook>')
    parts = {"_rels/.rels": _relationships({"rId1": ("officeDocument", "xl/workbook.xml")}),
             "xl/workbook.xml": workbook, "xl/worksheets/sheet1.xml": "".join(sheet),
             "xl/_rels/workbook.xml.rels": _relationships({"rId1": ("worksheet", "worksheets/sheet1.xml")})}
    return _office_package(parts, {"xl/workbook.xml": "spreadsheetml.sheet.main",
                                  "xl/worksheets/sheet1.xml": "spreadsheetml.worksheet"})


def _csv_literal(value: str) -> str:
    if _NUMBER.fullmatch(value):
        return value
    if unicodedata.normalize("NFKC", value).lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _prepare(name: str, content: str) -> bytes:
    raw = _text_bytes(content)
    extension = Path(name).suffix
    if extension == ".json":
        def invalid_number(_):
            raise ValueError("JSON 文件不能包含非有限数值。")
        try:
            json.loads(content, parse_constant=invalid_number)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError("生成内容不是有效的 JSON。") from error
    if extension in (".docx", ".xlsx") and _INVALID_XML.search(content):
        raise ValueError("Office 文件的内容包含无效控制字符。")
    if extension == ".docx":
        return _word(content)
    if extension == ".xlsx":
        return _xlsx(_table(content))
    if extension == ".csv":
        result = io.StringIO(newline="")
        writer = csv.writer(result)
        for row in _table(content):
            writer.writerow([_csv_literal(value) for value in row])
        return result.getvalue().encode("utf-8-sig")
    return raw


def _write_new(desktop: Path, name: str, data: bytes) -> Path:
    desktop.mkdir(exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    handle = tempfile.NamedTemporaryFile(dir=desktop, prefix=".be-", suffix=".tmp", delete=False)
    temp = Path(handle.name)
    try:
        with handle as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for i in range(10000):
            target = desktop / (name if i == 0 else f"{stem} ({i}){suffix}")
            try:
                if os.name == "nt":
                    os.rename(temp, target)  # Refuses to replace an existing file on Windows.
                else:
                    os.link(temp, target)
                return target
            except FileExistsError:
                continue
        raise FileExistsError("桌面上同名文件过多，请整理后重试。")
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Offer:
    name: str
    data: bytes


class ArtifactStore:
    """Offers have no filesystem side effects. The caller authorizes each save."""

    def __init__(self, desktop: Path | None = None):
        self._desktop = Path(desktop) if desktop is not None else None
        self._offers: OrderedDict[str, _Offer] = OrderedDict()
        self._saved: OrderedDict[str, Path] = OrderedDict()
        self._lock = threading.Lock()

    def offer(self, name: str, content: str) -> dict:
        name = _filename(name, _FORMATS)
        offer = _Offer(name, _prepare(name, content))
        identifier = secrets.token_urlsafe(24)
        with self._lock:
            self._offers[identifier] = offer
            if len(self._offers) > 64:
                self._offers.popitem(last=False)
        return {"id": identifier, "name": name}

    def _save(self, identifier: str, offer: _Offer | None) -> dict:
        target = self._saved.get(identifier)
        if target is None or not target.is_file():
            if offer is None:
                raise KeyError("文件已过期，请重新生成。")
            target = _write_new(self._desktop or _desktop_folder(), offer.name, offer.data)
        self._saved[identifier] = target
        self._saved.move_to_end(identifier)
        if len(self._saved) > 256:
            self._saved.popitem(last=False)
        return {"id": identifier, "name": target.name, "path": str(target)}

    def save(self, identifier: str) -> dict:
        with self._lock:
            return self._save(identifier, self._offers.get(identifier))

    def save_task(self, task_id: str, code: str, name: str = "生成的脚本.py") -> dict:
        """The caller supplies code from an accepted task, never from the save request."""
        offer = _Offer(_filename(name, {".py"}), _text_bytes(code))
        with self._lock:
            return self._save("task:" + task_id, offer)
