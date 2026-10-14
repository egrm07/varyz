"""Regenerate local installer copies; never upload or execute an installer."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sync() -> None:
    scripts = ROOT / "scripts"
    shell = (scripts / "install.sh").read_bytes()
    powershell = (scripts / "install.ps1").read_bytes()
    (scripts / "Install Varyz.command").write_bytes(shell)
    launcher = scripts / "Install Varyz.cmd"
    # Preserve the existing double-click wrapper; only its embedded script is
    # generated. Keep the Skip count consistent with the retained header.
    header = launcher.read_bytes().replace(b"\r\n", b"\n").split(b"\n")[:8]
    body = powershell.replace(b"\r\n", b"\n")
    launcher.write_bytes((b"\n".join(header) + b"\n" + body).replace(b"\n", b"\r\n"))
    website = ROOT / "website"
    if website.is_dir():
        for name, content in (("install.sh", shell), ("install.ps1", powershell)):
            (website / name).write_bytes(content)


if __name__ == "__main__":
    sync()
