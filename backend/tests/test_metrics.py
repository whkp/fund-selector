from app.metrics import calculate_metrics


def test_metrics_are_calculated_from_history():
    result = calculate_metrics([1.0, 1.1, 1.05, 1.2])
    assert result["status"] == "PASS"
    assert result["totalReturn"] == 20.0
    assert result["drawdown"] < 0
    assert result["sampleCount"] == 4


def test_metrics_report_insufficient_history():
    assert calculate_metrics([1.0])["status"] == "INSUFFICIENT_HISTORY"
