"""
tse_sector（東証33業種の対応表）のテスト。

JPX の data_j.xlsx と同じ列構成の小さな xlsx をその場で作り、
_download_listing を差し替えるので JPX は叩かない。

実行: source .venv/bin/activate && python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date
from io import BytesIO
from unittest import mock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tse_sector  # noqa: E402

JPX_COLUMNS = [
    "日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分",
    "17業種コード", "17業種区分", "規模コード", "規模区分",
]


def make_xlsx(rows):
    """rows: [(コード, 銘柄名, 33業種区分), ...] から data_j.xlsx 相当のバイト列を作る。"""
    records = [
        ["20260831", code, name, "プライム（内国株式）", "-", sector, "-", "-", "-", "-"]
        for code, name, sector in rows
    ]
    buf = BytesIO()
    pd.DataFrame(records, columns=JPX_COLUMNS).to_excel(buf, index=False)
    return buf.getvalue()


class NormalizeCodeTest(unittest.TestCase):
    def test_int_float_str(self):
        self.assertEqual(tse_sector.normalize_code(7203), "7203")
        self.assertEqual(tse_sector.normalize_code(7203.0), "7203")
        self.assertEqual(tse_sector.normalize_code(" 7203 "), "7203")

    def test_alphanumeric_code_kept(self):
        self.assertEqual(tse_sector.normalize_code("130A"), "130A")


class ParseListingTest(unittest.TestCase):
    def test_drops_rows_without_sector(self):
        content = make_xlsx([
            ("9104", "商船三井", "海運業"),
            ("1305", "ｉＦｒｅｅＥＴＦ", "-"),
            ("1301", "極洋", "水産・農林業"),
        ])
        rows = tse_sector.parse_jpx_listing(content)
        self.assertEqual([r["証券コード"] for r in rows], ["1301", "9104"])
        self.assertEqual(rows[1]["33業種区分"], "海運業")
        self.assertEqual(rows[0]["データ日付"], "20260831")

    def test_missing_column_raises(self):
        buf = BytesIO()
        pd.DataFrame([["1301"]], columns=["コード"]).to_excel(buf, index=False)
        with self.assertRaises(ValueError):
            tse_sector.parse_jpx_listing(buf.getvalue())


class LoadAndRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sector33.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self, rows):
        with mock.patch.object(tse_sector, "_download_listing", return_value=make_xlsx(rows)):
            return tse_sector.refresh_sector_map(self.path)

    def test_missing_file_is_empty(self):
        self.assertEqual(tse_sector.load_sector_map(self.path), {})

    def test_refresh_then_load(self):
        self.assertEqual(self.refresh([("9104", "商船三井", "海運業")]), [])
        sector_map = tse_sector.load_sector_map(self.path)
        self.assertEqual(sector_map, {"9104": "海運業"})
        self.assertEqual(tse_sector.sector_of(9104, sector_map), "海運業")
        self.assertEqual(tse_sector.sector_of(9104.0, sector_map), "海運業")
        self.assertEqual(tse_sector.sector_of(1301, sector_map), tse_sector.UNKNOWN_SECTOR)

    def test_refresh_reports_sector_changes_only(self):
        self.refresh([("9104", "商船三井", "海運業"), ("1301", "極洋", "水産・農林業")])
        changed = self.refresh([
            ("9104", "商船三井", "陸運業"),
            ("1301", "極洋", "水産・農林業"),
            ("7203", "トヨタ自動車", "輸送用機器"),  # 新規の出入りは変更に数えない
        ])
        self.assertEqual(changed, [("9104", "海運業", "陸運業")])
        self.assertEqual(tse_sector.load_sector_map(self.path)["7203"], "輸送用機器")

    def test_download_failure_keeps_existing_csv(self):
        self.refresh([("9104", "商船三井", "海運業")])
        with mock.patch.object(tse_sector, "_download_listing", side_effect=OSError("down")):
            self.assertEqual(tse_sector.refresh_sector_map(self.path), [])
        self.assertEqual(tse_sector.load_sector_map(self.path), {"9104": "海運業"})

    def test_stale_warning(self):
        self.assertIsNotNone(tse_sector.stale_warning(self.path, today=date(2026, 10, 5)))  # CSV 無し
        self.refresh([("9104", "商船三井", "海運業")])  # データ日付 2026-08-31
        self.assertIsNone(tse_sector.stale_warning(self.path, today=date(2026, 10, 15)))
        self.assertIsNone(tse_sector.stale_warning(self.path, today=date(2026, 11, 1)))  # 62日目
        message = tse_sector.stale_warning(self.path, today=date(2026, 11, 2))
        self.assertIn("2026-08-31", message)


if __name__ == "__main__":
    unittest.main()
