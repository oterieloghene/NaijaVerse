import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODENAME_RE = re.compile(r"^!keke \S+$")


def _csv_rows():
    with open(ROOT / "locations_codenames.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["code", "name"]
    return [r for r in rows[1:] if r]


def _approval():
    with open(ROOT / "codenames_for_approval.txt") as f:
        return [l.strip() for l in f if l.strip()]


def test_csv_rows_well_formed():
    rows = _csv_rows()
    assert len(rows) == 33, f"expected 33 codename rows, got {len(rows)}"
    codes = [r[0] for r in rows]
    assert len(set(codes)) == len(codes), "duplicate codenames in CSV"
    for code, name in rows:
        assert CODENAME_RE.match(code), f"malformed codename: {code!r}"
        assert name.strip(), f"empty name for {code!r}"


def test_approval_list_well_formed():
    codes = _approval()
    assert len(codes) == 33, f"expected 33 approval entries, got {len(codes)}"
    assert len(set(codes)) == len(codes), "duplicate codenames in approval list"
    for code in codes:
        assert CODENAME_RE.match(code), f"malformed codename: {code!r}"


def test_csv_and_approval_lists_match():
    csv_codes = {r[0] for r in _csv_rows()}
    approval_codes = set(_approval())
    assert csv_codes == approval_codes, (
        f"CSV/approval mismatch: csv-only={csv_codes - approval_codes}, "
        f"approval-only={approval_codes - csv_codes}"
    )


def test_university_codename_maps_to_administrative_office():
    rows = dict((r[0], r[1]) for r in _csv_rows())
    assert rows.get("!keke university") == "Administrative Office"