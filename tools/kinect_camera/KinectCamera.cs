using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Kinect;

// Local binary pipe only: no pictures, depth files or recordings are saved.
internal static class KinectCamera
{
    const int Width = 640, Height = 360;
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static double Qpc() { return (double)Stopwatch.GetTimestamp() / Stopwatch.Frequency; }
    [DllImport("winmm.dll")] static extern uint timeBeginPeriod(uint milliseconds);
    [StructLayout(LayoutKind.Sequential)] struct PowerThrottling { public uint Version, ControlMask, StateMask; }
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool SetProcessInformation(IntPtr process, int infoClass, ref PowerThrottling info, int size);
    [DllImport("gdi32.dll")] static extern int D3DKMTSetProcessSchedulingPriorityClass(IntPtr process, int priority);
    static bool Finite(float v) { return !Single.IsNaN(v) && !Single.IsInfinity(v); }
    static Dictionary<string, object> Fields(params object[] pairs)
    {
        var row = new Dictionary<string, object>();
        for (int i = 0; i < pairs.Length; i += 2) row.Add((string)pairs[i], pairs[i + 1]);
        return row;
    }
    static readonly Dictionary<JointType, string> Names = new Dictionary<JointType, string> {
        {JointType.Head,"head"}, {JointType.SpineBase,"pelvis"}, {JointType.SpineShoulder,"chest"},
        {JointType.ShoulderLeft,"left_shoulder"}, {JointType.ShoulderRight,"right_shoulder"},
        {JointType.ElbowLeft,"left_elbow"}, {JointType.ElbowRight,"right_elbow"},
        {JointType.WristLeft,"left_wrist"}, {JointType.WristRight,"right_wrist"},
        {JointType.HipLeft,"left_hip"}, {JointType.HipRight,"right_hip"},
        {JointType.KneeLeft,"left_knee"}, {JointType.KneeRight,"right_knee"},
        {JointType.AnkleLeft,"left_ankle"}, {JointType.AnkleRight,"right_ankle"},
        {JointType.FootLeft,"left_foot"}, {JointType.FootRight,"right_foot"}
    };
    static object Bodies(Body[] bodies, CoordinateMapper mapper)
    {
        var result = new List<object>();
        foreach (Body body in bodies)
        {
            if (!body.IsTracked) continue;
            var points = new Dictionary<string, object>();
            foreach (var pair in Names)
            {
                Joint joint = body.Joints[pair.Key];
                CameraSpacePoint p = joint.Position;
                if (joint.TrackingState != TrackingState.Tracked || !Finite(p.X) || !Finite(p.Y) || !Finite(p.Z) || p.Z <= 0) continue;
                ColorSpacePoint color = mapper.MapCameraPointToColorSpace(p);
                if (!Finite(color.X) || !Finite(color.Y)) continue;
                points.Add(pair.Value, Fields("x",p.X,"y",p.Y,"z",p.Z,"score",1.0,
                    "color_x",color.X / 1920.0,"color_y",color.Y / 1080.0));
            }
            result.Add(Fields("tracking_id",body.TrackingId.ToString(),"points",points));
        }
        return result;
    }
    static void Send(BinaryWriter writer, object metadata, byte[] pixels)
    {
        byte[] text = Encoding.UTF8.GetBytes(Json.Serialize(metadata));
        writer.Write(text.Length);
        writer.Write(pixels == null ? 0 : pixels.Length);
        writer.Write(text);
        if (pixels != null) writer.Write(pixels);
        writer.Flush();
    }
    // 管理员授权后单次运行：调高（或恢复）原厂服务的处理器与显卡调度，游戏占满显卡时它仍能按时出帧。
    // 只针对 KinectService；服务或电脑重启后系统恢复默认，需要再次授权。
    static int SetServicePriority(bool raise)
    {
        Process[] found = Process.GetProcessesByName("KinectService");
        if (found.Length == 0) { Console.Error.WriteLine("未找到 Kinect 原厂服务"); return 2; }
        try
        {
            Process.EnterDebugMode(); // 原厂服务以系统账户运行，需要调试特权才能打开。
            Process service = found[0];
            service.PriorityClass = raise ? ProcessPriorityClass.AboveNormal : ProcessPriorityClass.Normal;
            // 4 = D3DKMT_SCHEDULINGPRIORITYCLASS_HIGH，2 = NORMAL。它的显卡工作量很小，排在游戏前面对游戏影响有限；
            // 实时级需要额外特权，也会压过系统画面合成，不用。
            int status = D3DKMTSetProcessSchedulingPriorityClass(service.Handle, raise ? 4 : 2);
            if (status != 0) { Console.Error.WriteLine("显卡调度优先级未被接受：0x" + status.ToString("X8")); return 4; }
            return 0;
        }
        catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 3; }
    }
    public static int Main(string[] args)
    {
        if (Array.IndexOf(args, "--boost-service") >= 0) return SetServicePriority(true);
        if (Array.IndexOf(args, "--restore-service") >= 0) return SetServicePriority(false);
        try
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            KinectSensor sensor = KinectSensor.GetDefault();
            if (Array.IndexOf(args, "--list") >= 0)
            {
                if (sensor != null) sensor.Open();
                double waitUntil = Qpc() + 3;
                while (sensor != null && String.IsNullOrEmpty(sensor.UniqueKinectId) && Qpc() < waitUntil) Thread.Sleep(50);
                Console.WriteLine(Json.Serialize(sensor == null ? new object[0] : new object[] {
                    Fields("id","kinect2:"+(String.IsNullOrEmpty(sensor.UniqueKinectId) ? "default" : sensor.UniqueKinectId),"name","微软 Kinect",
                           "depth_supported",true,"available",sensor.IsAvailable)
                }));
                if (sensor != null) sensor.Close();
                return 0;
            }
            if (sensor == null) throw new InvalidOperationException("未找到微软 Kinect；请检查电源、USB 接口和原厂驱动。");
            bool depthEnabled = Array.IndexOf(args, "--no-depth") < 0;
            // 默认计时精度下 Sleep(1) 约 15.6 毫秒，会给每帧白白加上等待。
            timeBeginPeriod(1);
            // 游戏在前台时不被系统压低调度或节能降速；只到“高于普通”，不抢游戏输入。
            Process.GetCurrentProcess().PriorityClass = ProcessPriorityClass.AboveNormal;
            var throttling = new PowerThrottling { Version = 1, ControlMask = 0x1 | 0x4, StateMask = 0 };
            SetProcessInformation(GetCurrentProcess(), 4, ref throttling, Marshal.SizeOf(throttling));
            try
            {
            // 原厂深度与骨骼在显卡上计算；分开读取，游戏占满显卡时彩色画面不必等它们同步。
            // 深度、骨骼仍按时间戳判断是否与当前画面相符，过旧即不附带。
            using (var writer = new BinaryWriter(Console.OpenStandardOutput()))
            using (ColorFrameReader colorReader = sensor.ColorFrameSource.OpenReader())
            using (DepthFrameReader depthReader = depthEnabled ? sensor.DepthFrameSource.OpenReader() : null)
            using (BodyFrameReader bodyReader = depthEnabled ? sensor.BodyFrameSource.OpenReader() : null)
            {
                byte[] full = new byte[1920 * 1080 * 4], pixels = new byte[Width * Height * 3];
                ushort[] depths = new ushort[512 * 424];
                Body[] bodies = new Body[sensor.BodyFrameSource.BodyCount];
                double lastColor = -1, lastBody = -1, lastDepth = -1, lastHeartbeat = -1, lastArrival = Qpc();
                long colorCount = 0, depthCount = 0, bodyCount = 0;
                object tracked = new object[0], floor = null;
                double validRatio = 0;
                sensor.Open();
                while (true)
                {
                    if (depthEnabled)
                    {
                        using (DepthFrame depth = depthReader.AcquireLatestFrame())
                        {
                            if (depth != null && depth.RelativeTime.TotalSeconds != lastDepth)
                            {
                                lastDepth = depth.RelativeTime.TotalSeconds;
                                depth.CopyFrameDataToArray(depths);
                                int valid = 0;
                                foreach (ushort value in depths)
                                    if (value >= depth.DepthMinReliableDistance && value <= depth.DepthMaxReliableDistance) valid++;
                                validRatio = (double)valid / depths.Length;
                                depthCount++;
                            }
                        }
                        using (BodyFrame body = bodyReader.AcquireLatestFrame())
                        {
                            if (body != null && body.RelativeTime.TotalSeconds != lastBody)
                            {
                                lastBody = body.RelativeTime.TotalSeconds;
                                body.GetAndRefreshBodyData(bodies);
                                tracked = Bodies(bodies, sensor.CoordinateMapper);
                                Vector4 plane = body.FloorClipPlane;
                                floor = Finite(plane.X) && Finite(plane.Y) && Finite(plane.Z)
                                    ? (object)new float[] {plane.X, plane.Y, plane.Z, plane.W} : null;
                                bodyCount++;
                            }
                        }
                    }
                    using (ColorFrame color = colorReader.AcquireLatestFrame())
                    {
                        if (color != null && color.RelativeTime.TotalSeconds != lastColor)
                        {
                            double arrived = Qpc();
                            lastColor = color.RelativeTime.TotalSeconds;
                            color.CopyConvertedFrameDataToArray(full, ColorImageFormat.Bgra);
                            for (int y = 0; y < Height; y++)
                                for (int x = 0; x < Width; x++)
                                {
                                    int source = ((y * 3 + 1) * 1920 + x * 3 + 1) * 4;
                                    int target = (y * Width + x) * 3;
                                    pixels[target] = full[source]; pixels[target+1] = full[source+1]; pixels[target+2] = full[source+2];
                                }
                            colorCount++;
                            bool fresh = depthEnabled && validRatio > .05 &&
                                Math.Abs(lastColor - lastDepth) <= .08;
                            bool bodyFresh = fresh && Math.Abs(lastColor - lastBody) <= .08;
                            ColorCameraSettings cameraSettings = color.ColorCameraSettings;
                            Send(writer, Fields("width",Width,"height",Height,"qpc",arrived,
                                "color_frame_interval_ms",cameraSettings.FrameInterval.TotalMilliseconds,
                                "color_exposure_ms",cameraSettings.ExposureTime.TotalMilliseconds,
                                "color_gain",cameraSettings.Gain,
                                "sensor_seconds",lastColor,"device_id","kinect2:"+sensor.UniqueKinectId,
                                "depth_valid",fresh,"depth_ratio",validRatio,"bodies",bodyFresh ? tracked : new object[0],
                                "floor",bodyFresh ? floor : null,"color_frames",colorCount,
                                "depth_frames",depthCount,"body_frames",bodyCount), pixels);
                            lastArrival = arrived;
                            lastHeartbeat = arrived;
                        }
                    }
                    double now = Qpc();
                    if (now - lastHeartbeat >= 1)
                    {
                        Send(writer, Fields("qpc",now,"available",sensor.IsAvailable,"gap_s",now-lastArrival,
                            "color_frames",colorCount,"depth_frames",depthCount,"body_frames",bodyCount), null);
                        lastHeartbeat = now;
                    }
                    Thread.Sleep(1);
                }
            }
            }
            finally { sensor.Close(); }
        }
        catch (IOException) { return 0; } // Parent closed its pipe.
        catch (Exception ex) { Console.Error.WriteLine(ex.Message); return 1; }
    }
}
