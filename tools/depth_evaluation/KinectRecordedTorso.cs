using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Kinect;

// 只重放保存的深度。传感器仅提供同一设备的工厂坐标映射，不新建采集读取器。
internal static class KinectRecordedTorso
{
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 2000000 };
    static Dictionary<string, object> Fields(params object[] pairs)
    {
        var result = new Dictionary<string, object>();
        for (int i = 0; i < pairs.Length; i += 2) result.Add((string)pairs[i], pairs[i + 1]);
        return result;
    }
    static bool Finite(float value) { return !Single.IsNaN(value) && !Single.IsInfinity(value); }
    // 与正式KinectCamera.Surface相同的3×3表面选择，不把内部关节的Z当作衣服表面。
    static object Surface(CameraSpacePoint point, CoordinateMapper mapper, ushort[] depths, ushort minimum, ushort maximum)
    {
        DepthSpacePoint pixel = mapper.MapCameraPointToDepthSpace(point);
        if (!Finite(pixel.X) || !Finite(pixel.Y)) return null;
        int x = (int)Math.Round(pixel.X), y = (int)Math.Round(pixel.Y);
        if (x < 1 || x >= 511 || y < 1 || y >= 423) return null;
        var values = new List<ushort>();
        for (int dy = -1; dy <= 1; dy++) for (int dx = -1; dx <= 1; dx++)
        {
            ushort z = depths[(y + dy) * 512 + x + dx];
            if (z >= minimum && z <= maximum) values.Add(z);
        }
        if (values.Count < 5) return null;
        values.Sort();
        ushort median = values[values.Count / 2];
        if (values[values.Count - 1] - values[0] > 120) return null;
        CameraSpacePoint surface = mapper.MapDepthPointToCameraSpace(new DepthSpacePoint { X = x, Y = y }, median);
        ColorSpacePoint color = mapper.MapCameraPointToColorSpace(surface);
        if (!Finite(color.X) || !Finite(color.Y) || !Finite(surface.Z)) return null;
        return Fields("z", surface.Z, "color_x", color.X / 1920.0, "color_y", color.Y / 1080.0);
    }
    static int Main(string[] args)
    {
        if (args.Length != 3)
        {
            Console.Error.WriteLine("用法：KinectRecordedTorso 输入.jsonl 原始深度.bin 新输出.jsonl");
            return 2;
        }
        KinectSensor sensor = KinectSensor.GetDefault();
        try
        {
            if (sensor == null) throw new InvalidOperationException("未发现原设备，不能取得工厂坐标映射");
            sensor.Open();
            for (int i = 0; i < 60 && sensor.CoordinateMapper.GetDepthCameraIntrinsics().FocalLengthX <= 0; i++) Thread.Sleep(50);
            var mapper = sensor.CoordinateMapper;
            var bytes = new byte[512 * 424 * 2];
            var depths = new ushort[512 * 424];
            using (var binary = File.OpenRead(args[1]))
            using (var output = new StreamWriter(new FileStream(args[2], FileMode.CreateNew), new UTF8Encoding(false)))
            foreach (string line in File.ReadLines(args[0], Encoding.UTF8))
            {
                var row = Json.Deserialize<Dictionary<string, object>>(line);
                if ((string)row["device_id"] != "kinect2:" + sensor.UniqueKinectId) throw new InvalidOperationException("工厂映射设备与录制不同");
                binary.Position = Convert.ToInt64(row["depth_offset"]);
                int count = 0;
                while (count < bytes.Length)
                {
                    int got = binary.Read(bytes, count, bytes.Length - count);
                    if (got == 0) throw new EndOfStreamException();
                    count += got;
                }
                Buffer.BlockCopy(bytes, 0, depths, 0, bytes.Length);
                var result = new Dictionary<string, object>();
                foreach (var pair in (Dictionary<string, object>)row["points"])
                {
                    var p = (Dictionary<string, object>)pair.Value;
                    var point = new CameraSpacePoint { X = Convert.ToSingle(p["x_m"]), Y = Convert.ToSingle(p["y_m"]), Z = Convert.ToSingle(p["z_m"]) };
                    result[pair.Key] = Surface(point, mapper, depths, Convert.ToUInt16(row["minimum"]), Convert.ToUInt16(row["maximum"]));
                }
                output.WriteLine(Json.Serialize(Fields("frame_index", row["frame_index"], "points", result)));
            }
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error.Message); return 1; }
        finally { if (sensor != null && sensor.IsOpen) sensor.Close(); }
    }
}
