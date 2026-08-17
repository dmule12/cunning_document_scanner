"""Credential loading.

Small surface, but it is the first thing anyone touches, and a confusing
failure here stops the tool being used at all.
"""

from __future__ import annotations

import pytest

from cin7_reorder.config import (
    DEFAULT_BASE_URL,
    ConfigError,
    Credentials,
    load_env_file,
)


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for name in ("CIN7_ACCOUNT_ID", "CIN7_APP_KEY", "CIN7_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_reads_plain_values(tmp_path):
    path = write_env(tmp_path, "CIN7_ACCOUNT_ID=abc\nCIN7_APP_KEY=def\n")
    assert load_env_file(path) == {"CIN7_ACCOUNT_ID": "abc", "CIN7_APP_KEY": "def"}


def test_strips_surrounding_quotes(tmp_path):
    """People paste credentials with quotes; the quotes are not the value."""
    path = write_env(tmp_path, "CIN7_ACCOUNT_ID='abc'\nCIN7_APP_KEY=\"def\"\n")
    values = load_env_file(path)
    assert values["CIN7_ACCOUNT_ID"] == "abc"
    assert values["CIN7_APP_KEY"] == "def"


def test_tolerates_an_export_prefix(tmp_path):
    """Copied straight from a shell snippet, which is exactly what happens."""
    path = write_env(tmp_path, "export CIN7_ACCOUNT_ID=abc\n")
    assert load_env_file(path)["CIN7_ACCOUNT_ID"] == "abc"


def test_ignores_comments_and_blank_lines(tmp_path):
    path = write_env(tmp_path, "# a comment\n\nCIN7_ACCOUNT_ID=abc\n\n")
    assert load_env_file(path) == {"CIN7_ACCOUNT_ID": "abc"}


def test_strips_trailing_whitespace(tmp_path):
    path = write_env(tmp_path, "CIN7_ACCOUNT_ID=abc   \n")
    assert load_env_file(path)["CIN7_ACCOUNT_ID"] == "abc"


def test_missing_file_is_empty_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_lines_without_an_equals_are_skipped(tmp_path):
    path = write_env(tmp_path, "this is not a setting\nCIN7_APP_KEY=def\n")
    assert load_env_file(path) == {"CIN7_APP_KEY": "def"}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_loads_from_env_file(tmp_path):
    path = write_env(tmp_path, "CIN7_ACCOUNT_ID=acct\nCIN7_APP_KEY=key\n")
    creds = Credentials.from_env(path)
    assert creds.account_id == "acct"
    assert creds.app_key == "key"
    assert creds.base_url == DEFAULT_BASE_URL


def test_loads_from_environment_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("CIN7_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CIN7_APP_KEY", "key")
    creds = Credentials.from_env(tmp_path / "absent.env")
    assert creds.account_id == "acct"


def test_environment_wins_over_the_file(monkeypatch, tmp_path):
    """CI secrets must never be shadowed by a stray .env in a checkout."""
    path = write_env(tmp_path, "CIN7_ACCOUNT_ID=from-file\nCIN7_APP_KEY=from-file\n")
    monkeypatch.setenv("CIN7_ACCOUNT_ID", "from-env")

    creds = Credentials.from_env(path)
    assert creds.account_id == "from-env"
    # Falls back to the file for anything the environment does not set.
    assert creds.app_key == "from-file"


def test_base_url_override(tmp_path):
    path = write_env(
        tmp_path,
        "CIN7_ACCOUNT_ID=a\nCIN7_APP_KEY=b\nCIN7_BASE_URL=https://example.test/api/\n",
    )
    assert Credentials.from_env(path).base_url == "https://example.test/api"


def test_missing_credentials_explain_both_routes(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        Credentials.from_env(tmp_path / "absent.env")

    message = str(excinfo.value)
    assert "CIN7_ACCOUNT_ID" in message
    assert ".env" in message
    assert "export" in message
    # The error should say where to get the credentials, not just that they
    # are absent.
    assert "ExternalAPI" in message


def test_blank_values_count_as_missing(tmp_path):
    """A .env copied from the example but not filled in."""
    path = write_env(tmp_path, "CIN7_ACCOUNT_ID=\nCIN7_APP_KEY=\n")
    with pytest.raises(ConfigError):
        Credentials.from_env(path)
