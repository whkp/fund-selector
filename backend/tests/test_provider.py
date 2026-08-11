from app.providers import AKShareProvider


def test_history_period_is_limited_against_the_latest_nav_date():
    records = [
        {"date": "2024-08-09", "nav": 1.0, "dailyChange": 0.0},
        {"date": "2025-08-11", "nav": 1.1, "dailyChange": 0.0},
        {"date": "2026-08-10", "nav": 1.2, "dailyChange": 0.0},
        {"date": "2026-08-11", "nav": 1.3, "dailyChange": 0.0},
    ]

    limited = AKShareProvider._limit_history_period(records, "1年")

    assert [record["date"] for record in limited] == ["2025-08-11", "2026-08-10", "2026-08-11"]


def test_history_period_keeps_full_series_when_period_is_unknown():
    records = [{"date": "2020-01-01", "nav": 1.0, "dailyChange": 0.0}]
    assert AKShareProvider._limit_history_period(records, "成立以来") == records
