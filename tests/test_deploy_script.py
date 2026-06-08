from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_vps.sh"


def test_deploy_script_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_deploy_script_dry_run_does_not_expose_password() -> None:
    secret = "super-secret-value"
    env = {
        **os.environ,
        "DRY_RUN": "1",
        "SSHPASS": secret,
        "VPS_HOST": "example.invalid",
    }
    result = subprocess.run(
        [str(DEPLOY_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "[dry-run]" in output
    assert "Dry run complete" in output
    assert secret not in output


def test_deploy_script_keeps_sudo_password_out_of_remote_commands() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "echo '$SSHPASS' | sudo" not in script
    assert 'echo "$SSHPASS" | sudo' not in script
    assert "printf '%s\\n' \"$SSHPASS\"" in script
