"""
Тесты чистых функций (без сети и Telegram). Запуск:
    BOT_TOKEN=x INSTALOAD_DATA_DIR=/tmp/instaload-test python -m pytest -q
"""

import json
import os
import time

import pytest

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("INSTALOAD_DATA_DIR", "/tmp/instaload-test")

from instaload import config, cookies, media  # noqa: E402
from instaload.bot import extract_urls  # noqa: E402
from instaload.state import State  # noqa: E402

NETSCAPE = """# Netscape HTTP Cookie File
.instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\t123%3Aabc%3A1
.instagram.com\tTRUE\t/\tTRUE\t1790000000\tcsrftoken\tXYZ
#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1790000000\tds_user_id\t123
.google.com\tTRUE\t/\tTRUE\t1790000000\tNID\tshould-be-dropped
.facebook.com\tTRUE\t/\tTRUE\t1790000000\tc_user\tdropped-too
"""

JSON_EXPORT = json.dumps([
    {"domain": ".instagram.com", "name": "sessionid", "value": "s1", "path": "/", "secure": True,
     "expirationDate": 1790000000.5},
    {"domain": ".instagram.com", "name": "csrftoken", "value": "c1", "path": "/", "secure": True},
    {"domain": ".youtube.com", "name": "VISITOR", "value": "x", "path": "/"},
])


def test_parse_netscape_filters_to_instagram_only():
    cs = cookies.parse_cookies(NETSCAPE.encode())
    names = {c.name for c in cs}
    assert names == {"sessionid", "csrftoken", "ds_user_id"}
    assert all(c.domain.endswith("instagram.com") for c in cs)
    text = cookies.to_netscape_text(cs)
    assert text.startswith("# Netscape HTTP Cookie File")
    assert "google" not in text and "facebook" not in text
    # результат снова парсится (round-trip)
    assert {c.name for c in cookies.parse_cookies(text)} == names


def test_parse_json_export():
    cs = cookies.parse_cookies(JSON_EXPORT)
    assert {c.name for c in cs} == {"sessionid", "csrftoken"}
    assert next(c for c in cs if c.name == "sessionid").expires == 1790000000


def test_parse_header_string():
    raw = "sessionid=abc%3A1; csrftoken=XYZ; ds_user_id=42"
    assert cookies.looks_like_header_string(raw)
    cs = cookies.parse_cookies(raw)
    assert {c.name for c in cs} == {"sessionid", "csrftoken", "ds_user_id"}
    assert all(c.domain == ".instagram.com" for c in cs)
    assert all(c.expires > time.time() for c in cs)


@pytest.mark.parametrize("raw,fragment", [
    ("", "пустой"),
    (".google.com\tTRUE\t/\tTRUE\t0\tNID\tx\n", "нет cookies для instagram"),
    (".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tx\n", "sessionid"),
    ("{not json", "JSON"),
    ("просто текст", "instagram"),
])
def test_parse_rejects_garbage(raw, fragment):
    with pytest.raises(cookies.CookieError, match=fragment):
        cookies.parse_cookies(raw)


def test_store_is_per_user_and_private(tmp_path):
    store = cookies.CookieStore(tmp_path / "cookies")
    store.save(111, NETSCAPE)
    assert store.has(111) and not store.has(222) and store.path_for(222) is None
    p = store.path_for(111)
    assert p is not None and oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "cookies").stat().st_mode & 0o777) == "0o700"
    assert store.count() == 1
    assert store.delete(111) and not store.has(111) and not store.delete(111)


def test_store_write_back_keeps_old_on_invalid(tmp_path):
    store = cookies.CookieStore(tmp_path / "cookies")
    store.save(5, NETSCAPE)
    upd = tmp_path / "upd.txt"
    upd.write_text(NETSCAPE.replace("XYZ", "ROTATED"))
    assert store.write_back(5, upd)
    assert "ROTATED" in store.path_for(5).read_text()
    upd.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tonly\n")
    assert not store.write_back(5, upd)
    assert "sessionid" in store.path_for(5).read_text()


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    st = State(path)
    st.set_caption("hello")
    st.set_quality(-100123, "480")
    st2 = State(path)
    assert st2.caption == "hello" and st2.quality[-100123] == "480"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_extract_urls_in_order():
    text = ("смотри https://youtu.be/jNQXAC9IVRw и https://www.instagram.com/reel/Cabc_12/?igsh=1 "
            "а ещё https://vm.tiktok.com/ZMabc/ и https://www.instagram.com/user.name/p/XyZ/ "
            "https://www.youtube.com/shorts/abcDEF123")
    got = extract_urls(text)
    assert [p for p, _ in got] == ["youtube", "instagram", "tiktok", "instagram", "youtube"]
    assert got[3][1] == "https://www.instagram.com/user.name/p/XyZ/"


def test_shortcode_and_natural_sort():
    assert media.shortcode_from_url("https://www.instagram.com/reel/Cabc_12/?x=1") == "Cabc_12"
    assert media.shortcode_from_url("https://www.instagram.com/p/XyZ-9/") == "XyZ-9"
    names = ["a_10.jpg", "a_2.jpg", "a_1.jpg"]
    assert sorted(names, key=media.natural_key) == ["a_1.jpg", "a_2.jpg", "a_10.jpg"]


def test_yt_format_size_ladder_then_fallback():
    f = media.yt_format("720")
    parts = f.split("/")
    assert parts[0].startswith("bv*[height<=720][ext=mp4][filesize<")
    assert any("height<=480" in p and "filesize<" in p for p in parts)
    assert any("height<=360" in p and "filesize<" in p for p in parts)
    assert parts[-1] == "b"
    f360 = media.yt_format("360")
    assert "height<=720" not in f360 and "height<=480" not in f360


def test_build_ytdlp_cmd(tmp_path):
    cmd = media.build_ytdlp_cmd("https://www.instagram.com/p/X/", "instagram", tmp_path, "720",
                                tmp_path / "c.txt")
    assert cmd[0] == config.YTDLP_BIN
    assert "--yes-playlist" in cmd and "--cookies" in cmd and cmd[-1] == "https://www.instagram.com/p/X/"
    assert f"{config.MAX_DOWNLOAD_MB}M" in cmd
    cmd_yt = media.build_ytdlp_cmd("https://youtu.be/x", "youtube", tmp_path, "480", None)
    assert "--no-playlist" in cmd_yt and "--cookies" not in cmd_yt


def test_explain_ytdlp_error():
    assert "cookies" in media.explain_ytdlp_error("ERROR: [Instagram] X: login required").lower()
    assert "приватное" in media.explain_ytdlp_error("ERROR: Private video").lower()
    assert media.explain_ytdlp_error("something odd") == ""
