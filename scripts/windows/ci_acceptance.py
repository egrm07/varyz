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
sentinel = config.parent / "synthetic-retained.txt"
sentinel.write_text("preserve acceptance data")
documents = r / "synthetic-documents"
documents.mkdir()
(documents / "release-note.md").write_text(
    "# Fictional release\n\nThe synthetic team chose a blue checklist for its demo.\n"
)
# Setup never offers the Documents folder itself, only project checkouts in it.
project = documents / "projects" / "synthetic-project"
project.mkdir(parents=True)
(project / "README.md").write_text("# Fictional project\n\nA blue checklist for the demo.\n")
synthetic_git = ["git", "-C", str(project), "-c", "user.name=Synthetic",
                 "-c", "user.email=synthetic@example.invalid", "-c", "commit.gpgsign=false"]
for command in (["init", "-q"], ["add", "README.md"], ["commit", "-q", "-m", "Fictional demo"]):
    subprocess.run(synthetic_git + command, check=True, capture_output=True, timeout=60)
env = {
    **os.environ,
    "UV_TOOL_DIR": str(r / "tools"),
    "UV_TOOL_BIN_DIR": str(r / "bin"),
    "UV_PYTHON_INSTALL_DIR": str(r / "python"),
    "VARYZ_CONFIG": str(config),
    "PYTHONHOME": str(r / "nonexistent-foreign-python"),
    "PYTHONPATH": str(r / "foreign-modules"),
    "XDG_DOCUMENTS_DIR": str(documents),
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
assert not config.exists() and sentinel.read_text() == "preserve acceptance data"
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

# Resolve and actually launch the Start Menu shortcut. Only this disposable VM's
# fictional project is approved below; no real account, agent or Cloud is used.
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
results.append({"action": "actual-start-menu-shortcut", "authenticated_setup_page": 200})


def api(path, body=None):
    parsed = urllib.parse.urlsplit(url)
    token = urllib.parse.parse_qs(parsed.query)["t"][0]
    target = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    request = urllib.request.Request(
        target,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Varyz-Setup": token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def onboard(label, sources):
    assert api("/api/run", {"agents": [], "sources": sources, "analytics": False})["started"]
    deadline = time.monotonic() + 25 * 60
    last = None
    while time.monotonic() < deadline:
        snapshot = api("/api/state")
        # Token-free synthetic progress survives a failed or timed-out run.
        (r / "onboarding-state.json").write_text(json.dumps(snapshot, indent=2))
        progress = [(s["name"], s["state"]) for s in snapshot["steps"]]
        if progress != last:
            print(json.dumps({"onboarding": label, "steps": progress}), flush=True)
            last = progress
        if snapshot["finished"]:
            break
        time.sleep(2)
    else:
        raise AssertionError("Synthetic onboarding did not finish in 25 minutes")
    assert snapshot["result"]["verdict"] == "GO", snapshot["result"]
    assert not snapshot["result"]["problems"]
    assert snapshot["picked"]["agents"] == []
    assert api("/api/close", {})["closed"]
    results.append({"action": label, "verdict": "GO", "steps": progress,
                    "synthetic_sources_only": True, "analytics": False})


plan = api("/api/plan")
assert plan["exists"] is False, "First onboarding must create a fresh configuration"
assert not any(s["id"] == "documents" for s in plan["sources"]), "Documents folder offered"
assert any(s["id"] == "git" for s in plan["sources"])
onboard("fresh-onboarding", ["git"])
assert config.exists() and sentinel.read_text() == "preserve acceptance data"
python = r / "tools/varyzai/Scripts/python.exe"
assert python.is_file()
statement = "Keep synthetic notes concise"
run([str(python), "-I", "-m", "varyz", "rules", "add", statement,
     "--project", "synthetic-windows-acceptance", "--config", str(config)])


def stop_services():
    run([str(python), "-I", "-m", "varyz", "autosync", "uninstall", "--config", str(config)])
    run([str(python), "-I", "-m", "varyz", "daemon", "stop", "--config", str(config)])
    # Autosync observes the disabled marker every five seconds. Native preflight
    # checks that the real installed runtime is no longer in use; never kill it.
    native = Path(json.loads((app / "native-state.json").read_text())["payload"]) / "native.py"
    for _ in range(30):
        checked = subprocess.run([str(python), "-I", str(native), "uninstall-check",
                                  "--app", str(app), "--result", str(r / "preflight.json")],
                                 env=env, capture_output=True, timeout=30)
        if checked.returncode == 0:
            return
        time.sleep(2)
    raise AssertionError("Synthetic background services did not stop cleanly")


stop_services()
configuration_hash = hashlib.sha256(config.read_bytes()).hexdigest()
run([str(exe), "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
     "/DIR=" + str(app), "/LOG=" + str(r / "configured-repeat-install.log")])
assert hashlib.sha256(config.read_bytes()).hexdigest() == configuration_hash
results.append({"action": "configured-repeat-install", "config_preserved": True})
# Start the authenticated daemon again and verify actual retained rules rather
# than only an unrelated sentinel file.
run([str(python), "-I", "-m", "varyz", "daemon", "start", "--config", str(config)])
rules = subprocess.check_output([str(python), "-I", "-m", "varyz", "rules", "list",
                                 "--config", str(config)], env=env, timeout=120).decode()
assert all(word in rules for word in statement.split())
stop_services()
run([str(app / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
     "/LOG=" + str(r / "configured-uninstall.log")])
assert hashlib.sha256(config.read_bytes()).hexdigest() == configuration_hash
assert sentinel.read_text() == "preserve acceptance data"
run([str(exe), "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
     "/DIR=" + str(app), "/LOG=" + str(r / "configured-reinstall.log")])
assert hashlib.sha256(config.read_bytes()).hexdigest() == configuration_hash
run([str(python), "-I", "-m", "varyz", "daemon", "start", "--config", str(config)])
rules = subprocess.check_output([str(python), "-I", "-m", "varyz", "rules", "list",
                                 "--config", str(config)], env=env, timeout=120).decode()
assert all(word in rules for word in statement.split())
results.append({"action": "configured-uninstall-reinstall", "config_preserved": True,
                "synthetic_rule_preserved": True, "sentinel_preserved": True})
# Relaunch the actual installed shortcut and finish a returning-user setup run.
subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", launcher],
               env=env, check=True, timeout=30)
for _ in range(120):
    log = app / "setup.log"
    content = log.read_text(errors="replace") if log.exists() else ""
    match = re.search(r"http://127\.0\.0\.1:\d+/\?t=[A-Za-z0-9_-]+", content)
    if match and match.group() != url:
        url = match.group()
        break
    time.sleep(0.5)
else:
    raise AssertionError("Returning-user shortcut did not start setup")
assert api("/api/plan")["exists"] is True
onboard("returning-user-onboarding", [])
stop_services()
receipt = {
    "artifact_sha256": expected,
    "artifact_source_commit": source_commit,
    "launch_source_commit": source_commit,
    "machine": "fresh GitHub hosted Windows runner",
    "checks": results,
    "visual_wizard": "not inspected",
    "cloud_and_agent_accounts": "not connected; synthetic local onboarding only",
    "installer_signature": os.environ.get(
        "INSTALLER_SIGNATURE", "unsigned candidate; no public download switch"
    ),
    "foreign_python_environment": "deliberately injected throughout",
}
(r / "verified.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt))
