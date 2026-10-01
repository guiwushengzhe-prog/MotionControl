export const VIEW_CONTROL_CONTENT = {
  horizontal: [
    {value:'roll_tilt',label:'头部侧倾',description:'头往肩膀歪就转，回正就停'},
    {value:'head_responsive',label:'侧倾＋转脸',description:'侧倾或转脸都能转，幅度越大转得越快'},
    {value:'head_turn',label:'左右转头',description:'左右转头控制视角'},
    {value:'right',label:'右手握拳',description:'右手握拳后左右移动，松开就停'},
    {value:'left',label:'左手握拳',description:'左手握拳后左右移动，松开就停'},
    {value:'off',label:'关闭',description:'不控制左右视角'},
  ],
  vertical: [
    {value:'left',label:'左手握拳',description:'左手握拳后上下移动，松开就停'},
    {value:'right',label:'右手握拳',description:'右手握拳后上下移动，松开就停'},
    {value:'head',label:'抬头低头',description:'左手伸进画面里的绿框，再抬头低头'},
    {value:'off',label:'关闭',description:'不控制上下视角'},
  ],
};
