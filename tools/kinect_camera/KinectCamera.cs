using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
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
    public static int Main(string[] args)
    {
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
            try
            {
            using (var writer = new BinaryWriter(Console.OpenStandardOutput()))
            using (var reader = sensor.OpenMultiSourceFrameReader(FrameSourceTypes.Color |
                (depthEnabled ? FrameSourceTypes.Depth | FrameSourceTypes.Body : FrameSourceTypes.None)))
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
                    var multi = reader.AcquireLatestFrame();
                    if (multi != null)
                    {
                        if (depthEnabled)
                        {
                            using (DepthFrame depth = multi.DepthFrameReference.AcquireFrame())
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
                            using (BodyFrame body = multi.BodyFrameReference.AcquireFrame())
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
                        using (ColorFrame color = multi.ColorFrameReference.AcquireFrame())
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
                                Send(writer, Fields("width",Width,"height",Height,"qpc",arrived,
                                    "sensor_seconds",lastColor,"device_id","kinect2:"+sensor.UniqueKinectId,
                                    "depth_valid",fresh,"depth_ratio",validRatio,"bodies",bodyFresh ? tracked : new object[0],
                                    "floor",bodyFresh ? floor : null,"color_frames",colorCount,
                                    "depth_frames",depthCount,"body_frames",bodyCount), pixels);
                                lastArrival = arrived;
                                lastHeartbeat = arrived;
                            }
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
