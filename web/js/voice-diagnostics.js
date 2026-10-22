// 把采音状态、识别结果和执行拒绝分别说明。
export function voiceDiagnosticText(s){
  const reasons={'output disabled':'控制输出已暂停','no mapping':'没有对应映射','unknown command':'没有匹配到当前口令','not connected':'输入来源未连接','input overflow':'麦克风输入发生间断'};
  const reason=reasons[s.last_error]||s.last_error||'无';
  const mode={vosk_constrained_grammar:'中文命令识别（限定词表）',vosk:'中文语音识别',windows:'Windows系统语音'}[s.recognizer_mode]||'当前语音方案';
  const text=s.last_final||s.final||s.last_command||'—';
  const value=(v,unit='')=>v==null?'—':`${v}${unit}`;
  return [`模式：${mode} · 可用词条 ${value(s.supported_count)} 条`,
    `采音来源：${s.source_kind==='computer'?'电脑麦克风':s.source_kind==='phone'?'手机':'未连接'} · ${s.audio_device_name||'未指定设备'}`,
    `连接：${s.connected?'已连接':'未连接'} · 音频：${s.audio_ready?'已准备':'未准备'} · 采音 ${s.audio_alive||s.stream_alive?'运行中':'空闲'}`,
    `当前音量：${value(s.rms==null?null:Math.round(s.rms))} · 已接收 ${value(s.bytes_received)} 字节`,
    `待处理音频：${value(s.audio_queue_depth)} 块 · 丢弃 ${value(s.audio_dropped_frames)} 帧 · 流重置 ${value(s.audio_stream_resets)} 次`,
    `实时识别：${s.last_partial||s.partial||'—'} · 最后完成：${text}`,
    `已识别命令：${value(s.commands_heard)} 次 · 本次电脑执行：${s.last_executed===true?'已执行':s.last_executed===false?'未执行':'未确认'}`,
    `${s.last_executed===false?'未执行原因':'运行提示'}：${reason}`,
    `唤醒词：${s.wake_word||'无需唤醒词'} · 不支持的词条 ${s.unsupported?.length??0} 条 · 冲突 ${s.phrase_conflicts?.length??0} 条`,
  ].join('\n');
}
