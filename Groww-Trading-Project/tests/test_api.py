"""Flask API + data loader + Groww helper tests (against the test Postgres DB)."""
import time as _time
from datetime import date

import pandas as pd
import pytest

from app.data_loader import DataLoader, DemoSource
from app.db import PgStore
from app.groww_client import GrowwAuthError, parse_candles, read_keys


@pytest.fixture(scope="module")
def loaded_db(module_db):
    DataLoader(module_db, DemoSource()).run(date(2026, 5, 4), date(2026, 5, 22))
    return module_db


@pytest.fixture(scope="module")
def client(loaded_db):
    from app.web import create_app

    class NoGroww:
        @property
        def api(self):
            raise GrowwAuthError("no keys in tests")

        def profile(self):
            raise GrowwAuthError("no keys in tests")

    app = create_app(db=loaded_db, groww_client=NoGroww())
    app.testing = True
    return app.test_client()


def test_loader_is_incremental(loaded_db):
    cov = loaded_db.coverage()
    assert cov["source"] == "demo" and cov["spot_days"] >= 15 and cov["option_contracts"] > 0
    v = loaded_db.data_version()
    p = {}
    DataLoader(loaded_db, DemoSource(), p).run(date(2026, 5, 4), date(2026, 5, 22), refresh_today=False)
    assert p["total"] == 0                       # nothing re-fetched
    assert loaded_db.data_version() != v         # but version bumped


def test_pgstore_roundtrip(loaded_db):
    st = PgStore(loaded_db)
    s = st.spot_1m(date(2026, 5, 4), date(2026, 5, 4))
    assert len(s) == 375 and s.index[0] == pd.Timestamp("2026-05-04 09:15")
    assert all(e.weekday() == 1 for e in st.expiries())


def test_pages_render(client):
    for path in ("/", "/backtest", "/live", "/data"):
        r = client.get(path)
        assert r.status_code == 200 and b"9/21" in r.data


def test_health_and_coverage(client):
    assert client.get("/api/health").json["db"] is True
    assert client.get("/api/data/coverage").json["source"] == "demo"


def test_account_without_keys_is_401(client):
    r = client.get("/api/account")
    assert r.status_code == 401 and "no keys" in r.json["error"]


def test_backtest_api_and_caching(client):
    body = {"from": "2026-05-11", "to": "2026-05-22", "timeframe": 5, "entry_start": "09:30", "entry_cutoff": "14:30"}
    r1 = client.post("/api/backtest", json=body)
    assert r1.status_code == 200, r1.json
    assert r1.json["cache"] == "computed"
    assert set(r1.json["metrics"]) == {"ITM5_BUY", "ATM_BUY", "ATM_SELL"}
    assert r1.json["trades"]
    r2 = client.post("/api/backtest", json=body)
    assert r2.json["cache"] == "memory" and r2.json["trades"] == r1.json["trades"]


def test_backtest_api_validation(client):
    assert client.post("/api/backtest", json={"timeframe": 7}).status_code == 400
    assert client.post("/api/backtest", json={"entry_start": "13:00", "entry_cutoff": "12:00"}).status_code == 400
    assert client.post("/api/backtest", json={"from": "nope"}).status_code == 400


def test_chart_api(client):
    t = client.post("/api/backtest", json={"from": "2026-05-11", "to": "2026-05-22", "timeframe": 15}).json["trades"][0]
    r = client.get(f"/api/chart?day={t['day']}&tf=15&symbol={t['symbol']}")
    assert r.status_code == 200
    d = r.json
    assert len(d["spot"]) == 25 and d["option"] and d["ema21"]
    times = [c["time"] for c in d["spot"]]
    assert times == sorted(times)
    assert client.get("/api/chart?day=bad").status_code == 400


def test_live_endpoints_without_keys(client):
    assert client.get("/api/live/state").json["status"] == "IDLE"
    r = client.post("/api/live/start", json={"exec_mode": "LIVE"})
    assert r.status_code == 400 and "LIVE" in r.json["error"]
    assert client.post("/api/live/stop", json={}).status_code == 409
    assert client.get("/api/live/history").status_code == 200


def test_replay_endpoint(client):
    r = client.post("/api/live/replay", json={"day": "2026-05-12", "strategy_mode": "ATM_BUY", "timeframe": 5})
    assert r.status_code == 200, r.json
    assert r.json["status"] in ("DONE", "RUNNING", "HALTED")
    assert client.post("/api/live/replay", json={"day": "2020-01-01"}).status_code == 404
    assert client.post("/api/live/replay", json={}).status_code == 400


def test_download_demo_job(client):
    r = client.post("/api/data/download", json={"source": "demo", "from": "2026-05-25", "to": "2026-05-29"})
    assert r.status_code == 200
    for _ in range(100):
        p = client.get("/api/data/progress").json
        if not p["running"]:
            break
        _time.sleep(0.2)
    assert p["state"] == "done"
    assert client.post("/api/data/download", json={"from": "2026-05-29", "to": "2026-05-01"}).status_code == 400


# ------------------------------------------------------------------ groww helpers
def test_read_keys_formats(tmp_path):
    f = tmp_path / "keys.txt"
    f.write_text("# comment\nAPI_KEY=abc.def\nTOTP_SECRET = JBSW Y3DP\n")
    assert read_keys(f) == ("abc.def", "JBSWY3DP")
    f.write_text("abc.def\nJBSWY3DP\n")
    assert read_keys(f) == ("abc.def", "JBSWY3DP")
    f.write_text("only-one-line\n")
    with pytest.raises(GrowwAuthError):
        read_keys(f)
    with pytest.raises(GrowwAuthError):
        read_keys(tmp_path / "missing.txt")


def test_parse_candles_formats():
    epoch = int(pd.Timestamp("2026-03-02 09:15", tz="Asia/Kolkata").timestamp())
    df = parse_candles({"candles": [[epoch, 1, 2, 0.5, 1.5, 100], [epoch * 1000 + 60000, 1, 2, 0.5, 1.6, 100, 9]]})
    assert list(df.index) == [pd.Timestamp("2026-03-02 09:15"), pd.Timestamp("2026-03-02 09:16")]
    assert df["close"].tolist() == [1.5, 1.6]
    df2 = parse_candles({"candles": [["2026-03-02T09:15:00", 1, 2, 0.5, 1.5, 100, None]]})
    assert df2.index[0] == pd.Timestamp("2026-03-02 09:15")
    assert parse_candles({"candles": []}).empty
    assert parse_candles(None).empty
