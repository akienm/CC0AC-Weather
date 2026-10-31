"""The rain chart's hourly bars come from the hub's running daily total. python3 -m unittest tests/test_rain_chart.py"""
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from cc0ac import store   # noqa: E402


class RainBuckets(unittest.TestCase):
    def rain(self, totals):
        """Bar heights for one-hour buckets, for [(minutes after 00:00Z, daily total)]."""
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE readings (type TEXT, received_utc TEXT, rain_day_in REAL)")
        for minute, total in totals:
            con.execute("INSERT INTO readings VALUES ('Atlas', ?, ?)",
                        [f"2026-10-30T{minute // 60:02d}:{minute % 60:02d}:00Z", total])
        lo = store.iso_to_epoch("2026-10-30T00:00:00Z")
        out = store._rain_buckets(con, lo, lo + 86400, 3600, 0)
        return sum(out["in"])

    def test_rain_adds_up_to_what_fell(self):
        self.assertAlmostEqual(self.rain([(0, 0.0), (60, 0.1), (120, 0.3)]), 0.3)

    def test_a_dip_and_recovery_is_not_rain_twice(self):
        self.assertAlmostEqual(self.rain([(0, 0.0), (60, 0.32), (61, 0.31), (62, 0.32), (120, 0.33)]), 0.33)

    def test_the_midnight_reset_still_counts_the_new_days_rain(self):
        self.assertAlmostEqual(self.rain([(0, 0.3), (60, 0.0), (120, 0.1)]), 0.1)


if __name__ == "__main__":
    unittest.main()
