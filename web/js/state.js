// 几个功能模块都要改的状态放在这里：S.xxx。只有一个模块改的，放在那个模块里。

export const S = {
  audioSource: 'computer',
  audioMode: 'waiting',
  cameraIndex: 0,
  outputEpoch: 0,
  inputEpoch: 0,
  headDirty: false,
  desiredSource: null,
  customPoseScores: {},
};

// 最近一次扫描摄像头的结果。新手教学要按它判断「这台电脑现在能开什么」。

export const cameraScan={state:'idle',count:0,error:''};

export const output={enabled:false,mode:'mouse',strength:160,server:null,xinputEnabled:false,xinputMotionLeft:false,xinputUser:null,xinputStatus:null};

export const head={algorithm:'pnp',horizontalAlgorithm:'roll_tilt',deadzone:.10,sensitivityX:58,sensitivityY:46,enabled:true,invertY:false,verticalLookEnabled:false,verticalExclusive:false,bodyMotionGuard:false};

export const gameProfile={catalog:[],selected:null,actions:{},overrides:{}};

export const profileDirty=new Set();
