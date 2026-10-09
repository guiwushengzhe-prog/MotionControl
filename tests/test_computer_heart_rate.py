"""标准心率协议与读取来源生命周期；使用合成蓝牙设备，不扫描真实手环。"""
import time

from motioncontrol.fitness import HR_FRESH_S
from test_fitness import Clock, factory  # noqa: F401
from test_fitness_studio_http import app_http, browser  # noqa: F401


def test_input_ownership_preview_pause_and_persistence(factory):
    clock = Clock()
    store = factory(clock)
    store.set_heart_rate_input("computer", "page-a")
    assert store.heart_rate({"bpm": 120}, source="computer", reader_id="page-a")
    assert store.state()["heart_rate"] == 120
    assert store.state().get("hr_samples", 0) == 0
    store.control({"action": "start"})
    assert store.heart_rate({"bpm": 130}, source="computer", reader_id="page-a")
    assert not store.heart_rate({"bpm": 190})
    assert not store.heart_rate({"bpm": 180}, source="computer", reader_id="page-b")
    store.control({"action": "pause"})
    store.heart_rate({"bpm": 140}, source="computer", reader_id="page-a")
    assert store.state()["heart_rate"] == 140
    assert store.state()["hr_avg"] == 130 and store.state()["hr_samples"] == 1
    store.set_heart_rate_input("computer", "page-b")
    store.heart_rate({"bpm": 145}, source="computer", reader_id="page-b")
    assert not store.clear_computer_heart_rate("page-a", release=True)
    assert store.state()["heart_rate"] == 145
    clock.advance(HR_FRESH_S + .1)
    assert store.state()["heart_rate"] is None
    store.close()
    restored = factory(clock)
    assert restored.state()["heart_rate_source"] == "computer"
    assert restored.state()["heart_rate_reader"] == ""
    assert restored.state()["heart_rate"] is None
    assert restored.state()["heart_rate_instance"] != store.state()["heart_rate_instance"]
    restored.set_heart_rate_input("phone")
    assert restored.heart_rate({"bpm": 110})
    assert not restored.clear_computer_heart_rate("page-b", release=True)
    assert restored.state()["heart_rate"] == 110


FAKE_BLUETOOTH = """(() => {
  const characteristic=new EventTarget();
  characteristic.startNotifications=async()=>{
    if(window.holdHeartStart)await new Promise(resolve=>window.finishHeartStart=resolve);
    return characteristic;
  };
  const device=new EventTarget();device.name='合成心率手环';
  device.gatt={connected:false,connect:async()=>{
    device.gatt.connected=true;return device.gatt;
  },getPrimaryService:async name=>{window.heartService=name;return {
    getCharacteristic:async name=>{window.heartCharacteristic=name;return characteristic}
  }},disconnect:()=>{device.gatt.connected=false;device.dispatchEvent(new Event('gattserverdisconnected'))}};
  window.heartDevice=device;
  window.emitHeart=bytes=>{
    characteristic.value=new DataView(new Uint8Array(bytes).buffer);
    characteristic.dispatchEvent(new Event('characteristicvaluechanged'));
  };
  Object.defineProperty(navigator,'bluetooth',{value:{requestDevice:async options=>{
    window.heartRequest=options;return device;
  }}});
  localStorage.setItem('motioncontrol_tutorial_v2',JSON.stringify({seen:1,offered:true}));
})();"""


def open_fitness(browser, base, *, hold=False):
    page = browser.new_page()
    page.add_init_script(FAKE_BLUETOOTH)
    if hold:
        page.add_init_script("window.holdHeartStart=true")
    page.goto(base + "/")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="fitness"]')
    return page


def test_browser_direct_heart_rate_lifecycle_and_standard_packets(browser, app_http):
    app, base, _ = app_http
    page = open_fitness(browser, base)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    assert page.evaluate("window.heartRequest===undefined")  # 打开页面不扫描设备。
    parsed = page.evaluate("""async()=>{
      const {parseHeartRate}=await import('/js/heart-rate.js');
      return [[0,112],[1,130,0],[1,130],[4,110],[6,110],[0,0],[0,251]].map(
        bytes=>parseHeartRate(new DataView(new Uint8Array(bytes).buffer)));
    }""")
    assert parsed == [112, 130, None, None, 110, None, None]
    page.click("#fitnessHeartConnect")
    page.wait_for_function("fitnessHeartConnection.textContent.includes('电脑蓝牙 · 合成心率手环')")
    assert page.evaluate("heartRequest") == {"filters": [{"services": ["heart_rate"]}]}
    assert page.evaluate("[heartService,heartCharacteristic]") == ["heart_rate", "heart_rate_measurement"]
    page.evaluate("emitHeart([0,112])")
    page.wait_for_function("fitnessHeartNow.textContent==='112'")
    assert app.FITNESS.state().get("hr_samples", 0) == 0
    app.OUTPUT.set_config(enabled=True)
    page.evaluate("emitHeart([1,130,0])")
    page.wait_for_function("fitnessHeartAvg.textContent==='130'")
    assert not app.FITNESS.heart_rate({"bpm": 190})
    app.OUTPUT.set_config(enabled=False)
    page.evaluate("emitHeart([0,140])")
    page.wait_for_function("fitnessHeartNow.textContent==='140'")
    assert page.locator("#fitnessHeartAvg").inner_text() == "130"
    page.evaluate("emitHeart([4,140])")  # 有接触检测，但未佩戴。
    page.wait_for_function("fitnessHeartNow.textContent==='—'")
    page.evaluate("heartDevice.gatt.disconnect()")
    page.wait_for_function("!fitnessHeartConnect.hidden")
    page.wait_for_function("async()=>!(await (await fetch('/api/fitness/state')).json()).heart_rate_reader")
    assert page.locator("#fitnessHeartAvg").inner_text() == "130"
    page.click("#fitnessPhoneConnect")
    page.wait_for_function("!phoneConnectPanel.hidden")
    assert app.FITNESS.state()["heart_rate_source"] == "phone"
    assert not app.RUNTIME.camera.running
    assert errors == []
    page.close()


def test_cancel_pending_connection_and_close_page_release_only_owned_reader(browser, app_http):
    app, base, _ = app_http
    page = open_fitness(browser, base, hold=True)
    page.click("#fitnessHeartConnect")
    page.wait_for_function("typeof finishHeartStart==='function'")
    assert app.FITNESS.state()["heart_rate_reader"]
    page.click("#fitnessHeartDisconnect")
    page.evaluate("finishHeartStart()")
    page.wait_for_function("!heartDevice.gatt.connected")
    page.wait_for_function("async()=>!(await (await fetch('/api/fitness/state')).json()).heart_rate_reader")
    page.evaluate("window.holdHeartStart=false")
    page.click("#fitnessHeartConnect")
    page.wait_for_function("fitnessHeartConnection.textContent.includes('电脑蓝牙 · 合成心率手环')")
    page.goto("about:blank")
    deadline = time.monotonic() + 3
    while app.FITNESS.state()["heart_rate_reader"] and time.monotonic() < deadline:
        time.sleep(.05)
    assert app.FITNESS.state()["heart_rate_reader"] == ""
    page.close()


def test_hidden_page_stops_reader_when_backend_is_unreachable(browser, app_http):
    app, base, _ = app_http
    page = open_fitness(browser, base)
    page.click("#fitnessHeartConnect")
    page.wait_for_function("fitnessHeartConnection.textContent.includes('电脑蓝牙 · 合成心率手环')")
    page.click('#settingsNav [data-pane="voice"]')
    page.route("**/api/fitness/state", lambda route: route.abort())
    page.wait_for_function("!heartDevice.gatt.connected", timeout=10000)
    assert "后台连接已断开" in page.locator("#fitnessHeartConnection").text_content()
    page.close()
