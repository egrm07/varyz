using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;

// Compile as winexe. Task Scheduler's Hidden setting alone does not hide processes.
class CloudBackupLauncher
{
    static int Main()
    {
        try
        {
            string state = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string root = Directory.GetParent(Directory.GetParent(state).FullName).FullName;
            var info = new ProcessStartInfo
            {
                FileName = Path.Combine(root, ".venv", "Scripts", "python.exe"),
                Arguments = "\"" + Path.Combine(root, "scripts", "cloud_backup_pull.py")
                    + "\" --state \"" + state + "\"",
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (var child = Process.Start(info))
            {
                child.StandardInput.Close();
                var output = Task.Factory.StartNew(() => child.StandardOutput.BaseStream.CopyTo(Stream.Null));
                var error = Task.Factory.StartNew(() => child.StandardError.BaseStream.CopyTo(Stream.Null));
                child.WaitForExit();
                Task.WaitAll(output, error);
                return child.ExitCode;
            }
        }
        catch
        {
            return 1;
        }
    }
}
