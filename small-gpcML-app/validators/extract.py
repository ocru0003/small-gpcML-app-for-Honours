from __future__ import annotations

from io import BytesIO
from typing import List, Tuple, Optional

from lxml import etree


def _safe_text(el: Optional[etree._Element]) -> Optional[str]:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t if t else None


def parse_xml_bytes(xml_bytes: bytes) -> etree._Element:
    """
    Parse XML from bytes while preserving line numbers.
    (Line numbers only matter if the XML has newlines.)
    """
    doc = etree.parse(BytesIO(xml_bytes))  # parse() tracks sourceline better than XML()
    return doc.getroot()


def extract_contacts(root: etree._Element, filename: str) -> List[dict]:
    rows: List[dict] = []
    for c in root.xpath("/gpcML/ContactList/Contact"):
        rows.append(
            {
                "file": filename,
                "FirstName": c.get("FirstName"),
                "MiddleName": c.get("MiddleName"),
                "LastName": c.get("LastName"),
                "E-mail": c.get("E-mail"),
                "Institution": c.get("Institution"),
                "line": c.sourceline,
            }
        )
    return rows


def extract_columns(root: etree._Element, filename: str) -> List[dict]:
    rows: List[dict] = []
    # GuardColumn optional, Column 1..n
    cols = root.xpath(
        "/gpcML/RunTimeData/InstrumentConfiguration/ColumnList/*[self::GuardColumn or self::Column]"
    )

    for col in cols:
        idinfo = col.find("./ColumnInfo/IDinfo")
        rows.append(
            {
                "file": filename,
                "element": col.tag,  # Column / GuardColumn
                "PoreSizeMin": col.get("PoreSizeMin"),
                "PoreSizeMax": col.get("PoreSizeMax"),
                "StationaryPhase": col.get("StationaryPhase"),
                "DateOfInstallation": col.get("DateOfInstallation"),
                "IDinfo_Name": idinfo.get("Name") if idinfo is not None else None,
                "IDinfo_SerialNumber": idinfo.get("SerialNumber") if idinfo is not None else None,
                "IDinfo_Manufacturer": idinfo.get("Manufacturer") if idinfo is not None else None,
                "line": col.sourceline,
            }
        )
    return rows


def extract_eluent(root: etree._Element, filename: str) -> List[dict]:
    nodes = root.xpath("/gpcML/RunTimeData/InstrumentConfiguration/Eluent")
    if not nodes:
        return [
            {
                "file": filename,
                "EluentName": None,
                "AdditiveName": None,
                "RecyclingMode": None,
                "Ratio": None,
                "AdditiveConcentration": None,
                "line": None,
            }
        ]

    e = nodes[0]
    return [
        {
            "file": filename,
            "EluentName": _safe_text(e.find("./EluentName")),
            "AdditiveName": _safe_text(e.find("./AdditiveName")),
            "RecyclingMode": _safe_text(e.find("./RecyclingMode")),
            "Ratio": _safe_text(e.find("./Ratio")),
            "AdditiveConcentration": _safe_text(e.find("./AdditiveConcentration")),
            "line": e.sourceline,
        }
    ]


def extract_from_many(files: List[Tuple[str, bytes]]) -> Tuple[List[dict], List[dict], List[dict], List[str]]:
    """
    files: [(filename, xml_bytes), ...]
    Returns (contacts_df, columns_df, eluent_df, errors)
    """
    contacts_rows: List[dict] = []
    columns_rows: List[dict] = []
    eluent_rows: List[dict] = []
    errors: List[str] = []

    for fname, data in files:
        try:
            root = parse_xml_bytes(data)
            contacts_rows.extend(extract_contacts(root, fname))
            columns_rows.extend(extract_columns(root, fname))
            eluent_rows.extend(extract_eluent(root, fname))
        except Exception as ex:
            errors.append(f"{fname}: {type(ex).__name__}: {ex}")

    return contacts_rows, columns_rows, eluent_rows, errors
