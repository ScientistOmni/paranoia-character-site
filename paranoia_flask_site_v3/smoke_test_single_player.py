"""1人テストモードの簡易E2Eテスト。Flask等の依存関係を入れてから実行する。"""
import os
import tempfile
from pathlib import Path

# appをimportする前に環境変数を設定する。
os.environ["PLAYER_COUNT"] = "1"
os.environ["ROOM_CODE"] = "smoke-test-room"
os.environ["FLASK_SECRET_KEY"] = "smoke-test-secret-key-do-not-use-in-production"
_db_file = tempfile.NamedTemporaryFile(prefix="paranoia_smoke_", suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_PATH"] = _db_file.name

import app as site  # noqa: E402

try:
    client = site.app.test_client()
    response = client.get("/login")
    assert response.status_code == 200, ("login GET", response.status_code)

    response = client.post("/login", data={
        "room_code": "smoke-test-room", "player_id": "0", "name": "Tester"
    }, follow_redirects=True)
    assert response.status_code == 200, ("login POST", response.status_code)

    response = client.post("/ready", follow_redirects=True)
    assert response.status_code == 200, ("start", response.status_code)

    for round_number in range(1, 6):
        response = client.get("/round")
        assert response.status_code == 200, ("round GET", round_number, response.status_code)
        response = client.post("/round", data={
            "preferences": ["1", "2", "3"], "bid": "10"
        }, follow_redirects=True)
        assert response.status_code == 200, ("submit/result", round_number, response.status_code)
        assert b"UndefinedError" not in response.data
        response = client.get("/results")
        assert response.status_code == 200, ("results", round_number, response.status_code)
        response = client.post("/next-round", follow_redirects=True)
        assert response.status_code == 200, ("next/final", round_number, response.status_code)

    response = client.get("/final")
    assert response.status_code == 200, ("final page", response.status_code)
    with site.connect() as connection:
        game = site.load(connection)
    assert game["phase"] == "finished", game["phase"]
    assert game["players"][0]["state"]["values"] == [0] * 16
    assert game["players"][0]["state"]["xp"] == 100
    print("OK: 1人で5ラウンドを完走し、結果・最終画面を表示できました。能力値は全て0、XPは100のままです。")
finally:
    try:
        Path(_db_file.name).unlink(missing_ok=True)
    except OSError:
        pass
