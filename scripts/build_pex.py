from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist/release"
ASSET_NAME = "ssh-archive-deploy-linux-x86_64.pex"


def main() -> int:
    project = read_project_metadata()
    if project["name"] != "ssh-archive-deploy":
        raise BuildError(f"Unexpected project name: {project['name']}")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    pex_path = DIST_DIR / ASSET_NAME
    checksum_path = pex_path.with_suffix(f"{pex_path.suffix}.sha256")
    remove_if_exists(pex_path)
    remove_if_exists(checksum_path)

    with tempfile.TemporaryDirectory(prefix="ssh-archive-deploy-pex-") as tmp:
        tmp_path = Path(tmp)
        requirements = tmp_path / "runtime-requirements.txt"
        wheel_dir = tmp_path / "wheel"

        export_runtime_requirements(requirements)
        wheel = build_wheel(wheel_dir)
        build_pex(wheel, requirements, pex_path)

    pex_path.chmod(0o755)
    write_sha256(pex_path, checksum_path)
    print(f"Built {pex_path.relative_to(REPO_ROOT)}")
    print(f"Wrote {checksum_path.relative_to(REPO_ROOT)}")
    return 0


def read_project_metadata() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    return {
        "name": str(project["name"]),
        "version": str(project["version"]),
    }


def export_runtime_requirements(output: Path) -> None:
    run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-default-groups",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements.txt",
            "--output-file",
            str(output),
        ]
    )


def build_wheel(output_dir: Path) -> Path:
    run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(output_dir),
            "--no-create-gitignore",
        ]
    )
    wheels = sorted(output_dir.glob("ssh_archive_deploy-*.whl"))
    if len(wheels) != 1:
        raise BuildError(f"Expected one project wheel in {output_dir}, found {len(wheels)}")
    return wheels[0]


def build_pex(wheel: Path, requirements: Path, output: Path) -> None:
    run(
        [
            sys.executable,
            "-m",
            "pex",
            "--project",
            str(wheel),
            "--requirement",
            str(requirements),
            "--console-script",
            "ssh-archive-deploy",
            "--interpreter-constraint",
            "CPython==3.12.*",
            "--sh-boot",
            "--output-file",
            str(output),
        ]
    )


def write_sha256(path: Path, output: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    output.write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def remove_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise BuildError(
            "Command failed:\n"
            f"command: {' '.join(command)}\n"
            f"exit code: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


class BuildError(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
