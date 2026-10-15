"""Test the exact reviewed native EXE only on a disposable GitHub Windows runner."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import winreg
from pathlib import Path

assert os.name == "nt" and os.environ.get("GITHUB_ACTIONS") == "true"
assert os.environ.get("RUNNER_OS") == "Windows"
exe = Path(sys.argv[1]).resolve()
expected = os.environ["EXPECTED_EXE_SHA256"]
assert re.fullmatch("[0-9a-f]{64}", expected)
source_commit = os.environ["EXPECTED_SOURCE_COMMIT"]
assert re.fullmatch("[0-9a-f]{40}", source_commit)
assert hashlib.sha256(exe.read_bytes()).hexdigest() == expected
base = Path(os.environ["RUNNER_TEMP"]).resolve()
r = base / "varyz-native-acceptance"
assert not r.exists() and r.is_relative_to(base)
key = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{D151746E-801E-4E69-97C2-42ED91562AC7}_is1"
)
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
        raise RuntimeError("Existing native installation: refuse test")
except FileNotFoundError:
    pass
shortcuts = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Varyz"
assert not shortcuts.exists(), "Existing shortcut group: refuse test"
r.mkdir()
app = r / "app"
config = r / "archive/config.toml"
config.parent.mkdir()
config.write_text('[storage]\ndata_dir="data"\n[resolution]\nport=1248\n')
sentinel = config.parent / "synthetic-retained.txt"
sentinel.write_text("preserve acceptance data")
env = {
    **os.environ,
    "UV_TOOL_DIR": str(r / "tools"),
    "UV_TOOL_BIN_DIR": str(r / "bin"),
    "UV_PYTHON_INSTALL_DIR": str(r / "python"),
    "VARYZ_CONFIG": str(config),
    "PYTHONHOME": str(r / "nonexistent-foreign-python"),
    "PYTHONPATH": str(r / "foreign-modules"),
}
results = []


def run(args):
    process = subprocess.run(args, env=env, capture_output=True, timeout=600)
    assert process.returncode == 0, f"Worker exit {process.returncode}"


for attempt in range(2):
    run(
        [
            str(exe),
            "/SP-",
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=" + str(app),
            "/LOG=" + str(r / f"install-{attempt}.log"),
        ]
    )
    state = json.loads((app / "native-state.json").read_text())
    assert Path(state["config"]).resolve() == config
    assert Path(state["environment"]).resolve() == r / "tools/varyzai"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as reg:
        location, _ = winreg.QueryValueEx(reg, "InstallLocation")
        assert Path(location).resolve() == app
    assert {p.name for p in shortcuts.iterdir()} == {"Varyz Setup.lnk", "Uninstall Varyz.lnk"}
    results.append({"action": "install" if attempt == 0 else "repeat-install", "exit": 0})

# Check removal before starting an interactive browser setup session. Closing an
# unfinished setup deliberately returns failure and can show a native message box;
# do not create that modal dialog in unattended acceptance.
uninstaller = app / "unins000.exe"
assert uninstaller.resolve().is_relative_to(r) and uninstaller.is_file()
run(
    [
        str(uninstaller),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=" + str(r / "uninstall.log"),
    ]
)
assert config.exists() and sentinel.read_text() == "preserve acceptance data"
assert not (r / "tools/varyzai").exists() and not shortcuts.exists()
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
        raise RuntimeError("Test registration was not removed")
except FileNotFoundError:
    pass
results.append(
    {
        "action": "uninstall",
        "exit": 0,
        "data_preserved": True,
        "registration_and_shortcuts_removed": True,
    }
)
run(
    [
        str(exe),
        "/SP-",
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=" + str(app),
        "/LOG=" + str(r / "install-after-removal.log"),
    ]
)
assert sentinel.read_text() == "preserve acceptance data"
results.append({"action": "install-after-removal", "exit": 0, "retained_data_preserved": True})

# Resolve and actually launch the Start Menu shortcut and check its authenticated
# localhost page. Never submit setup, index sources or sync. The disposable hosted
# VM ends this unsubmitted setup session when the job finishes.
reader = (
    "$reader=New-Object -ComObject WScript.Shell; $link=$reader.CreateShortcut('"
    + str(shortcuts / "Varyz Setup.lnk").replace("'", "''")
    + "'); [pscustomobject]@{Target=$link.TargetPath;Args=$link.Arguments} | ConvertTo-Json"
)
shortcut = json.loads(
    subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", reader])
)
assert Path(shortcut["Target"]).resolve().is_relative_to(app)
assert shortcut["Args"].startswith("-I ")
launcher = (
    "Start-Process -FilePath '"
    + str(shortcuts / "Varyz Setup.lnk").replace("'", "''")
    + "' -WindowStyle Hidden"
)
subprocess.run(
    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", launcher],
    env=env,
    check=True,
    timeout=30,
)
url = None
for _ in range(120):
    log = app / "setup.log"
    content = log.read_text(errors="replace") if log.exists() else ""
    match = re.search(r"http://127\.0\.0\.1:\d+/\?t=[A-Za-z0-9_-]+", content)
    if match:
        url = match.group()
        break
    time.sleep(0.5)
assert url, "Shortcut did not start the setup server"
with urllib.request.urlopen(url, timeout=15) as response:
    page = response.read()
    assert response.status == 200 and b"Varyz" in page
results.append(
    {
        "action": "actual-start-menu-shortcut",
        "authenticated_setup_page": 200,
        "setup_and_sync_not_submitted": True,
        "cleanup": "disposable hosted VM teardown",
    }
)
receipt = {
    "artifact_sha256": expected,
    "artifact_source_commit": source_commit,
    "launch_source_commit": source_commit,
    "machine": "fresh GitHub hosted Windows runner",
    "checks": results,
    "visual_wizard": "not inspected",
    "foreign_python_environment": "deliberately injected throughout",
}
(r / "verified.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt))
