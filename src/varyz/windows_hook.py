"""Windows GUI entry points: no shell or console is created at launch."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from varyz.update_worker import sentinel_path

SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
class HookLauncher {
    // What a Varyz connection writes to end itself for an update
    // (mcp_server.RESTART_LINE). It never reaches the client.
    const string RestartLine =
        "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/varyz/restart\"}";
    static readonly object gate = new object();
    // The current connection's stdin; null while it restarts.
    static Stream childInput;
    static bool clientClosed;
    static volatile bool restartSeen;
    // update_worker.sentinel_path: fresh while an update installs.
    static string marker;
    static string Location() { return Assembly.GetExecutingAssembly().Location; }
    static void Record(string state) {
        try { File.WriteAllText(Location() + ".status", state); }
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
    // The update worker touches its marker every few seconds, so one left by a
    // crashed update stops counting after two minutes.
    static bool Updating() {
        if (marker == null) return false;
        try {
            var file = new FileInfo(marker);
            return file.Exists &&
                DateTime.UtcNow - file.LastWriteTimeUtc < TimeSpan.FromSeconds(120);
        } catch (Exception) { return false; }
    }
    static void AwaitUpdate() {
        DateTime until = DateTime.UtcNow.AddMinutes(30);
        while (Updating() && DateTime.UtcNow < until) Thread.Sleep(500);
    }
    // python, event, config and, from launchers that can restart, the marker.
    static string[] ReadConfig() {
        string[] config = File.ReadAllLines(Location() + ".launch");
        if (config.Length != 3 && config.Length != 4) return null;
        marker = config.Length == 4 ? config[3] : null;
        return config;
    }
    static ProcessStartInfo Command(string[] config, bool watchdog, bool mcp) {
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
        if (mcp && marker != null) info.EnvironmentVariables["VARYZ_MCP_RELAY"] = "1";
        return info;
    }
    // Client requests go to the current connection whole lines at a time, and
    // wait while it restarts.
    static void Send(byte[] data, int count) {
        lock (gate) {
            while (childInput == null) Monitor.Wait(gate);
            try { childInput.Write(data, 0, count); childInput.Flush(); }
            catch (IOException) {} catch (ObjectDisposedException) {}
        }
    }
    static void ForwardInput() {
        var line = new MemoryStream();
        try {
            Stream stdin = Console.OpenStandardInput();
            byte[] buffer = new byte[8192];
            int read;
            while ((read = stdin.Read(buffer, 0, buffer.Length)) > 0) {
                int start = 0;
                for (int i = 0; i < read; i++) {
                    if (buffer[i] != (byte)'\n') continue;
                    line.Write(buffer, start, i + 1 - start);
                    Send(line.GetBuffer(), (int)line.Length);
                    line.SetLength(0);
                    start = i + 1;
                }
                line.Write(buffer, start, read - start);
            }
            if (line.Length > 0) Send(line.GetBuffer(), (int)line.Length);
        } catch (IOException) {} catch (ObjectDisposedException) {}
        finally {
            lock (gate) {
                clientClosed = true;
                if (childInput != null) {
                    try { childInput.Close(); } catch (IOException) {}
                }
            }
        }
    }
    // The connection's replies reach the client unchanged, all but its
    // restart line, on which it gets no more requests.
    static void Relay(Stream source, Stream target) {
        var line = new MemoryStream();
        byte[] buffer = new byte[8192];
        int read;
        while ((read = source.Read(buffer, 0, buffer.Length)) > 0) {
            int start = 0;
            for (int i = 0; i < read; i++) {
                if (buffer[i] != (byte)'\n') continue;
                line.Write(buffer, start, i + 1 - start);
                Reply(line, target);
                start = i + 1;
            }
            line.Write(buffer, start, read - start);
        }
        if (line.Length > 0) Reply(line, target);
    }
    static void Reply(MemoryStream line, Stream target) {
        byte[] data = line.GetBuffer();
        int count = (int)line.Length;
        line.SetLength(0);
        if (count <= RestartLine.Length + 2 &&
                Encoding.UTF8.GetString(data, 0, count).TrimEnd('\r', '\n') == RestartLine) {
            restartSeen = true;
            lock (gate) {
                if (childInput != null) {
                    try { childInput.Close(); } catch (IOException) {}
                    childInput = null;
                }
            }
            return;
        }
        target.Write(data, 0, count);
        target.Flush();
    }
    // An agent's Varyz connection. It lives as long as the agent, and when
    // Varyz updates, its connection ends between two requests and a new one
    // starts on the new install, without the agent noticing.
    static int Serve(string[] config) {
        var input = new Thread(ForwardInput);
        input.IsBackground = true;
        Stream output = Console.OpenStandardOutput();
        Stream errors = Console.OpenStandardError();
        bool reading = false;
        while (true) {
            AwaitUpdate();
            restartSeen = false;
            using (var child = Process.Start(Command(config, false, true))) {
                lock (gate) {
                    childInput = child.StandardInput.BaseStream;
                    if (clientClosed) child.StandardInput.Close();
                    Monitor.PulseAll(gate);
                }
                if (!reading) { input.Start(); reading = true; }
                var replies = Task.Factory.StartNew(() =>
                    Relay(child.StandardOutput.BaseStream, output));
                var error = Task.Factory.StartNew(() =>
                    Pump(child.StandardError.BaseStream, errors));
                child.WaitForExit();
                Task.WaitAll(replies, error);
                bool closed;
                lock (gate) { childInput = null; closed = clientClosed; }
                if (!restartSeen || closed) return child.ExitCode;
            }
            config = ReadConfig();
            if (config == null || config[1] != "mcp" ||
                !File.Exists(config[0]) || !File.Exists(config[2])) return 1;
        }
    }
    [STAThread]
    static int Main() {
        bool watchdog = false;
        try {
            string[] config = ReadConfig();
            if (config == null) return 0;
            watchdog = config[1] == "watchdog";
            if (!File.Exists(config[0]) || !File.Exists(config[2])) {
                if (watchdog) { Record("failed:missing_launch_file"); return 1; }
                return 0;
            }
            if (config[1] != "session-start" && config[1] != "user-prompt" &&
                config[1] != "stop" && config[1] != "mcp" &&
                config[1] != "watchdog") return 0;
            bool mcp = config[1] == "mcp";
            if (mcp) return Serve(config);
            // Hooks fail open; starting Varyz now would hold the files the
            // update replaces.
            if (!watchdog && Updating()) return 0;
            if (watchdog) {
                // Returning would end the startup loop that keeps it running.
                AwaitUpdate();
                Record("starting");
            }
            using (var child = Process.Start(Command(config, watchdog, false))) {
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
        # The fourth line lets the launcher hold off while Varyz updates.
        payload = "\n".join((
            os.path.abspath(sys.executable), event, str(config_path),
            str(sentinel_path(folder.parent)),
        )) + "\n"
        temporary = sidecar.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, sidecar)
        finally:
            temporary.unlink(missing_ok=True)
    return scripts
