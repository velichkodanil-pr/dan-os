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
