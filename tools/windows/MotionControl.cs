// 固定的桌面入口。程序与数据可以迁移，快捷方式不绑定 Python 或开发副本。
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("MotionControl")]
[assembly: AssemblyProduct("MotionControl")]
[assembly: AssemblyDescription("MotionControl 桌面入口")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class MotionControlDesktop
{
    private const string RegistryPath = @"Software\MotionControl";
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

    private static Dictionary<string, object> Installation()
    {
        string local = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "installation.json");
        if (File.Exists(local))
        {
            try
            {
                Dictionary<string, object> value = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(local, Encoding.UTF8));
                if (!String.IsNullOrEmpty(Value(value, "program_root")) && !String.IsNullOrEmpty(Value(value, "data_root"))) return value;
            }
            catch (Exception) { /* 保留原件，回退到安装时内嵌的完整位置记录。 */ }
        }
        using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("MotionControl.Installation"))
        {
            if (stream == null) return new Dictionary<string, object>();
            using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                return Json.Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
        }
    }

    private static string Value(Dictionary<string, object> data, string key)
    {
        object value;
        return data != null && data.TryGetValue(key, out value) ? value as string : null;
    }

    private static string DataRoot(Dictionary<string, object> settings)
    {
        string root = Value(settings, "data_root"), identity = Value(settings, "data_id");
        if (!String.IsNullOrEmpty(identity))
        {
            try
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                {
                    string raw = key == null ? null : key.GetValue("DataLocation") as string;
                    Dictionary<string, object> registered = String.IsNullOrEmpty(raw) ? null : Json.Deserialize<Dictionary<string, object>>(raw);
                    string candidate = Value(registered, "path");
                    if (Value(registered, "id") == identity && Directory.Exists(candidate)) root = candidate;
                }
            }
            catch (Exception) { /* 跨应用启动时保留自身记录，不使用其它身份的默认数据。 */ }
        }
        if (String.IsNullOrEmpty(root) || !Directory.Exists(root)) throw new IOException("找不到原设置目录，未创建默认配置。请重新安装桌面入口以定位原设置。");
        if (!String.IsNullOrEmpty(identity))
        {
            Dictionary<string, object> marker = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(root, ".motioncontrol-data.json"), Encoding.UTF8));
            if (Value(marker, "id") != identity) throw new IOException("设置目录身份不相符，原配置未被替换。");
        }
        return root;
    }

    private static void Remember(Dictionary<string, object> settings, string root)
    {
        if (Value(settings, "program_root") == root) return;
        settings["program_root"] = root;
        string path = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "installation.json");
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, Json.Serialize(settings), new UTF8Encoding(false));
            if (File.Exists(path)) { File.Copy(path, path + ".previous", true); File.Replace(temporary, path, null); }
            else File.Move(temporary, path);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    private static string ShellPath()
    {
        string modern = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "PowerShell", "7", "pwsh.exe");
        return File.Exists(modern) ? modern : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    }

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
        bool diagnose = args.Length == 2 && args[0] == "--diagnose";
        bool noBrowser = args.Length == 1 && args[0] == "--no-browser";
        try
        {
            if (verify)
            {
                bool valid = IsProgram(args[1]);
                File.WriteAllText(args[2], valid ? "ready" : "missing-program");
                return valid ? 0 : 2;
            }
            if (args.Length != 0 && !diagnose && !noBrowser) throw new ArgumentException("不支持这个启动参数。");
            Dictionary<string, object> installation = Installation();
            if (diagnose)
            {
                string installed = Value(installation, "program_root");
                File.WriteAllText(args[1], Json.Serialize(new { program_root = installed, program_ready = IsProgram(installed), shell = ShellPath(), data_root = DataRoot(installation) }), new UTF8Encoding(false));
                return IsProgram(installed) ? 0 : 2;
            }
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
                    root = Value(installation, "program_root");
                    if (!IsProgram(root)) using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RegistryPath))
                        root = key == null ? null : key.GetValue("ProgramRoot") as string;
                    if (!IsProgram(root))
                    {
                        if (noBrowser) throw new IOException("程序文件夹未找到，请重新安装桌面入口。");
                        using (FolderBrowserDialog picker = new FolderBrowserDialog())
                        {
                            picker.Description = "请选择 MotionControl 程序文件夹，不需要寻找脚本文件。";
                            picker.ShowNewFolderButton = false;
                            if (picker.ShowDialog() != DialogResult.OK) return 2;
                            root = picker.SelectedPath;
                        }
                        if (!IsProgram(root)) throw new IOException("所选目录不完整，需要同时包含 START.ps1 和 server.py。");
                        using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RegistryPath))
                            key.SetValue("ProgramRoot", root);
                    }
                    Remember(installation, root);
                    string shell = ShellPath();
                    ProcessStartInfo start = new ProcessStartInfo(shell,
                        "-NoLogo -NoProfile -ExecutionPolicy Bypass -File " + Quote(Path.Combine(root, "START.ps1")) + " -Desktop" + (noBrowser ? " -NoBrowser" : ""));
                    start.WorkingDirectory = root;
                    start.UseShellExecute = false;
                    start.CreateNoWindow = true;
                    start.EnvironmentVariables["MOTIONCONTROL_USER_DIR"] = DataRoot(installation);
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
            if (verify || diagnose) { File.WriteAllText(args[args.Length - 1], error.Message); return 1; }
            if (noBrowser) return 1;
            MessageBox.Show(error.Message, "MotionControl", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
    }
}
