// 把现有状态串成可读的处理链，不增加识别、录制或独立状态轮询。
import {$,setText} from './core.js';

const number=(v,unit='',digits=1)=>v!=null&&Number.isFinite(Number(v))?`${Number(v).toFixed(digits)}${unit}`:'—';
const yes=(v,on='是',off='否')=>v==null?'—':v?on:off;
const method={roll_tilt:'头部侧倾',head_responsive:'侧倾与转脸',gesture_v188:'骨骼转头',hfhpe:'模型转头·原规则',hfhpe_simple:'模型转头·简化回正',off:'关闭',left:'左手握拳',right:'右手握拳',head_pitch:'抬头低头'};
const source={model:'模型骨骼',depth:'Kinect深度骨骼',computer:'电脑摄像头',computer_camera:'电脑摄像头',phone:'手机摄像头',mixed:'分别选择'};
const state={IDLE:'静止',NEUTRAL:'中心区',ACTIVE:'正在输出',MOVING:'正在移动',RETURNING:'正在回正',ENGAGED:'已接管',DISENGAGED:'未接管'};
const motion={march:'踏步',back_leg_raise:'小腿后抬',jump:'跳跃',squat:'下蹲',lean_left:'身体左倾',lean_right:'身体右倾',lean_back:'身体后倾',lean_forward:'身体前倾'};
const handState={idle:'待机',disabled:'已关闭',lost:'手部丢失',open:'张手',engaged:'握拳接管',moving:'移动中'};
const gripSource={kinect:'Kinect原厂张合',hand:'手指关节',pose:'人体指尖',none:'无有效数据'};

function write(id,lines){setText($('#'+id),lines.filter(Boolean).join('\n'))}

export function renderRuntimeDiagnostics(p,k={},o={},input={}){
  const h=k.head||{},hands=h.hand_mouse||{},axes=hands.axes||{},exp=k.sensitivity_experiments||{},body=exp.body||{};
  write('diagCapture',[
    `来源：${source[input.body_mode]||source[p.source]||'未确定'} · 状态：${yes(p.running,'运行中','已停止')} · 采集方式：${p.backend_name||'—'}`,
    `画面：${p.camera_resolution?.width||'—'}×${p.camera_resolution?.height||'—'} · 格式：${p.fourcc||'—'} · 请求 ${number(p.requested_fps,'帧/秒')} / 实际 ${number(p.actual_capture_fps,'帧/秒')}`,
    `采集 ${p.captured_frames??'—'} 帧 · 处理 ${p.processed_frames??'—'} 帧 · 跳过 ${p.skipped_frames??'—'} 帧 · 骨骼年龄 ${number(p.pose_frame_age_ms,'毫秒')}`,
    `处理耗时95%不超过：${number(p.inference_p95_ms,'毫秒')} · 当前画面到结果：${number(p.total_latency_ms,'毫秒')}`,
    `预览 ${number(p.preview_fps,'帧/秒')} · 编码平均 ${number(p.preview_encode_avg_ms,'毫秒')} / 95% ${number(p.preview_encode_p95_ms,'毫秒')} · 画面年龄 ${number(p.preview_last_age_ms,'毫秒')}`,
    `预览平均体积 ${number(p.preview_jpeg_avg_bytes==null?null:p.preview_jpeg_avg_bytes/1024,'KB')} · 当前人体 ${p.recent_humans??'—'} 人`,
    p.last_error?`采集错误：${p.last_error}`:'',
  ]);
  write('diagHead',[
    `左右方式：${method[h.horizontal_algorithm]||'未确定'} · 开启：${yes(h.enabled)} · 已校准：${yes(h.horizontal_calibrated??h.calibrated)}`,
    `当前允许控制：${yes(h.horizontal_control_ready)} · 原因：${h.horizontal_block_message||h.estimate_error||'—'}`,
    `左右角：${number(h.hfhpe_yaw_deg??h.raw_yaw,h.raw_yaw_units==='ratio'?'比例':'°',2)} · 侧倾：${number(h.raw_tilt_deg,'°',2)} · 俯仰：${number(h.hfhpe_pitch_deg??h.raw_pitch,h.raw_pitch_units==='ratio'?'比例':'°',2)}`,
    `设定死区：${number(h.hfhpe_requested_deadzone_deg??(h.deadzone==null?null:h.deadzone*18),'°')} · 实际死区：${number(h.hfhpe_deadzone_deg??h.tilt_deadzone_deg,'°')} · 动作防晃倍率 ${number(h.motion_deadzone_scale,'倍',2)}`,
    `左右输出：${number(h.output_x==null?null:h.output_x*100,'%')} · 上下输出：${number(h.output_y==null?null:h.output_y*100,'%')} · 阶段：${state[h.yaw_intent_state]||state[h.tilt_state]||'—'}`,
    `头姿模型：${({vitt128:'轻量128像素',vitt64:'轻量64像素',hgnet64:'卷积64像素'})[p.hfhpe?.model]||'未使用'} · 本帧模型处理 ${number(p.hfhpe?.inference_ms,'毫秒')} · ${p.hfhpe?.message||({ready:'已就绪',inactive:'未启用',loading:'加载中'})[p.hfhpe?.state]||'—'}`,
    `上下视角门：${yes(k.vertical_gate_active,'已进入','未进入')} · 上下独占：${yes(k.vertical_look?.exclusive_axes)}`,
  ]);
  write('diagHands',[
    `手部识别：${({idle:'按需待机',loading:'加载中',ready:'已就绪',native:'使用Kinect原厂张合',error:'未就绪'})[p.hand_model_state]||'未确定'} · ${p.hand_model_error||'无模型错误'}`,
    `调度：${p.hand_scheduling==='parallel_same_frame'?'同帧并行':'同帧顺序'} · 独立平均 ${number(p.hand_processing_avg_ms,'毫秒')} · 并行比例 ${number(p.hand_parallel_percent,'%')} · 裁切重试 ${p.hand_crop_retries??'—'} 次`,
    ...['horizontal','vertical'].map(axis=>{const a=axes[axis]||{};return `${axis==='horizontal'?'左右':'上下'}：${method[a.hand]||method[a.source]||'未分配'} · ${yes(a.engaged,'握拳已接管','未接管')} · 输出 ${number(a.output==null?null:a.output*100,'%')} · ${handState[a.state]||a.reason||a.message||'—'} · ${gripSource[a.grip_source]||'—'} · 手指展开 ${number(a.spread,'',3)} / 弯曲 ${number(a.curl,'',3)}`}),
  ]);
  const raw=k.motions||[],active=(Array.isArray(raw)?raw:Object.entries(raw).filter(([,v])=>v===true||v?.active===true).map(([name])=>name)).map(name=>motion[name]||'自定义动作');
  const zones=Object.entries(k.zones||{}).filter(([name,z])=>!['leftHandUpper','leftHandLower','rightHandUpper','rightHandLower'].includes(name)&&z.mapped!==false&&z.rect);
  write('diagActions',[
    `身体数据年龄：${number(k.pose_age_ms,'毫秒')} · 脚是否入镜：${yes(k.feet_in_view)} · 当前动作：${active.join('、')||'无持续动作'}`,
    `踏步：${({legacy:'稳健',responsive:'灵敏'})[k.march_algorithm]||'未确定'} · 首步来源：${source[exp.first_step_source]||'—'} · ${exp.first_step_message||''}`,
    `已映射区域：${zones.length} 个 · 当前点亮 ${zones.filter(([,z])=>z.pressed||z.recognized).length} 个 · 触发方式 ${k.zone_trigger_mode==='simple'?'灵敏':'智能'} · 固定区域 ${yes(k.zones_frozen)}`,
    `侧倾来源：${source[body.side_source]||'—'} · 前后倾来源：${source[body.fore_aft_source]||'—'} · ${body.active?.map(x=>motion[x]||x).join('、')||'未触发走位'}`,
    ...['model','depth'].map(key=>{const s=body.sources?.[key]||{};return `${source[key]}：侧倾 ${number(s.side_deg,'°')} · 前后倾 ${number(s.back_deg,'°')} · ${s.reason||'—'}`}),
    `学习：${yes(body.learning?.enabled,'开启','关闭')} · 已采 ${body.learning?.accepted_samples??0} 对 · 验证 ${body.learning?.validation_samples??0} 对 · ${body.learning?.reason||'—'}`,
  ]);
  const os=o.server||o;
  write('diagOutput',[
    `控制输出：${yes(o.enabled??os.enabled,'开启','暂停')} · 输出方式：${(o.mode||os.mode)==='gamepad'?'Xbox右摇杆':'鼠标'} · 虚拟手柄：${yes(os.gamepad_connected,'已连接','未连接')}`,
    `实际接收量：左右 ${number(os.last_x,'',3)} · 上下 ${number(os.last_y,'',3)} · 这是系统输出状态，不是游戏画面角度`,
    `键盘按住：${os.keyboard_holds?.join('、')||'无'} · 鼠标按住：${os.mouse_button_holds?.join('、')||'无'} · 手柄按钮：${os.buttons?.join('、')||'无'}`,
    `手柄合流：${yes(os.xinput_merge_active,'已接管','未接管')} · 体感左摇杆：${yes(os.xinput_motion_left_enabled)} · 执行中的宏 ${os.macros_running?.length??0} 个`,
    `最近触发：${k.recent_triggers?.at(-1)?.label||k.recent_triggers?.at(-1)?.name||'—'} · 输出错误：${os.last_error||'无'}`,
    `触发录制：${yes(k.trigger_recording?.config?.enabled,'监听中','关闭')} · 本轮保存 ${k.trigger_recording?.saved_clips??0} 段 · ${k.trigger_recording?.saving?'正在保存':'未在保存'}`,
  ]);
}
