"""
edinet_dividend の減配判定のテスト。

株式分割を減配と誤検知した実例（三井住友トラスト(8309)、2026-08-01 に1:4分割、
「185.0 → 47.5円の減配予想」と毎週 LINE 通知された）の実レスポンスを
fixtures/earnings_8309.json に保存し、_fetch_earnings を差し替えて API は叩かない。

実行: source .venv/bin/activate && python -m unittest discover -s tests -v
"""

import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import edinet_dividend  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "earnings_8309.json")
CODE_MAP = {"8309": "E03611"}


def load_8309():
    """8309 の earnings 配列（新しい順）。2026-07-30 Q1 が先頭。"""
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["data"]["earnings"]


def set_forecast(earnings, value, count):
    """新しい順に count 件のレコードの予想配当を書き換えたコピーを返す。"""
    earnings = copy.deepcopy(earnings)
    for record in earnings[:count]:
        record["forecast_dividend_per_share"] = value
    return earnings


def reduction_of(earnings):
    with mock.patch.object(edinet_dividend, "_fetch_earnings", return_value=earnings):
        return edinet_dividend._latest_reduction("E03611")


def is_cut_of(earnings):
    with mock.patch.object(edinet_dividend, "_fetch_earnings", return_value=earnings):
        return edinet_dividend._is_dividend_cut("E03611")


def synthetic(actual_adjusted=None, actual_raw=None, forecast=None, forecast_eps=None):
    """分割シグナルの無い銘柄の本決算1件ぶん。"""
    return [
        {
            "adjusted_annual_dividend_per_share": actual_adjusted,
            "dividend_per_share": actual_raw,
            "forecast_dividend_per_share": forecast,
            "forecast_eps": forecast_eps,
            "quarter": 4,
        }
    ]


class Split8309Test(unittest.TestCase):
    def test_split_is_not_reduction(self):
        # 185.0（分割前基準・未調整）vs 47.5（分割後基準）→ 46.25 → 47.5 の増配
        earnings = load_8309()
        self.assertIsNone(reduction_of(earnings))
        self.assertFalse(is_cut_of(earnings))
        with mock.patch.object(edinet_dividend, "API_KEY", "dummy"), mock.patch.object(
            edinet_dividend, "_fetch_earnings", return_value=earnings
        ):
            self.assertEqual(edinet_dividend.get_dividend_reductions(["8309"], code_map=CODE_MAP), {})
            self.assertEqual(edinet_dividend.get_dividend_cut_codes(["8309"], code_map=CODE_MAP), set())

    def test_real_cut_after_split_is_detected(self):
        # 分割後基準で 46.25 → 40 の本物の減配（旧4%ガードの外）
        earnings = set_forecast(load_8309(), 40.0, 2)
        self.assertEqual(reduction_of(earnings)[:2], (46.25, 40.0))

    def test_small_cut_after_split_is_detected(self):
        # 分割比の±4%に収まる小幅減配（46.25 → 45）も、抑止ではなく基準合わせなので拾える
        earnings = set_forecast(load_8309(), 45.0, 2)
        self.assertEqual(reduction_of(earnings)[:2], (46.25, 45.0))

    def test_raise_with_split_is_not_reduction(self):
        # 分割と同時の8%増配。常に_looks_like_splitを通す案では誤検知が残るケース
        earnings = set_forecast(load_8309(), 50.0, 2)
        self.assertIsNone(reduction_of(earnings))

    def test_pre_split_forecast_rewound_to_2026_01_30(self):
        # 2026-01-30 Q3 時点まで巻き戻す: 予想170（pre_split）vs 実績38.75（調整済み）
        earnings = load_8309()[2:]
        self.assertIsNone(reduction_of(earnings))
        # 予想を150に下げていれば 37.5 < 38.75 で減配。旧実装は 150 vs 38.75 で見逃していた
        cut = set_forecast(earnings, 150.0, 1)
        self.assertEqual(reduction_of(cut)[:2], (38.75, 37.5))

    def test_no_dividend_with_split_signal(self):
        # 無配転落(0.0)は is None で欠損扱いせず、減配として拾う
        earnings = set_forecast(load_8309(), 0.0, 2)
        self.assertEqual(reduction_of(earnings)[:2], (185.0, 0.0))


class NoSplitSignalTest(unittest.TestCase):
    def test_cut_at_split_like_ratio_is_detected(self):
        # 分割シグナルの無い銘柄では、分割比に近い比率の減配も今まで通り拾う
        for forecast in (50.0, 33.0, 25.0, 20.0, 10.0):
            with self.subTest(forecast=forecast):
                earnings = synthetic(actual_adjusted=100.0, actual_raw=100.0, forecast=forecast)
                self.assertEqual(reduction_of(earnings)[:2], (100.0, forecast))

    def test_raw_only_split_guard(self):
        # 調整後実績が無く生値にフォールバックした場合の既存ガード（回帰）
        earnings = synthetic(actual_raw=100.0, forecast=25.0)
        self.assertIsNone(reduction_of(earnings))

    def test_payout_filter(self):
        # 減配かつ予想配当性向>100% のみ候補から除外（回帰）
        self.assertTrue(is_cut_of(synthetic(actual_adjusted=100.0, forecast=80.0, forecast_eps=60.0)))
        self.assertFalse(is_cut_of(synthetic(actual_adjusted=100.0, forecast=80.0, forecast_eps=120.0)))


class FailOpenTest(unittest.TestCase):
    def test_api_error(self):
        self.assertIsNone(reduction_of(None))
        with mock.patch.object(edinet_dividend, "API_KEY", "dummy"), mock.patch.object(
            edinet_dividend, "_fetch_earnings", return_value=None
        ):
            self.assertEqual(edinet_dividend.get_dividend_reductions(["8309"], code_map=CODE_MAP), {})


if __name__ == "__main__":
    unittest.main()
