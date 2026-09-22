export const VIEW_CONTROL_CONTENT = {
  horizontal: [
    {value:'roll_tilt',label:'头部侧倾',description:'脸朝屏幕，头向左肩或右肩倾斜；保持倾斜持续左右转，回正停止。只控制左右。'},
    {value:'head_turn',label:'左右转头',description:'向左或右转动头部控制水平视角，只控制左右。不同转头方案可在高级设置中选择。'},
    {value:'right',label:'右手握拳',description:'右手握拳后移动手控制左右，松开停止。'},
    {value:'left',label:'左手握拳',description:'左手握拳后移动手控制左右，松开停止。'},
    {value:'off',label:'关闭',description:'关闭左右视角控制。'},
  ],
  vertical: [
    {value:'left',label:'左手握拳',description:'左手握拳后移动手控制上下，松开停止。'},
    {value:'right',label:'右手握拳',description:'右手握拳后移动手控制上下，松开停止。'},
    {value:'off',label:'关闭',description:'关闭上下视角控制。'},
    {value:'legacy',label:'原有上下方案',description:'当前使用更多设置中的上下方案，左手放入绿色区域后开启上下控制。'},
  ],
  legacyVerticalNote:'绿色区域或抬低头方案已在更多设置中启用；它与握拳上下是不同方案。',
  guide: {
    title:'操作指导',
    intro:'按下面几步开始使用；以后可随时从页面顶部打开这份指导。',
    steps:[
      {title:'连接设备',text:'到“通用设置”选择摄像头来源，再点“连接并开始识别”。'},
      {title:'正面校准',text:'脸朝屏幕、自然站好，在“开始”页点“站好并校准”。'},
      {title:'使用默认视角',text:'默认用头部侧倾控制左右，用左手握拳后移动手控制上下；松开手，上下立即停止。'},
      {title:'切换方案',text:'在“视角控制”中分别选择左右和上下。可用同一只手全向控制，也可让左右手分轴控制。'},
      {title:'开始游戏控制',text:'确认画面和设置后，点页面顶部的“开始游戏控制”。'},
      {title:'随时停止',text:'按 F9 或点击顶部“紧急停止”立即停止游戏控制。'},
    ],
  },
};
