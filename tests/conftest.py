import datetime as dt

import pytest
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import FileType, Manufacturer, Sport

BASE = dt.datetime(2026, 8, 20, 6, 30, 0, tzinfo=dt.timezone.utc)
BASE_MS = int(BASE.timestamp() * 1000)


def build_fit(n=600, speed_mps=3.2, hr0=120) -> bytes:
    """A plain one-session run: n records at 1 Hz, one lap, one session."""
    b = FitFileBuilder(auto_define=True)
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.time_created = BASE_MS
    b.add(fid)
    for i in range(n):
        r = RecordMessage()
        r.timestamp = BASE_MS + i * 1000
        r.distance = float(i) * speed_mps
        r.speed = speed_mps
        r.heart_rate = hr0 + (i % 40)
        r.power = 200 + (i % 50)
        r.altitude = 100.0 + (i % 20)
        b.add(r)
    lap = LapMessage()
    lap.start_time = BASE_MS
    lap.timestamp = BASE_MS + (n - 1) * 1000
    lap.total_elapsed_time = float(n - 1)
    lap.total_timer_time = float(n - 1)
    lap.total_distance = (n - 1) * speed_mps
    b.add(lap)
    s = SessionMessage()
    s.sport = Sport.RUNNING.value
    s.start_time = BASE_MS
    s.timestamp = BASE_MS + (n - 1) * 1000
    s.total_elapsed_time = float(n - 1)
    s.total_timer_time = float(n - 1)
    s.total_distance = (n - 1) * speed_mps
    s.avg_heart_rate = 139
    s.max_heart_rate = 159
    s.avg_power = 224
    s.max_power = 249
    s.avg_speed = speed_mps
    s.max_speed = speed_mps
    b.add(s)
    return bytes(b.build().to_bytes())


@pytest.fixture(scope="session")
def fit_bytes() -> bytes:
    return build_fit()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may read or write the real ~/.garmin-mcp."""
    monkeypatch.setenv("GARMIN_TOKENS_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("GARMIN_MCP_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MCP_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("APP_SECRET", "test-secret")
    monkeypatch.delenv("GARMIN_TOKENS", raising=False)
    from garmin_mcp import db
    db.init()
    return tmp_path


@pytest.fixture
def user_id():
    """A registered, invited account."""
    from garmin_mcp import users
    return users.create_user("anja@example.com", "supersecret123",
                             users.create_invite("Anja"))
