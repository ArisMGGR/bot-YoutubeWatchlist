import os
import shutil
import subprocess
import sys

import pytest

import archive
import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- test isolation --------------------------------------------------------- #
def test_project_conf_env_does_not_reach_the_tests(tmp_path):
    """Regression (F-047): conftest let config.py load the project's real
    conf.env, so a configured install (e.g. MAX_VIDEO_AGE_DAYS=1) failed tests.
    Runs conftest against a scratch project that has such a conf.env."""
    shutil.copy(os.path.join(ROOT, "config.py"), tmp_path)
    (tmp_path / "tests").mkdir()
    shutil.copy(os.path.join(ROOT, "tests", "conftest.py"), tmp_path / "tests")
    (tmp_path / "conf.env").write_text(
        "MAX_VIDEO_AGE_DAYS=1\nYOUTUBE_API_KEY=real\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_probe.py").write_text(
        "import config\n"
        "def test_defaults():\n"
        "    assert config.MAX_VIDEO_AGE_DAYS == 14\n"
        "    assert config.YOUTUBE_API_KEY == '' and config.TELEGRAM_CHAT_ID == ''\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("MAX_VIDEO_AGE_DAYS", "YOUTUBE_API_KEY", "TELEGRAM_CHAT_ID")}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_gitignore_covers_downloaded_client_secret_names(tmp_path):
    """Regression (F-048): only the exact name client_secret.json was ignored,
    not the name Google downloads an OAuth client JSON under."""
    shutil.copy(os.path.join(ROOT, ".gitignore"), tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    names = ["client_secret.json", "client_secret_123-abc.apps.googleusercontent.com.json"]
    proc = subprocess.run(["git", "check-ignore", *names], cwd=tmp_path,
                          capture_output=True, text=True)
    assert proc.stdout.split() == names


# --- config._load_dotenv ---------------------------------------------------- #
def test_dotenv_tolerates_bom(tmp_path, monkeypatch):
    p = tmp_path / "conf.env"
    p.write_bytes(b"\xef\xbb\xbfTELEGRAM_BOT_TOKEN=123:abc\n")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    config._load_dotenv(str(p))
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:abc"


def test_dotenv_skips_comments_blank_and_malformed(tmp_path, monkeypatch):
    p = tmp_path / "conf.env"
    p.write_text("# c\n\nNOEQUALS\nX_KEY = 'quoted' \n", encoding="utf-8")
    monkeypatch.delenv("X_KEY", raising=False)
    config._load_dotenv(str(p))
    assert os.environ["X_KEY"] == "quoted"
    assert "NOEQUALS" not in os.environ


def test_dotenv_repeated_key_last_wins(tmp_path, monkeypatch):
    """Regression (F-006): config.py took the first line, systemd's
    EnvironmentFile= the last, so the service and a manual run disagreed."""
    p = tmp_path / "conf.env"
    p.write_text("X_KEY=\nX_KEY=real\n", encoding="utf-8")
    monkeypatch.delenv("X_KEY", raising=False)
    config._load_dotenv(str(p))
    assert os.environ["X_KEY"] == "real"


def test_dotenv_skips_a_line_without_a_key(tmp_path, monkeypatch):
    """Regression (F-084): a `=value` line was passed to os.environ with an
    empty name, and the ValueError stopped every entry point at import."""
    p = tmp_path / "conf.env"
    p.write_text("=oops\n  = also\nX_KEY=kept\n", encoding="utf-8")
    monkeypatch.delenv("X_KEY", raising=False)
    config._load_dotenv(str(p))
    assert os.environ["X_KEY"] == "kept"


def test_empty_path_setting_means_default(monkeypatch):
    """Regression (F-018): `ARCHIVE_FILE=` resolved to the code folder itself."""
    monkeypatch.setenv("SOME_FILE", "  ")
    assert config._path("SOME_FILE", "archive.json") == os.path.join(config.HERE, "archive.json")


@pytest.mark.parametrize("value", ["0", "-5"])
def test_check_interval_is_at_least_one_minute(monkeypatch, value):
    """Regression (F-056): 0 or a negative interval was kept as is, so /help
    said "Every 0 minute(s)" while the loop checked every minute."""
    import importlib.util

    monkeypatch.setenv("CHECK_INTERVAL_MINUTES", value)
    spec = importlib.util.spec_from_file_location("config_probe", os.path.join(ROOT, "config.py"))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.CHECK_INTERVAL_MINUTES == 1


def test_dotenv_does_not_override_real_environment(tmp_path, monkeypatch):
    p = tmp_path / "conf.env"
    p.write_text("X_KEY=file\n", encoding="utf-8")
    monkeypatch.setenv("X_KEY", "env")
    config._load_dotenv(str(p))
    assert os.environ["X_KEY"] == "env"


# --- config file-path anchoring -------------------------------------------- #
def test_paths_are_anchored_to_code_folder(monkeypatch):
    monkeypatch.setenv("SOME_FILE", "relative.json")
    assert config._path("SOME_FILE", "x") == os.path.join(config.HERE, "relative.json")
    monkeypatch.delenv("SOME_FILE")
    assert config._path("SOME_FILE", "archive.json") == os.path.join(config.HERE, "archive.json")
    absolute = os.path.abspath(os.sep + "elsewhere")
    monkeypatch.setenv("SOME_FILE", absolute)
    assert config._path("SOME_FILE", "x") == absolute


# --- archive ---------------------------------------------------------------- #
def test_load_missing_or_corrupt_archive_gives_defaults(tmp_path):
    missing = archive.load(str(tmp_path / "nope.json"))
    assert missing["channels"] == [] and missing["seen"] == {} and missing["resolved"] == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert archive.load(str(bad))["seen"] == {}
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    assert archive.load(str(lst))["channels"] == []


def test_save_is_atomic_and_round_trips(tmp_path):
    path = str(tmp_path / "a.json")
    data = archive.load(path)
    data["seen"]["v1"] = {"title": "t", "channel": "c", "channel_id": "UC1"}
    archive.save(path, data)
    assert not os.path.exists(path + ".tmp")
    assert archive.load(path)["seen"]["v1"]["channel_id"] == "UC1"

    # Regression (F-083): no .tmp left over is also true of a write in place,
    # so make a save fail midway and require the previous archive intact. A
    # lone surrogate can't be encoded, so json.dump raises while writing.
    broken = archive.load(path)
    broken["seen"]["v2"] = {"title": "\ud800", "channel": "c", "channel_id": "UC1"}
    with pytest.raises(UnicodeEncodeError):
        archive.save(path, broken)
    assert list(archive.load(path)["seen"]) == ["v1"]


def test_archive_with_a_bom_is_read(tmp_path, caplog):
    """Regression (F-080): archive.json was read as strict UTF-8, so a BOM
    from a Windows editor (the README says to hand-edit it) silently reset the
    whole archive, and a non-UTF-8 byte crashed the bot at startup."""
    path = tmp_path / "a.json"
    path.write_text('{"channels": ["UC1"], "notify_chat_id": "5"}', encoding="utf-8-sig")
    data = archive.load(str(path))
    assert data["channels"] == ["UC1"] and data["notify_chat_id"] == "5"

    path.write_bytes(b'{"channels": ["Caf\xe9"]}')  # cp1252, not UTF-8
    with caplog.at_level("WARNING"):
        assert archive.load(str(path))["channels"] == []
    assert "Couldn't read" in caplog.text


def test_save_check_keeps_notify_target_registered_meanwhile(tmp_path):
    """Regression (F-001): a check saved the snapshot it loaded before /start,
    wiping the chat/channel /start had just persisted."""
    path = str(tmp_path / "a.json")
    snapshot = archive.load(path)          # the check starts
    archive.set_keys(path, notify_chat_id="111",       # /start during the check
                     discord_channel_id="5", discord_guild_id="100")
    snapshot["channels"].append("UC1")
    archive.save_check(path, snapshot)     # the check finishes
    saved = archive.load(path)
    assert saved["notify_chat_id"] == "111" and saved["discord_channel_id"] == "5"
    assert saved["discord_guild_id"] == "100" and saved["channels"] == ["UC1"]


def test_legacy_archive_counts_as_announced(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"channels": ["UC1"], "seen": {}}', encoding="utf-8")
    assert archive.load(str(path))["setup_announced"] is True
    assert archive.load(str(tmp_path / "new.json"))["setup_announced"] is False


def _video(vid):
    return {"video_id": vid, "title": "t", "channel_title": "Chan", "channel_id": "UCx"}


def test_mark_seen_records_channel_id():
    data = archive.load("/nonexistent/archive.json")
    archive.mark_seen(data, _video("a"))
    archive.mark_seen(data, {"video_id": "b", "title": "t"}, "Other", "UCy")
    assert data["seen"]["a"] == {"title": "t", "channel": "Chan", "channel_id": "UCx"}
    assert data["seen"]["b"] == {"title": "t", "channel": "Other", "channel_id": "UCy"}


def test_prune_is_per_channel_and_keeps_newest():
    data = archive.load("/nonexistent/archive.json")
    for cid in ("UC1", "UC2"):
        for i in range(60):
            archive.mark_seen(data, {"video_id": f"{cid}-{i}", "title": "t"}, "n", cid)
    data["seen"]["legacy"] = {"title": "old", "channel": "n"}  # no channel_id
    archive.prune(data, keep_per_channel=50)
    ids = set(data["seen"])
    assert "legacy" in ids
    for cid in ("UC1", "UC2"):
        kept = [k for k in ids if k.startswith(cid)]
        assert len(kept) == 50
        assert f"{cid}-0" not in ids and f"{cid}-59" in ids


def test_prune_never_evicts_a_seeded_video_for_many_channels():
    """Regression: a global cap evicted seeded ids once subscriptions × 15
    exceeded it, so they were re-notified on every check."""
    data = archive.load("/nonexistent/archive.json")
    for c in range(400):
        for j in range(15):
            archive.mark_seen(data, {"video_id": f"UC{c}-v{j}", "title": "t"}, "n", f"UC{c}")
    archive.prune(data)
    assert len(data["seen"]) == 400 * 15
