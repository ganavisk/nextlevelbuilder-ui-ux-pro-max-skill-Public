import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_defaults_are_development_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert not settings.is_production
    assert settings.llm_model_primary == "claude-sonnet-5"
    assert settings.llm_model_fast == "claude-haiku-4-5"


def test_cors_origins_parse_as_list() -> None:
    settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setenv("MAX_CALL_DURATION_SECONDS", "600")
    settings = Settings(_env_file=None)
    assert settings.is_production
    assert settings.max_call_duration_seconds == 600


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize(
    "secret", ["dev-only-secret", "change-me-generate-with-openssl-rand-hex-32", "short"]
)
def test_deployed_environments_reject_weak_or_default_secret_key(env: str, secret: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(_env_file=None, app_env=env, secret_key=secret)


def test_deployed_environments_accept_a_strong_secret_key() -> None:
    settings = Settings(_env_file=None, app_env="production", secret_key="a" * 64)
    assert settings.is_production


def test_development_and_test_keep_the_convenient_default() -> None:
    assert Settings(_env_file=None, app_env="development").secret_key == "dev-only-secret"
    assert Settings(_env_file=None, app_env="test").secret_key == "dev-only-secret"


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_platform_style_database_urls_get_the_async_driver(scheme: str) -> None:
    settings = Settings(_env_file=None, database_url=f"{scheme}://u:p@db.internal:5432/app")
    assert settings.database_url == "postgresql+asyncpg://u:p@db.internal:5432/app"


def test_an_explicit_driver_is_left_alone() -> None:
    url = "postgresql+asyncpg://u:p@db.internal:5432/app"
    assert Settings(_env_file=None, database_url=url).database_url == url


def test_env_files_resolve_to_the_api_folder_and_the_repo_root() -> None:
    """`make dev` runs inside apps/api, so a cwd-relative ".env" used to ignore
    the repo-root file the docs tell people to fill in."""
    from app.core.config import ENV_FILES

    api_env, root_env = ENV_FILES
    assert (api_env.parent / "pyproject.toml").exists()
    assert (root_env.parent / "docker-compose.yml").exists()
    assert ENV_FILES.index(root_env) > ENV_FILES.index(api_env)  # the repo-root file wins
