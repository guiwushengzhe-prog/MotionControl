export const VIEW_CONTROL_CONTENT = {
  horizontal: [
    {value:'roll_tilt',label:'头部侧倾',description:'脸朝屏幕，头向左肩或右肩倾斜；保持倾斜持续左右转，回正停止。只控制左右。'},
    {value:'head_responsive',label:'侧倾＋转脸（新版灵敏）',description:'侧倾或左右转脸都能转向，快速换边直接反向。回正范围更宽；靠近中心慢转，大幅动作快转。'},
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
};
