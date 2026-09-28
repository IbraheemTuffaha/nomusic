"""Build real artifacts so stale output cannot silently survive a source deletion."""

import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _build(project: Path, kind: str) -> Path:
    (project / "dist").mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-c", f"import uv_build; print(uv_build.build_{kind}('dist'))"],
        cwd=project, capture_output=True, text=True,
        env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    return project / "dist" / result.stdout.strip().splitlines()[-1]


def test_rebuild_omits_deleted_sources_and_private_build_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(ROOT / name, project / name)
    package = project / "backend" / "nomusic"
    shutil.copytree(ROOT / "backend" / "nomusic", package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    removed = package / "removed_module.py"
    removed.write_text("OLD_VALUE = True\n")
    with zipfile.ZipFile(_build(project, "wheel")) as wheel:
        assert "nomusic/removed_module.py" in wheel.namelist()

    removed.unlink()
    stale = project / "build" / "lib" / "nomusic" / "removed_module.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("STALE_BUILD = True\n")
    private = project / "mds" / "private.txt"
    private.parent.mkdir()
    private.write_text("LOCAL_ONLY\n")
    (project / ".env").write_text("LOCAL_ONLY=true\n")
    expected = {str(p.relative_to(package.parent)) for p in package.rglob("*.py")}

    def assert_wheel(path):
        with zipfile.ZipFile(path) as wheel:
            names = set(wheel.namelist())
            assert {n for n in names if n.endswith(".py")} == expected
            assert not any(n.startswith(("build/", "mds/")) or n.endswith(".env")
                           for n in names)

    assert_wheel(_build(project, "wheel"))
    unpacked = tmp_path / "sdist"
    with tarfile.open(_build(project, "sdist")) as archive:
        names = archive.getnames()
        assert not any("/mds/" in n or "/build/" in n or n.endswith("/.env")
                       for n in names)
        archive.extractall(unpacked, filter="data")
    assert_wheel(_build(next(unpacked.iterdir()), "wheel"))
