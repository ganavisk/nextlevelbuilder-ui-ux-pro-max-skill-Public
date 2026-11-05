"""The backup and restore scripts refuse unsafe input before touching
anything. Their real behaviour (backups, restores) is proven by the drills in
docs/runbooks/backup-and-restore.md, not here: these tests only pin the
guard rails, which must not erode quietly."""

import os
import subprocess
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[3] / "infra"
SCRIPTS = sorted(
    [
        *(INFRA / "pgbackrest").glob("*.sh"),
        *(INFRA / "postgres" / "image").glob("*.sh"),
        INFRA / "postgres" / "pitr-restore.sh",
        INFRA / "scripts" / "smoke-postgres-image.sh",
    ]
)
REQUIRED = {
    "PGBACKREST_REPO1_S3_BUCKET": "b",
    "PGBACKREST_REPO1_S3_ENDPOINT": "s3.example.com",
    "PGBACKREST_REPO1_S3_REGION": "eu-central-1",
    "PGBACKREST_REPO1_S3_KEY": "k",
    "PGBACKREST_REPO1_S3_KEY_SECRET": "s",
    "PGBACKREST_REPO1_CIPHER_PASS": "x" * 48,
}


def run(script: Path, *args: str, env: dict[str, str] | None = None):
    base = {k: v for k, v in os.environ.items() if not k.startswith("PGBACKREST_")}
    return subprocess.run(
        ["bash", str(script), *args],
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_and_are_executable(script: Path) -> None:
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    assert os.access(script, os.X_OK), f"{script.name} is not executable"


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_setup_refuses_without_each_required_setting(missing: str) -> None:
    env = {k: v for k, v in REQUIRED.items() if k != missing}
    result = run(INFRA / "pgbackrest" / "setup.sh", env=env)
    assert result.returncode == 2
    assert f"missing {missing}" in result.stderr


def test_setup_refuses_a_short_cipher_passphrase() -> None:
    result = run(
        INFRA / "pgbackrest" / "setup.sh",
        env={**REQUIRED, "PGBACKREST_REPO1_CIPHER_PASS": "short-but-not-32-chars"},
    )
    assert result.returncode == 2
    assert "shorter than 32" in result.stderr


def test_backup_refuses_an_unknown_type() -> None:
    result = run(INFRA / "pgbackrest" / "backup.sh", "weekly")
    assert result.returncode == 2
    assert "full, diff or incr" in result.stderr


@pytest.mark.parametrize(
    "script", [INFRA / "pgbackrest" / "restore.sh", INFRA / "postgres" / "pitr-restore.sh"]
)
def test_restores_never_overwrite_an_existing_directory(script: Path, tmp_path: Path) -> None:
    existing = tmp_path / "data"
    existing.mkdir()
    (existing / "PG_VERSION").write_text("16\n")
    if script.name == "restore.sh":
        args = ["latest", str(existing)]
    else:
        base = tmp_path / "base"
        base.mkdir()
        (base / "base.tar.gz").write_bytes(b"")
        args = [str(base), str(tmp_path), "latest", str(existing)]
    result = run(script, *args)
    assert result.returncode == 2
    assert "refusing to overwrite" in result.stderr
    assert (existing / "PG_VERSION").read_text() == "16\n"


@pytest.mark.parametrize(
    "script", [INFRA / "pgbackrest" / "restore.sh", INFRA / "postgres" / "pitr-restore.sh"]
)
def test_restores_print_usage_without_arguments(script: Path) -> None:
    result = run(script)
    assert result.returncode == 2
    assert "new-data-dir" in result.stderr
