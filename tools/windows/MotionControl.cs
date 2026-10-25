// 固定的桌面入口。程序与数据可以迁移，快捷方式不绑定 Python 或开发副本。
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("MotionControl")]
[assembly: AssemblyProduct("MotionControl")]
[assembly: AssemblyDescription("MotionControl 桌面入口")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class MotionControlDesktop
{
    private const string RegistryPath = @"Software\MotionControl";

    private static string Quote(string value)
    {
        // 文件夹由系统选择，完整文件路径不会以反斜线结尾。
        return "\"" + value + "\"";
    }

    private static bool IsProgram(string root)
    {
        return !String.IsNullOrWhiteSpace(root) && File.Exists(Path.Combine(root, "START.ps1"))
            && File.Exists(Path.Combine(root, "server.py"));
    }

    [STAThread]
    private static int Main(string[] args)
    {
        bool verify = args.Length == 3 && args[0] == "--verify";
        try
        {
            if (verify)
            {
                bool valid = IsProgram(args[1]);
                File.WriteAllText(args[2], valid ? "ready" : "missing-program");
                return valid ? 0 : 2;
            }
            if (args.Length != 0) throw new ArgumentException("不支持这个启动参数。");
            Application.EnableVisualStyles();
            using (Mutex gate = new Mutex(false, @"Local\MotionControl.Desktop.Start"))
            {
                bool acquired;
                try { acquired = gate.WaitOne(1500); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) return 0; // 连续双击不会重复启动后台。
                try
                {
                    string root;
                    using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                        root = key == null ? null : key.GetValue("ProgramRoot") as string;
                    if (!IsProgram(root))
                    {
                        using (OpenFileDialog picker = new OpenFileDialog())
                        {
                            picker.Title = "请选择迁移后的 MotionControl 文件夹中的 START.ps1";
                            picker.Filter = "MotionControl 启动文件|START.ps1";
                            picker.CheckFileExists = true;
                            if (picker.ShowDialog() != DialogResult.OK) return 2;
                            root = Path.GetDirectoryName(picker.FileName);
                        }
                        if (!IsProgram(root)) throw new IOException("所选目录不完整，需要同时包含 START.ps1 和 server.py。");
                        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
                            key.SetValue("ProgramRoot", root);
                    }
                    string shell;
                    using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                        shell = key == null ? null : key.GetValue("PowerShellPath") as string;
                    if (String.IsNullOrEmpty(shell) || !File.Exists(shell))
                    {
                        string standard = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "PowerShell", "7", "pwsh.exe");
                        if (File.Exists(standard)) shell = standard;
                        else using (OpenFileDialog picker = new OpenFileDialog())
                        {
                            picker.Title = "原 PowerShell 7 位置已失效，请选择现有的 pwsh.exe";
                            picker.Filter = "PowerShell 7|pwsh.exe";
                            if (picker.ShowDialog() != DialogResult.OK) return 2;
                            shell = picker.FileName;
                        }
                        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
                            key.SetValue("PowerShellPath", shell);
                    }
                    ProcessStartInfo start = new ProcessStartInfo(shell,
                        "-NoLogo -NoProfile -ExecutionPolicy Bypass -File " + Quote(Path.Combine(root, "START.ps1")) + " -Desktop");
                    start.WorkingDirectory = root;
                    start.UseShellExecute = false;
                    start.CreateNoWindow = true;
                    start.RedirectStandardOutput = true;
                    start.RedirectStandardError = true;
                    start.StandardOutputEncoding = Encoding.UTF8;
                    start.StandardErrorEncoding = Encoding.UTF8;
                    using (Process child = Process.Start(start))
                    {
                        StringBuilder messages = new StringBuilder();
                        DataReceivedEventHandler collect = delegate(object sender, DataReceivedEventArgs line)
                        { if (line.Data != null) lock (messages) messages.AppendLine(line.Data); };
                        child.OutputDataReceived += collect;
                        child.ErrorDataReceived += collect;
                        child.BeginOutputReadLine();
                        child.BeginErrorReadLine();
                        // 等待文件夹选择和 Windows 权限确认；服务就绪等待由启动脚本限时。
                        child.WaitForExit();
                        string log = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "last-start.log");
                        File.WriteAllText(log, messages.ToString(), Encoding.UTF8);
                        if (child.ExitCode != 0) throw new IOException("启动未完成，原配置已保留。请查看启动记录：" + log);
                    }
                    return 0;
                }
                finally { gate.ReleaseMutex(); }
            }
        }
        catch (Exception error)
        {
            if (verify) { File.WriteAllText(args[2], error.Message); return 1; }
            MessageBox.Show(error.Message, "MotionControl", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
    }
}
