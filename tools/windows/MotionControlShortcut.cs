// 使用原生宽字符接口保存快捷方式，中文路径不依赖系统的非 Unicode 代码页。
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
internal class MotionControlShellLink { }

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMotionControlShellLink
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr data, uint flags);
    void GetIDList(out IntPtr list);
    void SetIDList(IntPtr list);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
    void GetHotkey(out short value);
    void SetHotkey(short value);
    void GetShowCmd(out int value);
    void SetShowCmd(int value);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public static class MotionControlShortcut
{
    public static string[] Read(string file)
    {
        var link = (IMotionControlShellLink)new MotionControlShellLink();
        try
        {
            ((IPersistFile)link).Load(file, 0);
            var target = new StringBuilder(32768);
            var directory = new StringBuilder(32768);
            var icon = new StringBuilder(32768);
            var arguments = new StringBuilder(32768);
            int index;
            link.GetPath(target, target.Capacity, IntPtr.Zero, 4);
            link.GetWorkingDirectory(directory, directory.Capacity);
            link.GetIconLocation(icon, icon.Capacity, out index);
            link.GetArguments(arguments, arguments.Capacity);
            return new[] { target.ToString(), directory.ToString(), icon + "," + index, arguments.ToString() };
        }
        finally { Marshal.FinalReleaseComObject(link); }
    }

    public static void Save(string file, string target, string directory)
    {
        var link = (IMotionControlShellLink)new MotionControlShellLink();
        try
        {
            link.SetPath(target);
            link.SetWorkingDirectory(directory);
            link.SetDescription("MotionControl · 体感控制");
            link.SetIconLocation(target, 0);
            ((IPersistFile)link).Save(file, true);
        }
        finally { Marshal.FinalReleaseComObject(link); }
    }
}
