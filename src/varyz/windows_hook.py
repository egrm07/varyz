"""Windows GUI entry points: no shell or console is created at launch."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
class HookLauncher {
    static void Record(string state) {
        try { File.WriteAllText(Assembly.GetExecutingAssembly().Location + ".status", state); }
        catch (Exception) {}
    }
    // Stream.CopyTo leaves a FileStream's buffer unflushed until the source
    // closes, so an MCP client that keeps stdin open never got its handshake
    // through. Forward and flush every chunk as it arrives.
    static void Pump(Stream source, Stream target) {
        byte[] buffer = new byte[8192];
        int read;
        while ((read = source.Read(buffer, 0, buffer.Length)) > 0) {
            target.Write(buffer, 0, read);
            target.Flush();
        }
    }
    static string Quote(string value) {
        // Paths cannot contain quotes; double trailing slashes before the closing quote.
        return "\"" + value.TrimEnd('\\') +
            new string('\\', (value.Length - value.TrimEnd('\\').Length) * 2) + "\"";
    }
    [STAThread]
    static int Main() {
        bool watchdog = false;
        try {
            string[] config = File.ReadAllLines(
                Assembly.GetExecutingAssembly().Location + ".launch");
            if (config.Length != 3) return 0;
            watchdog = config[1] == "watchdog";
            if (!File.Exists(config[0]) || !File.Exists(config[2])) {
                if (watchdog) { Record("failed:missing_launch_file"); return 1; }
                return 0;
            }
            if (config[1] != "session-start" && config[1] != "user-prompt" &&
                config[1] != "stop" && config[1] != "mcp" &&
                config[1] != "watchdog") return 0;
            bool mcp = config[1] == "mcp";
            var info = new ProcessStartInfo(config[0],
                (watchdog ? "-m varyz.resolution.availability" :
                 mcp ? "-m varyz mcp" : "-m varyz.hooks " + config[1]) +
                " --config " + Quote(config[2]) + (watchdog ? " --watch" : ""));
            info.UseShellExecute = false;
            info.WorkingDirectory = Path.GetDirectoryName(config[2]);
            info.CreateNoWindow = true;
            info.RedirectStandardInput = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            if (watchdog) Record("starting");
            using (var child = Process.Start(info)) {
                if (watchdog) {
                    Record("child_started");
                    child.StandardInput.Close();
                    var drainOutput = Task.Factory.StartNew(() =>
                        child.StandardOutput.BaseStream.CopyTo(Stream.Null));
                    var drainError = Task.Factory.StartNew(() =>
                        child.StandardError.BaseStream.CopyTo(Stream.Null));
                    child.WaitForExit();
                    Task.WaitAll(drainOutput, drainError);
                    Record("child_exit:" + child.ExitCode);
                    return child.ExitCode;
                }
                Task.Factory.StartNew(() => {
                    try { Pump(Console.OpenStandardInput(), child.StandardInput.BaseStream); }
                    catch (IOException) {} catch (ObjectDisposedException) {}
                    finally { try { child.StandardInput.Close(); } catch (IOException) {} }
                });
                var output = Task.Factory.StartNew(() =>
                    Pump(child.StandardOutput.BaseStream, Console.OpenStandardOutput()));
                var error = Task.Factory.StartNew(() =>
                    Pump(child.StandardError.BaseStream, Console.OpenStandardError()));
                child.WaitForExit();
                Task.WaitAll(output, error);
                if (mcp) return child.ExitCode;
            }
        } catch (Exception error) {
            // Hooks fail open, without modal error dialogs or private payload logging.
            if (watchdog) { Record("failed:" + error.GetType().Name); return 1; }
        }
        return 0;
    }
}
'''


def launcher_paths(folder: Path, events: dict[str, str]) -> dict[str, Path]:
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()[:12]
    return {
        name: folder / (
            f"varyz-mcp-{digest}.exe" if event == "mcp"
            else f"varyz-watchdog-{digest}.exe" if event == "watchdog"
            else f"varyz-hook-{event}-{digest}.exe"
        ) for name, event in events.items()
    }


def write_launchers(folder: Path, config_path: Path, events: dict[str, str]) -> dict[str, Path]:
    """Compile with the Windows inbox framework, then publish immutable launchers."""
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()[:12]
    cache = folder / ".windowless" / digest
    binary = cache / "launcher.exe"
    if not binary.exists():
        windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        candidates = [
            windows / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
            for framework in ("Framework64", "Framework")
        ]
        compiler = next((path for path in candidates if path.is_file()), None)
        if compiler is None:
            raise RuntimeError("Windows .NET Framework compiler unavailable; hooks unchanged")
        cache.mkdir(parents=True, exist_ok=True)
        source = cache / f"launcher-{os.getpid()}.cs"
        temporary = cache / f"launcher-{os.getpid()}.exe"
        try:
            source.write_text(SOURCE, encoding="utf-8")
            subprocess.run(
                [str(compiler), "/nologo", "/target:winexe", f"/out:{temporary}", str(source)],
                capture_output=True, check=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            os.replace(temporary, binary)
        finally:
            source.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
    scripts = launcher_paths(folder, events)
    for name, script in scripts.items():
        event = events[name]
        if not script.exists():
            temporary = script.with_suffix(f".{os.getpid()}.tmp")
            try:
                shutil.copyfile(binary, temporary)
                os.replace(temporary, script)
            finally:
                temporary.unlink(missing_ok=True)
        sidecar = Path(str(script) + ".launch")
        payload = "\n".join((os.path.abspath(sys.executable), event, str(config_path))) + "\n"
        temporary = sidecar.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, sidecar)
        finally:
            temporary.unlink(missing_ok=True)
    return scripts
