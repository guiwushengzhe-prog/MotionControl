// 免鼠标导航：每次只走一格，偏转幅度决定重复间隔，回正停留确认。
export class TuningNavigation{
  constructor(){this.reset()}
  reset(){this.armed=false;this.centerAt=null;this.moved=false;this.direction=0;this.lastAt=0;this.tier=0}
  step(angle,now,{valid=true}={}){
    if(!valid||!Number.isFinite(angle)){this.reset();return {tier:0,waiting:true}}
    const absolute=Math.abs(angle);
    if(absolute<=4){
      this.direction=0;this.tier=0;
      if(this.centerAt===null)this.centerAt=now;
      if(!this.armed&&now-this.centerAt>=500)this.armed=true;
      if(this.armed&&this.moved&&now-this.centerAt>=900){this.reset();return {tier:0,confirm:true}}
      return {tier:0,progress:this.moved?Math.min(1,(now-this.centerAt)/900):0,waiting:!this.armed};
    }
    this.centerAt=null;
    if(!this.armed||absolute<8){this.tier=0;return {tier:0,waiting:!this.armed}}
    const direction=Math.sign(angle),tier=absolute>=24?3:absolute>=15?2:1;
    // 换方向先走一格；持续偏转按慢/中/快三档逐格重复，卡帧不补跳多格。
    const changed=direction!==this.direction;
    this.direction=direction;this.tier=tier;this.moved=true;
    if(changed||now-this.lastAt>=[0,700,350,160][tier]){this.lastAt=now;return {delta:direction,tier}}
    return {tier};
  }
}
