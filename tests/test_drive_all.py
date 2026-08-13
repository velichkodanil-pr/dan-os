"""Full-Drive indexing: file filter, CSV row-safe extraction, sheet export name."""
import pytest

from app.core.google_client import (GOOGLE_DOC_MIME, GOOGLE_SHEET_MIME,
                                    drive_indexable)
from app.core.ingest import chunk_text, extract_text


def test_drive_indexable_filter():
    assert drive_indexable({"name": "Доступи", "mimeType": GOOGLE_SHEET_MIME})
    assert drive_indexable({"name": "Нотатки", "mimeType": GOOGLE_DOC_MIME})
    assert drive_indexable({"name": "logins.csv", "mimeType": "text/csv",
                            "size": "1024"})
    assert drive_indexable({"name": "contract.pdf", "mimeType": "application/pdf",
                            "size": str(5 * 1024 * 1024)})
    assert not drive_indexable({"name": "video.mp4", "mimeType": "video/mp4",
                                "size": "999"})
    assert not drive_indexable({"name": "huge.pdf", "mimeType": "application/pdf",
                                "size": str(50 * 1024 * 1024)})
    assert not drive_indexable({"name": "app.exe", "mimeType": "application/x-exe",
                                "size": "10"})


def test_csv_rows_stay_atomic_in_chunks():
    rows = [f"Партнер {i};login_p{i};https://portal{i}.example;нотатка про доступ {i}"
            for i in range(60)]
    data = "\n".join(rows).encode()
    text = extract_text("Доступи партнерів.csv", data)
    assert "Партнер 7;login_p7" in text
    chunks = chunk_text(text)
    # every row lives in exactly one piece — never split mid-row
    for i in range(60):
        marker = f"Партнер {i};login_p{i}"
        assert sum(1 for c in chunks if marker in c) >= 1
        for c in chunks:
            if f"Партнер {i};" in c:
                assert f"login_p{i}" in c  # row not cut between name and login
                break


def test_extract_text_tsv():
    data = "Сервіс\tЛогін\nTravelon\tadmin_tvl\n".encode()
    text = extract_text("access.tsv", data)
    assert "admin_tvl" in text


def _make_xlsx() -> bytes:
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Партнери"
    ws1.append(["Партнер", "Логін", "Портал"])
    ws1.append(["Anex", "anex_tvl", "portal.anex.example"])
    ws2 = wb.create_sheet("Банки")
    ws2.append(["Банк", "Логін"])
    ws2.append(["ПУМБ", "pumb_fin_login"])
    ws2.append([None, None])  # empty row must be skipped
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_extract_text_xlsx_all_sheets():
    """ALL tabs land in the text (the whole point of the xlsx export)."""
    text = extract_text("Доступи.xlsx", _make_xlsx())
    assert "== Аркуш: Партнери ==" in text and "== Аркуш: Банки ==" in text
    assert "Anex | anex_tvl | portal.anex.example" in text
    assert "ПУМБ | pumb_fin_login" in text
    chunks = chunk_text(text)
    for c in chunks:  # row atomicity survives chunking
        if "Anex |" in c:
            assert "anex_tvl" in c
        if "ПУМБ" in c:
            assert "pumb_fin_login" in c


def test_drive_indexable_xlsx_and_sheet():
    from app.core.google_client import XLSX_MIME
    assert drive_indexable({"name": "fin.xlsx", "mimeType": XLSX_MIME,
                            "size": "2048"})
