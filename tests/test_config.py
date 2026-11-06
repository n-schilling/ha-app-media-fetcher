"""Option loading."""

import json

import pytest

from media_fetcher.config import ConfigError, load


def write(tmp_path, **options):
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options))
    return path


def test_defaults_and_a_random_token(tmp_path):
    cfg = load(write(tmp_path))
    assert (cfg.max_jobs, cfg.keep_minutes, cfg.allow_any_site, cfg.log_level) == (2, 60, False, "INFO")
    assert not cfg.token_fixed and len(cfg.token) >= 32
    assert load(write(tmp_path)).token != cfg.token


def test_a_set_token_is_kept(tmp_path):
    cfg = load(write(tmp_path, token=" my-token ", max_jobs=4, keep_minutes=5, allow_any_site=True, log_level="debug"))
    assert (cfg.token, cfg.token_fixed) == ("my-token", True)
    assert (cfg.max_jobs, cfg.keep_minutes, cfg.allow_any_site, cfg.log_level) == (4, 5, True, "DEBUG")


@pytest.mark.parametrize("bad", [{"max_jobs": 0}, {"max_jobs": "2"}, {"max_jobs": True}, {"keep_minutes": 0},
                                 {"keep_minutes": 100000}, {"log_level": "loud"}])
def test_invalid_values_are_named(tmp_path, bad):
    with pytest.raises(ConfigError, match=next(iter(bad))):
        load(write(tmp_path, **bad))


def test_unreadable_options(tmp_path):
    with pytest.raises(ConfigError, match="not readable"):
        load(tmp_path / "missing.json")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(ConfigError, match="no options"):
        load(tmp_path / "list.json")
