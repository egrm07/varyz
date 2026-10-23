"""CI: notarize verified signed artifacts without exposing Apple credentials."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def run(args: list[str], *, private: bool = False) -> str:
    # Apple's ticket service can lag an Accepted response or drop the connection.
    # Retry only stapler exit 68 (staple or validate), with a bounded delay; never
    # bypass validation or assessment.
    for attempt in range(4):
        result = subprocess.run(args, capture_output=True, text=True, timeout=1500)
        if result.returncode != 68 or args[:2] != ["xcrun", "stapler"]:
            break
        if attempt < 3:
            time.sleep((10, 30, 60)[attempt])
    if result.returncode:
        if private:
            raise RuntimeError(
                "Apple credential validation failed; check repository secrets"
            )
        label = " ".join(args[:3]) if args[0] == "xcrun" else Path(args[0]).name
        detail = ""
        if args[:2] == ["xcrun", "stapler"]:
            detail = ": " + (result.stdout + result.stderr)[-2000:]
        raise RuntimeError(f"{label} failed with exit code {result.returncode}{detail}")
    return result.stdout


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    folder = Path(os.environ["SIGNED_ARTIFACT"])
    output = Path(os.environ["NOTARIZED_OUTPUT"])
    expected = os.environ["EXPECTED_SOURCE_COMMIT"]
    provenance = json.loads((folder / "native-release-provenance.json").read_text())
    if provenance["revision"] != expected or provenance["working_tree_dirty"]:
        raise RuntimeError("Signed candidate has unexpected source provenance")
    packages = list(folder.glob("*.pkg"))
    if len(packages) != 1:
        raise RuntimeError("Expected exactly one architecture-specific signed package")
    package = packages[0]
    signing = json.loads(package.with_suffix(".signing.json").read_text())
    before = digest(package)
    if (
        before != signing["sha256"]
        or before != provenance["artifacts"]["signed-candidate/" + package.name]
    ):
        raise RuntimeError("Signed package hash mismatch")
    if signing["signed_runtime_binaries"] < 1 or signing["signed_wheel_libraries"] < 1:
        raise RuntimeError("Missing nested code signing evidence")
    output.mkdir(parents=True, exist_ok=True)
    final_package = output / package.name
    shutil.copyfile(package, final_package)
    keychain_folder = Path(tempfile.mkdtemp(prefix="varyz-notary-", dir=os.environ["RUNNER_TEMP"]))
    keychain = keychain_folder / "notary.keychain-db"
    password = secrets.token_hex(32)
    print("::add-mask::" + password, flush=True)
    try:
        run(["security", "create-keychain", "-p", password, str(keychain)], private=True)
        run(["security", "unlock-keychain", "-p", password, str(keychain)], private=True)
        run(
            [
                "xcrun",
                "notarytool",
                "store-credentials",
                "varyz-release",
                "--keychain",
                str(keychain),
                "--apple-id",
                os.environ["APPLE_ID"],
                "--team-id",
                os.environ["APPLE_TEAM_ID"],
                "--password",
                os.environ["APPLE_APP_SPECIFIC_PASSWORD"],
            ],
            private=True,
        )
        submission = json.loads(
            run(
                [
                    "xcrun",
                    "notarytool",
                    "submit",
                    str(final_package),
                    "--keychain-profile",
                    "varyz-release",
                    "--keychain",
                    str(keychain),
                    "--wait",
                    "--output-format",
                    "json",
                ]
            )
        )
        receipt = {
            "source_commit": expected,
            "signing_workflow_run": os.environ["SIGNING_RUN_ID"],
            "notarization_id": submission.get("id"),
            "notarization_status": submission.get("status"),
            "signed_sha256_before_stapling": before,
        }
        (output / "notarization.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if submission.get("status") != "Accepted":
            if submission.get("id"):
                run(
                    [
                        "xcrun",
                        "notarytool",
                        "log",
                        submission["id"],
                        "--keychain-profile",
                        "varyz-release",
                        "--keychain",
                        str(keychain),
                        str(output / "apple-notarization-log.json"),
                    ]
                )
            raise RuntimeError(
                "Apple did not accept this installer; inspect retained notarization receipt"
            )
        run(["xcrun", "stapler", "staple", str(final_package)])
        run(["xcrun", "stapler", "validate", str(final_package)])
        run(["pkgutil", "--check-signature", str(final_package)])
        run(["spctl", "--assess", "--type", "install", "--verbose=2", str(final_package)])
        after = digest(final_package)
        signing.update(
            sha256=after,
            notarized=True,
            stapled=True,
            gatekeeper_assessment="passed",
            distribution_ready=False,
            native_gui_acceptance="pending",
        )
        final_package.with_suffix(".signing.json").write_text(json.dumps(signing, indent=2) + "\n")
        receipt.update(
            sha256=after,
            stapled=True,
            gatekeeper_assessment="passed",
            native_gui_acceptance="pending",
        )
        (output / "notarization.json").write_text(json.dumps(receipt, indent=2) + "\n")
        provenance.update(
            signed_artifacts_before_notarization=provenance["artifacts"],
            artifacts={final_package.name: after},
            notarization=receipt,
            notarization_workflow_revision=os.environ["GITHUB_SHA"],
        )
        (output / "native-release-provenance.json").write_text(
            json.dumps(provenance, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    "artifact": final_package.name,
                    "sha256": after,
                    "notarized": True,
                    "stapled": True,
                    "gatekeeper_assessment": "passed",
                    "native_gui_acceptance": "pending",
                }
            )
        )
    finally:
        subprocess.run(["security", "delete-keychain", str(keychain)], capture_output=True)
        shutil.rmtree(keychain_folder, ignore_errors=True)


if __name__ == "__main__":
    main()
