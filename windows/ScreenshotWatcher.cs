// Build with the Windows .NET Framework compiler; no NuGet dependencies.
// Python runs only after startup recovery, relevant filesystem events, or an
// explicit pending-work exit code. There is no timer or scan while idle.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace ScreenshotInOne
{
    internal sealed class Options
    {
        internal string Python;
        internal string Project;
        internal string Config;
        internal int Debounce;
        internal readonly List<string> Roots = new List<string>();

        internal static Options Parse(string[] args)
        {
            Options value = new Options();
            HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
            for (int i = 0; i < args.Length; i += 2)
            {
                if (i + 1 >= args.Length) throw new ArgumentException("Every option requires a value.");
                string key = args[i], argument = args[i + 1];
                if (key != "--root" && !seen.Add(key)) throw new ArgumentException("Duplicate option.");
                switch (key)
                {
                    case "--python": value.Python = Path.GetFullPath(argument); break;
                    case "--project": value.Project = Path.GetFullPath(argument); break;
                    case "--config": value.Config = Path.GetFullPath(argument); break;
                    case "--debounce-ms":
                        if (!Int32.TryParse(argument, out value.Debounce) || value.Debounce < 1)
                            throw new ArgumentException("Debounce must be between 1 and 2147483647 milliseconds.");
                        break;
                    case "--root":
                        string root = Path.GetFullPath(argument).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                        if (!value.Roots.Exists(delegate(string existing) { return String.Equals(existing, root, StringComparison.OrdinalIgnoreCase); }))
                            value.Roots.Add(root);
                        if (value.Roots.Count > 32) throw new ArgumentException("At most 32 roots are supported.");
                        break;
                    default: throw new ArgumentException("Unknown watcher option.");
                }
            }
            if (value.Python == null || !File.Exists(value.Python) || !String.Equals(Path.GetFileName(value.Python), "pythonw.exe", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("--python must identify an existing pythonw.exe.");
            if (value.Project == null || !Directory.Exists(value.Project)) throw new ArgumentException("--project must identify an existing directory.");
            if (value.Config == null || !File.Exists(value.Config)) throw new ArgumentException("--config must identify an existing configuration file.");
            if (value.Debounce == 0 || value.Roots.Count == 0) throw new ArgumentException("--debounce-ms and at least one --root are required.");
            foreach (string root in value.Roots)
                if (!Directory.Exists(root)) throw new ArgumentException("Every --root must identify an existing userdata directory.");
            return value;
        }

        internal string MutexName()
        {
            byte[] bytes = Encoding.UTF8.GetBytes(Config.ToUpperInvariant());
            using (SHA256 hash = SHA256.Create())
                return "Global\\ScreenshotInOne-" + BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "");
        }
    }

    internal sealed class Log
    {
        private readonly string path;
        private readonly object gate = new object();
        internal Log(string directory)
        {
            Directory.CreateDirectory(directory);
            path = Path.Combine(directory, "watcher.log");
        }
        internal void Write(string message)
        {
            lock (gate)
            {
                try
                {
                    if (File.Exists(path) && new FileInfo(path).Length >= 262144)
                    {
                        string previous = path + ".1";
                        if (File.Exists(previous)) File.Delete(previous);
                        File.Move(path, previous);
                    }
                    if (message.Length > 2000) message = message.Substring(0, 2000);
                    File.AppendAllText(path, DateTime.UtcNow.ToString("o") + " " + message + Environment.NewLine, Encoding.UTF8);
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }

    // A job is assigned while the child is suspended, before Python can run.
    // Closing the parent (including Task Scheduler termination) closes the job
    // handle and terminates its child; a replaced watcher cannot leave uploads behind.
    internal sealed class ChildJob : IDisposable
    {
        private IntPtr handle;
        internal ChildJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = 0x00002000; // KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(handle, 9, ref limits, (uint)Marshal.SizeOf(limits)))
            {
                int error = Marshal.GetLastWin32Error();
                Dispose();
                throw new Win32Exception(error);
            }
        }

        internal Process Start(ProcessStartInfo info)
        {
            if (info.UseShellExecute || !info.CreateNoWindow || info.WindowStyle != ProcessWindowStyle.Hidden)
                throw new InvalidOperationException("Worker must be launched without a shell or window.");
            STARTUPINFO startup = new STARTUPINFO();
            startup.cb = Marshal.SizeOf(startup);
            startup.dwFlags = 1; // STARTF_USESHOWWINDOW
            startup.wShowWindow = 0; // SW_HIDE
            PROCESS_INFORMATION child;
            StringBuilder command = new StringBuilder(Quote(info.FileName) + " " + info.Arguments);
            if (!CreateProcess(info.FileName, command, IntPtr.Zero, IntPtr.Zero, false,
                               0x08000004, IntPtr.Zero, info.WorkingDirectory, ref startup, out child))
                throw new Win32Exception(Marshal.GetLastWin32Error()); // NO_WINDOW | SUSPENDED
            Process process = null;
            try
            {
                if (!AssignProcessToJobObject(handle, child.hProcess)) throw new Win32Exception(Marshal.GetLastWin32Error());
                process = Process.GetProcessById((int)child.dwProcessId);
                IntPtr retainedHandle = process.Handle;
                if (ResumeThread(child.hThread) == UInt32.MaxValue) throw new Win32Exception(Marshal.GetLastWin32Error());
                return process;
            }
            catch
            {
                // This handle identifies only the child we created suspended.
                TerminateProcess(child.hProcess, 1);
                if (process != null) process.Dispose();
                throw;
            }
            finally
            {
                CloseHandle(child.hThread);
                CloseHandle(child.hProcess);
            }
        }

        internal static string Quote(string argument)
        {
            StringBuilder quoted = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in argument)
            {
                if (character == '\\') { slashes++; continue; }
                if (character == '"') quoted.Append('\\', slashes * 2 + 1).Append('"');
                else quoted.Append('\\', slashes).Append(character);
                slashes = 0;
            }
            return quoted.Append('\\', slashes * 2).Append('"').ToString();
        }

        public void Dispose()
        {
            if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            internal long PerProcessUserTimeLimit, PerJobUserTimeLimit;
            internal uint LimitFlags;
            internal UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
            internal uint ActiveProcessLimit;
            internal UIntPtr Affinity;
            internal uint PriorityClass, SchedulingClass;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            internal ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            internal ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            internal JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            internal IO_COUNTERS IoInfo;
            internal UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            internal int cb;
            internal string lpReserved, lpDesktop, lpTitle;
            internal uint dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
            internal short wShowWindow, cbReserved2;
            internal IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            internal IntPtr hProcess, hThread;
            internal uint dwProcessId, dwThreadId;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int informationClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, uint length);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(string application, StringBuilder command, IntPtr processAttributes, IntPtr threadAttributes,
            bool inheritHandles, uint flags, IntPtr environment, string currentDirectory, ref STARTUPINFO startup, out PROCESS_INFORMATION process);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);
    }

    internal sealed class Relay : IDisposable
    {
        private readonly Options options;
        private readonly Log log;
        private readonly object gate = new object();
        private readonly AutoResetEvent wake = new AutoResetEvent(false);
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private readonly List<FileSystemWatcher> watchers = new List<FileSystemWatcher>();
        private readonly ChildJob job;
        private bool eventPending, retryPending, disposed, watcherFailed;
        private long eventDue, eventStarted, retryDue;
        private int retrySeconds = 5, startFailures;
        private Process worker;

        internal Relay(Options options, Log log)
        {
            this.options = options;
            this.log = log;
            job = new ChildJob();
        }

        internal void Arm()
        {
            foreach (string root in options.Roots)
            {
                FileSystemWatcher watcher = new FileSystemWatcher(root);
                watcher.IncludeSubdirectories = true;
                watcher.NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName | NotifyFilters.LastWrite | NotifyFilters.Size;
                watcher.InternalBufferSize = 16384;
                watcher.Created += OnCreated;
                watcher.Changed += OnChanged;
                watcher.Renamed += OnRenamed;
                watcher.Error += OnError;
                watchers.Add(watcher);
                watcher.EnableRaisingEvents = true;
            }
            // All roots are armed before the recovery scan: events arriving
            // during startup or its worker run remain pending for another pass.
            lock (gate) { eventPending = true; eventStarted = eventDue = clock.ElapsedMilliseconds; }
            log.Write("Watcher armed for " + watchers.Count + " root(s); starting recovery sync.");
        }

        private static bool Digits(string value)
        {
            if (value.Length == 0) return false;
            foreach (char character in value) if (character < '0' || character > '9') return false;
            return true;
        }

        // Exposed internally so a small standalone test assembly can exercise
        // routing without generating an operating-system buffer overflow.
        internal static bool Relevant(string relative, bool directory)
        {
            if (String.IsNullOrEmpty(relative) || relative.Length > 4096) return false;
            string[] parts = relative.Split(new char[] { '\\', '/' });
            int expected = directory ? parts.Length : 6;
            if (parts.Length != expected || parts.Length < 1 || parts.Length > (directory ? 5 : 6)) return false;
            if (!Digits(parts[0])) return false;
            if (parts.Length > 1 && parts[1] != "760") return false;
            if (parts.Length > 2 && !String.Equals(parts[2], "remote", StringComparison.OrdinalIgnoreCase)) return false;
            if (parts.Length > 3 && !Digits(parts[3])) return false;
            if (parts.Length > 4 && !String.Equals(parts[4], "screenshots", StringComparison.OrdinalIgnoreCase)) return false;
            if (directory) return true;
            string extension = Path.GetExtension(parts[5]);
            return String.Equals(extension, ".jpg", StringComparison.OrdinalIgnoreCase)
                || String.Equals(extension, ".jpeg", StringComparison.OrdinalIgnoreCase)
                || String.Equals(extension, ".png", StringComparison.OrdinalIgnoreCase)
                || String.Equals(extension, ".webp", StringComparison.OrdinalIgnoreCase);
        }

        private void MarkDirty()
        {
            lock (gate)
            {
                if (disposed) return;
                long now = clock.ElapsedMilliseconds;
                if (!eventPending) eventStarted = now;
                eventPending = true;
                // A continuous stream still receives a bounded recovery pass.
                eventDue = Math.Min(now + options.Debounce, eventStarted + Math.Max(10000, options.Debounce * 4L));
                startFailures = 0;
                wake.Set();
            }
        }

        private void OnChanged(object sender, FileSystemEventArgs args)
        {
            if (Relevant(args.Name, false)) MarkDirty();
        }
        private void OnCreated(object sender, FileSystemEventArgs args)
        {
            if (Relevant(args.Name, false) || (Relevant(args.Name, true) && Directory.Exists(args.FullPath))) MarkDirty();
        }
        private void OnRenamed(object sender, RenamedEventArgs args)
        {
            if (Relevant(args.Name, false) || (Relevant(args.Name, true) && Directory.Exists(args.FullPath))) MarkDirty();
        }
        private void OnError(object sender, ErrorEventArgs args)
        {
            if (args.GetException() is InternalBufferOverflowException)
            {
                log.Write("Filesystem notification buffer overflow; scheduling full recovery sync.");
                MarkDirty();
            }
            else
            {
                log.Write("Filesystem watcher stopped; restart is required.");
                lock (gate) { watcherFailed = true; if (!disposed) wake.Set(); }
            }
        }

        private void ScheduleRetry(string reason)
        {
            lock (gate)
            {
                retryPending = true;
                retryDue = clock.ElapsedMilliseconds + retrySeconds * 1000L;
                log.Write(reason + "; retry in " + retrySeconds + " seconds while work is pending.");
                retrySeconds = Math.Min(300, retrySeconds * 2);
            }
        }

        private void Launch()
        {
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = options.Python;
            info.Arguments = "-m screenshotinone --config " + ChildJob.Quote(options.Config) + " event-sync";
            info.WorkingDirectory = options.Project;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WindowStyle = ProcessWindowStyle.Hidden;
            try
            {
                worker = job.Start(info);
                worker.Exited += delegate { lock (gate) { if (!disposed) wake.Set(); } };
                worker.EnableRaisingEvents = true;
                log.Write("Sync worker started (PID " + worker.Id + ").");
                lock (gate) { startFailures = 0; }
            }
            catch (Exception error)
            {
                log.Write("Cannot start sync worker (" + error.GetType().Name + ").");
                if (worker != null)
                {
                    // If attaching the exit notification failed after launch,
                    // stop this exact owned child before another can be started.
                    if (!worker.HasExited)
                    {
                        worker.Kill();
                        if (!worker.WaitForExit(5000)) throw new InvalidOperationException("Cannot retire the failed worker.");
                    }
                    worker.Dispose(); worker = null;
                }
                bool retry;
                lock (gate) { startFailures++; retry = startFailures <= 3; }
                if (retry) ScheduleRetry("Worker launch failed");
                else log.Write("Worker launch retries exhausted; waiting for a new screenshot or watcher restart.");
            }
        }

        internal int Run()
        {
            while (true)
            {
                lock (gate) { if (watcherFailed) return 1; }
                if (worker != null && worker.HasExited)
                {
                    int exitCode = worker.ExitCode;
                    worker.Dispose();
                    worker = null;
                    log.Write("Sync worker exited with code " + exitCode + ".");
                    if (exitCode == 10) ScheduleRetry("Sync reports pending work");
                    else
                    {
                        lock (gate) { retryPending = false; retrySeconds = 5; }
                        if (exitCode != 0) log.Write("Sync needs attention; waiting for a new screenshot or watcher restart.");
                    }
                }
                bool launch = false;
                int wait = Timeout.Infinite;
                lock (gate)
                {
                    if (worker == null)
                    {
                        long due = Int64.MaxValue;
                        if (eventPending) due = eventDue;
                        if (retryPending) due = Math.Min(due, retryDue);
                        if (due != Int64.MaxValue)
                        {
                            long remaining = due - clock.ElapsedMilliseconds;
                            if (remaining <= 0)
                            {
                                launch = true;
                                eventPending = retryPending = false;
                            }
                            else wait = (int)Math.Min(Int32.MaxValue, remaining);
                        }
                    }
                }
                if (launch) { Launch(); continue; }
                // Infinite when idle or uploading. Only filesystem callbacks
                // and the current worker's exit signal wake the resident relay.
                wake.WaitOne(wait);
            }
        }

        public void Dispose()
        {
            lock (gate) { disposed = true; }
            foreach (FileSystemWatcher watcher in watchers) watcher.Dispose();
            job.Dispose();
            if (worker != null) worker.Dispose();
            wake.Dispose();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Log log = null;
            try
            {
                Options options = Options.Parse(args);
                log = new Log(Path.GetDirectoryName(options.Config));
                using (Mutex single = new Mutex(false, options.MutexName()))
                {
                    bool owned;
                    try { owned = single.WaitOne(0, false); }
                    catch (AbandonedMutexException) { owned = true; }
                    if (!owned) { log.Write("A watcher already owns this configuration; duplicate instance exited."); return 0; }
                    try
                    {
                        using (Relay relay = new Relay(options, log)) { relay.Arm(); return relay.Run(); }
                    }
                    finally { single.ReleaseMutex(); }
                }
            }
            catch (Exception error)
            {
                try
                {
                    if (log == null) log = new Log(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "screenshotinone"));
                    log.Write("Watcher failed (" + error.GetType().Name + "): " + error.Message);
                }
                catch { }
                return 1;
            }
        }
    }
}
