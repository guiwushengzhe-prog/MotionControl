"""设备类型目录。连接和配置仍由各自的既有组件负责。"""
import copy

DEVICE_TYPES = (
    {"id": "camera", "name": "摄像头", "target": "cameraSettings", "add_control": "cameraScanBtn",
     "description": "电脑摄像头、采集卡或 Kinect", "action": "查找摄像头",
     "guide": "先连接摄像头并安装它需要的驱动，再查找并选择设备。"},
    {"id": "gunmouse", "name": "枪型飞鼠", "target": "gunmouseSettings", "add_control": "gunmouseScanBtn",
     "description": "鼠标体感、触摸、摇杆与甩拳", "action": "查找已配对飞鼠",
     "guide": "先在 Windows 蓝牙设置中配对 GunMouse SuperMini，再在这里选择设备并连接管理。"},
    {"id": "phone", "name": "手机", "target": "phoneGroup", "add_control": "phoneConnectBtn",
     "description": "手机摄像头、麦克风和手持输入", "action": "扫码连接",
     "guide": "手机打开 MotionControl，扫描连接二维码。"},
    {"id": "microphone", "name": "麦克风", "target": "microphoneSettings", "add_control": "audioScanBtn",
     "description": "电脑麦克风或外接音频设备", "action": "查找麦克风",
     "guide": "先连接麦克风，再查找并选择输入设备。"},
)


def device_catalog():
    return {"types": copy.deepcopy(DEVICE_TYPES)}
