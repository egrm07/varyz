from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compile_backup_is_outside_the_archive() -> None:
    """The backup command refuses to copy ``data`` into one of its children."""
    script = (ROOT / "scripts" / "supervise_compile.ps1").read_text(encoding="utf-8")

    assert "Join-Path $root '.compile-backups'" in script
    assert 'Join-Path $root "data\\backups' not in script
