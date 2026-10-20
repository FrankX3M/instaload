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

from instaload import config, cookies, links, media  # noqa: E402
from instaload.links import build_caption, clean_url, extract_urls  # noqa: E402
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


def test_extract_urls_deduplicates():
    t = "https://youtu.be/jNQXAC9IVRw https://youtu.be/jNQXAC9IVRw"
    assert len(extract_urls(t)) == 1


def test_clean_url_strips_tracking():
    assert clean_url("https://www.instagram.com/reel/Cabc_12/?igsh=abc&utm_source=x", "instagram") \
        == "https://www.instagram.com/reel/Cabc_12/"
    assert clean_url("https://www.youtube.com/watch?v=jNQXAC9IVRw&si=zzz&t=10", "youtube") \
        == "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=10"
    assert clean_url("https://youtu.be/jNQXAC9IVRw?si=zzz", "youtube") == "https://youtu.be/jNQXAC9IVRw"
    assert clean_url("https://vm.tiktok.com/ZMabc/?_r=1", "tiktok") == "https://vm.tiktok.com/ZMabc/"


def test_caption_always_contains_source():
    url = "https://www.instagram.com/reel/Cabc_12/?igsh=1"
    only = build_caption(url, "instagram")
    assert 'href="https://www.instagram.com/reel/Cabc_12/"' in only and "igsh" not in only
    with_text = build_caption(url, "instagram", "<b>hi</b> & co")
    assert with_text.startswith("&lt;b&gt;hi&lt;/b&gt; &amp; co") and with_text.endswith("</a>")
    # длинный текст обрезается, ссылка остаётся целой
    long = build_caption(url, "instagram", "x" * 5000)
    assert len(long) <= links.CAPTION_LIMIT and "Cabc_12" in long and long.endswith("</a>")
    # источник выключен
    assert build_caption(url, "instagram", with_source=False) is None
    assert build_caption(url, "instagram", "text", with_source=False) == "text"


# ─── YouTube cookies ─────────────────────────────────────────────────────────

YT_NETSCAPE = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t1790000000\tVISITOR_INFO1_LIVE\tvis
#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\tLOGIN_INFO\tlogin-yt
#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\t__Secure-3PSID\tyt-psid
#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1790000000\t__Secure-3PSID\tgoogle-psid
.google.com\tTRUE\t/\tTRUE\t1790000000\tSID\tgsid
.instagram.com\tTRUE\t/\tTRUE\t1790000000\tsessionid\tshould-not-be-in-yt
.facebook.com\tTRUE\t/\tTRUE\t1790000000\tc_user\tdropped
"""


def test_parse_youtube_keeps_youtube_and_google_only():
    cs = cookies.parse_cookies(YT_NETSCAPE, "youtube")
    assert {c.domain.lstrip(".") for c in cs} == {"youtube.com", "google.com"}
    assert "sessionid" not in {c.name for c in cs}
    # одноимённые cookies на разных доменах не затирают друг друга
    psid = {c.domain.lstrip("."): c.value for c in cs if c.name == "__Secure-3PSID"}
    assert psid == {"youtube.com": "yt-psid", "google.com": "google-psid"}
    text = cookies.to_netscape_text(cs, "youtube")
    assert "youtube.com / google.com" in text and "facebook" not in text and "instagram" not in text
    assert len(cookies.parse_cookies(text, "youtube")) == len(cs)


def test_detect_platforms():
    assert cookies.detect_platforms(NETSCAPE) == ["instagram"]
    assert cookies.detect_platforms(YT_NETSCAPE) == ["instagram", "youtube"]
    assert cookies.detect_platforms(YT_NETSCAPE.replace(".instagram.com", ".example.org")) == ["youtube"]
    assert cookies.detect_platforms("sessionid=abc; csrftoken=x") == ["instagram"]
    assert cookies.detect_platforms("SID=abc; HSID=x; __Secure-3PSID=y") == ["youtube"]
    assert cookies.detect_platforms(JSON_EXPORT) == ["instagram"]  # у youtube там нет cookies входа
    assert cookies.detect_platforms("") == [] and cookies.detect_platforms("{bad json") == []
    assert cookies.detect_platforms("просто текст") == []
    # без cookies входа — всё равно youtube (по домену), чтобы показать конкретную ошибку
    assert cookies.detect_platforms(".google.com\tTRUE\t/\tTRUE\t0\tNID\tx\n") == ["youtube"]
    # случайная cookie google.com в экспорте Instagram не делает его «YouTube»
    assert cookies.detect_platforms(NETSCAPE) == ["instagram"]


def test_youtube_header_string():
    raw = "SID=abc; HSID=h; __Secure-3PSID=p; LOGIN_INFO=l"
    assert cookies.header_string_platform(raw) == "youtube"
    assert cookies.header_string_platform("sessionid=1; csrftoken=2") == "instagram"
    assert cookies.header_string_platform("привет SID=1") is None
    cs = cookies.parse_cookies(raw, "youtube")
    assert {c.name for c in cs} == {"SID", "HSID", "__Secure-3PSID", "LOGIN_INFO"}
    assert all(c.domain == ".youtube.com" for c in cs)


def test_youtube_requires_login_cookies():
    only_visitor = ".youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tx\n"
    with pytest.raises(cookies.CookieError, match="входа"):
        cookies.parse_cookies(only_visitor, "youtube")
    with pytest.raises(cookies.CookieError, match="youtube.com"):
        cookies.parse_cookies(".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tx\n", "youtube")


def test_youtube_store_is_separate_from_instagram(tmp_path):
    ig = cookies.CookieStore(tmp_path / "cookies")
    yt = cookies.CookieStore(tmp_path / "cookies" / "youtube", platform="youtube")
    ig.save(7, NETSCAPE)
    assert ig.has(7) and not yt.has(7) and ig.count() == 1 and yt.count() == 0
    yt.save(7, YT_NETSCAPE)
    assert yt.has(7) and ig.count() == 1 and yt.count() == 1
    assert "instagram" not in yt.path_for(7).read_text()
    assert oct(yt.path_for(7).stat().st_mode & 0o777) == "0o600"
    upd = tmp_path / "upd.txt"
    upd.write_text(YT_NETSCAPE.replace("login-yt", "ROTATED"))
    assert yt.write_back(7, upd) and "ROTATED" in yt.path_for(7).read_text()
    upd.write_text(".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tonly\n")
    assert not yt.write_back(7, upd) and "ROTATED" in yt.path_for(7).read_text()
    assert yt.delete(7) and ig.has(7)


def test_save_and_write_back_shared(tmp_path):
    shared = tmp_path / "youtube_shared.txt"
    cookies.save_shared(shared, YT_NETSCAPE, "youtube")
    assert oct(shared.stat().st_mode & 0o777) == "0o600" and "instagram" not in shared.read_text()
    upd = tmp_path / "upd.txt"
    upd.write_text(shared.read_text().replace("login-yt", "ROTATED"))
    assert cookies.write_back_shared(str(shared), upd, "youtube") and "ROTATED" in shared.read_text()
    with pytest.raises(cookies.CookieError):
        cookies.save_shared(tmp_path / "x.txt", NETSCAPE, "youtube")


def test_youtube_auth_hint_is_explained():
    assert media.explain_ytdlp_error("ERROR: Sign in to confirm you’re not a bot") == media.YT_AUTH_HINT
