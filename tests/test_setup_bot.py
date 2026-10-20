"""setup_bot.py helpers. Standard library only, so these run on any install —
including a single-backend venv made by setup_bot.py itself."""
import os
import shutil
import subprocess
import sys

import pytest

import config
import setup_bot


def test_ask_clear_token(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert setup_bot.ask("x", "123") == "123"
    monkeypatch.setattr("builtins.input", lambda prompt="": setup_bot.CLEAR)
    assert setup_bot.ask("x", "123") == ""


def test_ask_number_rejects_negative_then_accepts(monkeypatch):
    answers = iter(["-1", "abc", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert setup_bot.ask_number("n", "3", int) == "2"


def test_read_conf_tolerates_bom(tmp_path):
    p = tmp_path / "conf.env"
    p.write_bytes(b"\xef\xbb\xbfTELEGRAM_BOT_TOKEN=t\n# X=1\nY = 'q'\n")
    assert setup_bot.read_conf(p) == {"TELEGRAM_BOT_TOKEN": "t", "Y": "q"}


def test_set_conf_key_leaves_exactly_one_active_line(tmp_path):
    """Regression: uncommenting the example's `# CHAT_PLATFORM=` while a later
    active line existed left two active lines."""
    p = tmp_path / "conf.env"
    p.write_text("# CHAT_PLATFORM=telegram\nTOKEN=x\nCHAT_PLATFORM=discord\n", encoding="utf-8")
    setup_bot.set_conf_key(p, "CHAT_PLATFORM", "telegram")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert [l for l in lines if l.startswith("CHAT_PLATFORM=")] == ["CHAT_PLATFORM=telegram"]
    assert lines[0].startswith("#")

    p.write_text("# CHAT_PLATFORM=telegram\nTOKEN=x\n", encoding="utf-8")
    setup_bot.set_conf_key(p, "CHAT_PLATFORM", "discord")
    assert p.read_text(encoding="utf-8").splitlines()[0] == "CHAT_PLATFORM=discord"

    p.write_text("TOKEN=x\n", encoding="utf-8")
    setup_bot.set_conf_key(p, "CHAT_PLATFORM", "discord")
    assert p.read_text(encoding="utf-8").splitlines() == ["TOKEN=x", "CHAT_PLATFORM=discord"]


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0
    or not shutil.which("bash") or not shutil.which("id"),
    reason="needs a POSIX shell, not as root (the installer refuses User=root)",
)
def test_install_service_writes_paths_with_sed_metacharacters(tmp_path):
    """Regression (F-038): `&` or `|` in the project or python path was read as
    sed syntax, so the rendered unit had mangled paths."""
    import getpass

    project = tmp_path / "R&D|bot"
    shutil.copytree(setup_bot.SETUP_DIR / "deploy", project / "manual-setup" / "deploy")
    env = dict(os.environ, SERVICE_USER=getpass.getuser(), PYTHON=sys.executable)
    out = subprocess.run(
        ["bash", str(project / "manual-setup" / "deploy" / "install-service.sh"), "--dry-run"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout
    unit = out.split("-" * 70)[1]
    assert f"WorkingDirectory={project}\n" in unit
    assert f"EnvironmentFile=-{project}/conf.env\n" in unit
    assert f'ExecStart="{sys.executable}" ytwatchlistbot.py\n' in unit


def test_set_conf_key_replaces_a_spaced_active_line(tmp_path, monkeypatch):
    """Regression (F-037): a hand-written `CHAT_PLATFORM = telegram` line wasn't
    seen as active, so the switch went into the commented line above it and the
    later spaced line still won in config.py."""
    p = tmp_path / "conf.env"
    p.write_text(
        "# CHAT_PLATFORM=telegram\nTELEGRAM_BOT_TOKEN=t\nCHAT_PLATFORM = telegram\n",
        encoding="utf-8",
    )
    setup_bot.set_conf_key(p, "CHAT_PLATFORM", "discord")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines == ["# CHAT_PLATFORM=telegram", "TELEGRAM_BOT_TOKEN=t", "CHAT_PLATFORM=discord"]
    monkeypatch.delenv("CHAT_PLATFORM", raising=False)
    config._load_dotenv(str(p))
    assert config.os.environ["CHAT_PLATFORM"] == "discord"


_PLATFORM_CASES = [
    {"CHAT_PLATFORM": "Discord"},
    {"CHAT_PLATFORM": "telegram", "DISCORD_BOT_TOKEN": "d"},
    {"CHAT_PLATFORM": "bogus", "DISCORD_BOT_TOKEN": "d"},
    {"DISCORD_BOT_TOKEN": "d"},
    {"TELEGRAM_BOT_TOKEN": "t"},
    {"DISCORD_BOT_TOKEN": "d", "TELEGRAM_BOT_TOKEN": "t"},
    {},
]


def test_configured_platform_matches_config_py_rule(monkeypatch, tmp_path):
    """Regression (F-021): this used to check hard-coded answers only, so it
    couldn't notice setup_bot and config.py disagreeing (they did, for an
    unknown CHAT_PLATFORM value). Now each case runs through both."""
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)  # config's warning
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)  # no token files here
    for existing in _PLATFORM_CASES:
        if "CHAT_PLATFORM" in existing:
            monkeypatch.setenv("CHAT_PLATFORM", existing["CHAT_PLATFORM"])
        else:
            monkeypatch.delenv("CHAT_PLATFORM", raising=False)
        monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", existing.get("DISCORD_BOT_TOKEN", ""))
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", existing.get("TELEGRAM_BOT_TOKEN", ""))
        # setup_bot reports "" when nothing decides; main() treats that as telegram.
        from_setup = setup_bot.configured_platform(existing) or "telegram"
        assert from_setup == config._chat_platform(), existing


def test_write_conf_carries_unknown_keys(tmp_path, monkeypatch):
    conf = tmp_path / "conf.env"
    monkeypatch.setattr(setup_bot, "CONF", conf)
    answers = {"DISCORD_BOT_TOKEN": "d", "CHECK_INTERVAL_MINUTES": "30"}
    existing = {"TELEGRAM_BOT_TOKEN": "t", "YOUTUBE_DEVICE_CLIENT_SECRET_FILE": "dev.json",
                "CHAT_PLATFORM": "telegram"}
    setup_bot.write_conf("discord", answers, existing)
    got = setup_bot.read_conf(conf)
    assert got["CHAT_PLATFORM"] == "discord"
    assert got["DISCORD_BOT_TOKEN"] == "d"
    assert got["TELEGRAM_BOT_TOKEN"] == "t"
    assert got["YOUTUBE_DEVICE_CLIENT_SECRET_FILE"] == "dev.json"
    assert "CHECK_INTERVAL_MINUTES" not in got  # at default -> commented out


def test_configured_platform_reads_token_files(monkeypatch, tmp_path):
    """Regression (F-025): config.py falls back to token.txt /
    discord_token.txt, setup_bot didn't, so they named different platforms."""
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)
    (tmp_path / "discord_token.txt").write_text("d\n", encoding="utf-8")
    assert setup_bot.configured_platform({}) == "discord"
    assert setup_bot.configured_token({}, "DISCORD_BOT_TOKEN") == "d"
    (tmp_path / "token.txt").write_text("t\n", encoding="utf-8")
    assert setup_bot.configured_platform({}) == "telegram"


def test_youtube_step_empty_path_means_default(monkeypatch, tmp_path):
    """Regression (F-026): `YOUTUBE_TOKEN_FILE=` resolved to the project
    folder, which exists, so step 6 said YouTube was already connected."""
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)
    said, asked = [], []
    monkeypatch.setattr(setup_bot, "say", lambda m="": said.append(m))
    monkeypatch.setattr(setup_bot, "ask_yes_no", lambda p, default=False: asked.append(p) or False)
    setup_bot.youtube_step("vpy", {"YOUTUBE_TOKEN_FILE": "", "YOUTUBE_CLIENT_SECRET_FILE": " "})
    assert not any("already exists" in m for m in said)
    assert any("client_secret.json isn't here yet" in m for m in said)
    assert asked == []


def test_platform_switch_without_rewrite_asks_for_the_token(monkeypatch, tmp_path):
    """Regression (F-028): switching a kept conf.env to Discord set
    CHAT_PLATFORM but never asked for DISCORD_BOT_TOKEN, so the bot couldn't
    start even though setup reported success."""
    conf = tmp_path / "conf.env"
    conf.write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)
    monkeypatch.setattr(setup_bot, "CONF", conf)
    monkeypatch.setattr(setup_bot.sys, "argv", ["setup_bot.py"])
    for name in ("preflight", "youtube_step", "exceptions_step", "service_step"):
        monkeypatch.setattr(setup_bot, name, lambda *a, **k: None)
    monkeypatch.setattr(setup_bot, "say", lambda m="": None)
    monkeypatch.setattr(setup_bot, "make_venv", lambda *a: "vpy")
    verified = []
    monkeypatch.setattr(setup_bot, "verify_token",
                        lambda vpy, platform, token: verified.append((platform, token)) or True)
    # platform: 2) Discord; rewrite conf.env: no; set CHAT_PLATFORM: yes;
    # then the missing token ("" first, to hit the "required" re-ask).
    answers = iter(["2", "n", "y", "", "dtok"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    setup_bot.main()
    assert verified == [("discord", "dtok")]
    got = setup_bot.read_conf(conf)
    assert got["CHAT_PLATFORM"] == "discord" and got["DISCORD_BOT_TOKEN"] == "dtok"
    assert got["TELEGRAM_BOT_TOKEN"] == "t"


def test_exceptions_step_creates_the_configured_file(monkeypatch, tmp_path):
    """Regression (F-045): step 6 always created exceptions.txt, even when
    EXCEPTIONS_FILE named another file — the one the bot actually reads."""
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)
    monkeypatch.setattr(setup_bot, "say", lambda m="": None)
    monkeypatch.setattr(setup_bot, "ask_yes_no", lambda p, default=False: True)
    setup_bot.exceptions_step({"EXCEPTIONS_FILE": "my_rules.txt"})
    assert (tmp_path / "my_rules.txt").exists()
    assert not (tmp_path / "exceptions.txt").exists()
    setup_bot.exceptions_step({"EXCEPTIONS_FILE": " "})  # blank -> the default
    assert (tmp_path / "exceptions.txt").exists()


def test_conf_env_example_leaves_the_platform_to_the_token_you_fill_in(monkeypatch, tmp_path):
    """Regression (F-024): the example's active placeholder Telegram token made
    a Discord-only fill-in (CHAT_PLATFORM left out, as it says) start on
    Telegram."""
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)  # judge conf.env alone
    values = setup_bot.read_conf(setup_bot.SETUP_DIR / "conf.env.example")
    assert "TELEGRAM_BOT_TOKEN" not in values
    values["DISCORD_BOT_TOKEN"] = "d"
    assert setup_bot.configured_platform(values) == "discord"


def test_token_check_failure_does_not_print_the_token(monkeypatch):
    """Regression (F-049): when Telegram couldn't be reached, step 5 printed
    the requests error, whose text carries the URL — bot token included."""
    token = "123456789:AAFakeTokenForTestsOnly_abcdefghij"
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # refused at once, offline
    said = []
    monkeypatch.setattr(setup_bot, "say", said.append)
    assert setup_bot.verify_token(sys.executable, "telegram", token) is True
    assert any("Couldn't reach telegram" in m for m in said), said
    assert not any(token in m for m in said), said


def test_platform_switch_without_rewrite_keeps_a_backup(monkeypatch, tmp_path):
    """Regression (F-050): flipping CHAT_PLATFORM in a kept conf.env edited it
    in place with no conf.env.bak, though setup promises one for any change."""
    conf = tmp_path / "conf.env"
    original = "TELEGRAM_BOT_TOKEN=t\nDISCORD_BOT_TOKEN=d\n"
    conf.write_text(original, encoding="utf-8")
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)
    monkeypatch.setattr(setup_bot, "CONF", conf)
    monkeypatch.setattr(setup_bot.sys, "argv", ["setup_bot.py"])
    for name in ("preflight", "youtube_step", "exceptions_step", "service_step"):
        monkeypatch.setattr(setup_bot, name, lambda *a, **k: None)
    monkeypatch.setattr(setup_bot, "say", lambda m="": None)
    monkeypatch.setattr(setup_bot, "make_venv", lambda *a: "vpy")
    monkeypatch.setattr(setup_bot, "verify_token", lambda *a: True)
    answers = iter(["2", "n", "y"])  # Discord; keep conf.env; switch CHAT_PLATFORM
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    setup_bot.main()
    assert setup_bot.read_conf(conf)["CHAT_PLATFORM"] == "discord"
    assert (tmp_path / "conf.env.bak").read_text(encoding="utf-8") == original


def _drive_main(monkeypatch, tmp_path, conf_text, inputs, **stubs):
    """Run setup_bot.main() on a kept conf.env with scripted answers. Returns
    what it printed and the (platform, token) pairs it verified."""
    conf = tmp_path / "conf.env"
    conf.write_text(conf_text, encoding="utf-8")
    monkeypatch.setattr(setup_bot, "HERE", tmp_path)
    monkeypatch.setattr(setup_bot, "CONF", conf)
    monkeypatch.setattr(setup_bot.sys, "argv", ["setup_bot.py"])
    for name in ("preflight", "youtube_step", "exceptions_step", "service_step"):
        monkeypatch.setattr(setup_bot, name, stubs.get(name, lambda *a, **k: None))
    said, verified = [], []
    monkeypatch.setattr(setup_bot, "say", lambda m="": said.append(m))
    monkeypatch.setattr(setup_bot, "make_venv", lambda *a: "vpy")
    monkeypatch.setattr(setup_bot, "verify_token",
                        lambda vpy, platform, token: verified.append((platform, token)) or True)
    answers = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    setup_bot.main()
    return said, verified


def test_kept_conf_without_a_token_asks_for_it(monkeypatch, tmp_path):
    """Regression (F-058): keeping a conf.env copied from the example (no
    token) on the same platform printed "Setup complete", and the bot then
    refused to start for lack of a token."""
    original = (setup_bot.SETUP_DIR / "conf.env.example").read_text(encoding="utf-8")
    said, verified = _drive_main(
        monkeypatch, tmp_path, original,
        ["1", "n", "", "ttok"],  # Telegram; keep conf.env; token ("" re-asks)
    )
    assert verified == [("telegram", "ttok")]
    assert setup_bot.read_conf(tmp_path / "conf.env")["TELEGRAM_BOT_TOKEN"] == "ttok"
    assert (tmp_path / "conf.env.bak").read_text(encoding="utf-8") == original
    assert not any("No TELEGRAM_BOT_TOKEN" in m for m in said)


def test_failed_service_install_is_not_reported_as_complete(monkeypatch, tmp_path):
    """Regression (F-057): step 7 ignored the installer's exit code, so setup
    printed "Setup complete" right after "did NOT come up cleanly"."""
    import subprocess as sp

    real_ask_yes_no = setup_bot.ask_yes_no
    monkeypatch.setattr(setup_bot.sys, "platform", "linux")
    monkeypatch.setattr(setup_bot.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup_bot, "ask_yes_no", lambda p, default=False: True)
    monkeypatch.setattr(setup_bot, "say", lambda m="": None)
    monkeypatch.setattr(setup_bot, "run", lambda cmd, **k: sp.CompletedProcess(cmd, 1))
    assert setup_bot.service_step() is False

    monkeypatch.setattr(setup_bot, "ask_yes_no", real_ask_yes_no)
    said, _ = _drive_main(monkeypatch, tmp_path, "TELEGRAM_BOT_TOKEN=t\n", ["1", "n"],
                          service_step=lambda: False)
    assert not any("Setup complete" in m for m in said)
    assert any("service install failed" in m for m in said)


def test_service_step_without_sudo_does_not_crash(monkeypatch):
    """Regression (F-057): with systemd but no sudo (e.g. Debian installed
    with a root password), step 7 died with FileNotFoundError."""
    monkeypatch.setattr(setup_bot.sys, "platform", "linux")
    monkeypatch.setattr(setup_bot.shutil, "which",
                        lambda name: None if name == "sudo" else f"/usr/bin/{name}")
    ran, said = [], []
    monkeypatch.setattr(setup_bot, "run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(setup_bot, "say", lambda m="": said.append(m))
    assert setup_bot.service_step() is None
    assert ran == [] and any("sudo isn't installed" in m for m in said)


def test_uninstaller_parses_arguments_before_asking_for_root():
    """Regression (F-060): the uninstaller re-ran itself under sudo before
    reading its arguments, so --help (or a typo) asked for a root password."""
    text = (setup_bot.SETUP_DIR / "deploy" / "uninstall-service.sh").read_text(encoding="utf-8")
    assert text.index('for arg in "$@"') < text.index("exec sudo")


def test_deploy_scripts_are_run_through_bash():
    """Regression (F-051): the docs said `sudo ./manual-setup/deploy/...`, and
    the scripts re-ran themselves as "$0" under sudo. Both need an executable
    bit that a checkout committed from Windows doesn't have."""
    root = setup_bot.HERE
    docs = [root / "README.md", root / "manual-setup" / "deploy" / "README.md",
            root / "manual-setup" / "setup_bot.py"]
    scripts = [setup_bot.SETUP_DIR / "deploy" / n
               for n in ("install-service.sh", "uninstall-service.sh")]
    for path in docs + scripts:
        assert "sudo ./manual-setup/deploy/" not in path.read_text(encoding="utf-8"), path
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        assert '-- bash "$0" "$@"' in text and '-- "$0"' not in text, path


def test_secret_defaults_are_masked_in_prompts(monkeypatch):
    """Regression (F-064): conf.env's listing masked the token and API key,
    then the rewrite prompts printed both in full as the bracketed default."""
    existing = {"TELEGRAM_BOT_TOKEN": "123456789:AAsecretTokenValue_xyz",
                "YOUTUBE_API_KEY": "AIzaFakeKeyForTests1234"}
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    monkeypatch.setattr(setup_bot, "say", lambda m="": None)
    answers = setup_bot.collect_settings("telegram", existing)
    for secret in existing.values():
        assert not any(secret in p for p in prompts), prompts
    assert any("[…_xyz]" in p for p in prompts) and any("[…1234]" in p for p in prompts)
    # A path setting whose name says TOKEN is not a secret.
    assert any("[youtube_token.json]" in p for p in prompts), prompts
    # Enter still keeps the real values.
    assert answers["TELEGRAM_BOT_TOKEN"] == existing["TELEGRAM_BOT_TOKEN"]
    assert answers["YOUTUBE_API_KEY"] == existing["YOUTUBE_API_KEY"]


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_conf_env_and_its_backup_are_private(monkeypatch, tmp_path):
    """Regression (F-065): conf.env (bot token) was written with the umask's
    0644, readable by every local user."""
    conf = tmp_path / "conf.env"
    monkeypatch.setattr(setup_bot, "CONF", conf)
    monkeypatch.setattr(setup_bot, "say", lambda m="": None)
    old_umask = os.umask(0o022)
    try:
        setup_bot.write_conf("telegram", {"TELEGRAM_BOT_TOKEN": "t"})
        assert conf.stat().st_mode & 0o777 == 0o600
        conf.chmod(0o644)  # e.g. copied from conf.env.example by hand
        setup_bot.set_conf_key(conf, "CHAT_PLATFORM", "telegram")
        assert conf.stat().st_mode & 0o777 == 0o600
        conf.chmod(0o644)
        setup_bot.backup_conf()
        assert (tmp_path / "conf.env.bak").stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)
