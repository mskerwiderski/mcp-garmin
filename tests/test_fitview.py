from garmin_mcp import fitview


def test_metrics_reads_the_session(fit_bytes):
    m = fitview.metrics(fit_bytes)
    assert m["sport"] == "running"
    assert 590 <= m["duration_s"] <= 600
    assert 1800 < m["distance_m"] < 2000
    assert m["avg_hr"] == 139
    assert "laps" not in m                      # laps belong to the stream tool


def test_streams_are_resampled_and_summarised(fit_bytes):
    view = fitview.stream_view(fit_bytes, max_points=50)
    assert view["n_points"] == 50
    assert view["n_source_points"] > 50
    assert "hr" in view["available_channels"]
    hr = view["channels"]["hr"]
    assert len(hr["values"]) == 50
    assert hr["min"] >= 120 and hr["max"] <= 160
    assert len(view["t_s"]) == 50


def test_channel_filter_and_point_cap(fit_bytes):
    view = fitview.stream_view(fit_bytes, channels=["hr"], max_points=10_000)
    assert list(view["channels"]) == ["hr"]
    assert view["n_points"] <= fitview.MAX_POINTS_CAP


def test_short_series_is_not_padded(fit_bytes):
    view = fitview.stream_view(fit_bytes, max_points=500)
    assert len(view["t_s"]) == view["n_points"] <= 500


def test_sensors_empty_for_a_file_without_ciq(fit_bytes):
    assert fitview.sensors(fit_bytes) == []
