using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Web.Script.Serialization;
[ComImport, Guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface DeviceEnumerator {
    [PreserveSig] int CreateClassEnumerator([In] ref Guid category, out IEnumMoniker items, int flags);
}
[ComImport, Guid("55272A00-42CB-11CE-8135-00AA004BB851"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface DevicePropertyBag {
    [PreserveSig] int Read([MarshalAs(UnmanagedType.LPWStr)] string name, [MarshalAs(UnmanagedType.Struct)] out object value, IntPtr error);
    [PreserveSig] int Write([MarshalAs(UnmanagedType.LPWStr)] string name, [In, MarshalAs(UnmanagedType.Struct)] ref object value);
}
class CameraDevices {
    static object[] List() {
        var result = new List<object>();
        object owner = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")));
        IEnumMoniker items = null;
        try {
            var category = new Guid("860BB310-5D01-11D0-BD3B-00A0C911CE86");
            if (((DeviceEnumerator)owner).CreateClassEnumerator(ref category, out items, 0) != 0) return result.ToArray();
            var one = new IMoniker[1];
            int index = 0;
            while (items.Next(1, one, IntPtr.Zero) == 0) {
                object bag = null;
                try {
                    var iid = new Guid("55272A00-42CB-11CE-8135-00AA004BB851");
                    try { one[0].BindToStorage(null, null, ref iid, out bag); } catch { continue; }
                    var props = (DevicePropertyBag)bag;
                    object name, path;
                    if (props.Read("FriendlyName", out name, IntPtr.Zero) < 0) props.Read("Description", out name, IntPtr.Zero);
                    props.Read("DevicePath", out path, IntPtr.Zero);
                    string moniker;
                    one[0].GetDisplayName(null, null, out moniker);
                    result.Add(new { index = index++, name = name, device_path = path, moniker = moniker });
                } finally {
                    if (bag != null) Marshal.ReleaseComObject(bag);
                    Marshal.ReleaseComObject(one[0]);
                }
            }
        } finally {
            if (items != null) Marshal.ReleaseComObject(items);
            Marshal.ReleaseComObject(owner);
        }
        return result.ToArray();
    }
    static int Main() {
        try {
            Console.OutputEncoding = new System.Text.UTF8Encoding(false);
            Console.WriteLine(new JavaScriptSerializer().Serialize(List()));
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
    }
}
