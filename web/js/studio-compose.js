// 预览、录像与直播人物来源共用裁切和等比缩放；人物镜像已由共享处理帧完成。
export const FULL_CROP={x:0,y:0,width:1,height:1};

export function sourceSize(image,crop=FULL_CROP){
  return [(image.videoWidth||image.width)*crop.width,(image.videoHeight||image.height)*crop.height];
}

export function containGeometry(image,width,height,crop=FULL_CROP,zoom=100,pan={x:0,y:0}){
  const factor=Math.max(1,Math.min(4,(Number(zoom)||100)/100));
  const [sw,sh]=sourceSize(image,crop),scale=Math.min(width/sw,height/sh)*factor,dw=sw*scale,dh=sh*scale;
  const overflowX=Math.max(0,(dw-width)/2),overflowY=Math.max(0,(dh-height)/2);
  const clamp=value=>Math.max(-1,Math.min(1,Number(value)||0));
  return {dw,dh,overflowX,overflowY,dx:(width-dw)/2+clamp(pan.x)*overflowX,dy:(height-dh)/2+clamp(pan.y)*overflowY};
}

export function portraitViewport(image,width,height,config,hasScreen){
  if(!hasScreen)return {x:0,y:0,width,height};
  const [sw,sh]=sourceSize(image,config.portrait_crop||FULL_CROP);
  const w=width*Math.max(.15,Math.min(.65,Number(config.size)/100)),h=Math.min(height*.75,w*sh/sw);
  const inset=Math.round(Math.min(width,height)*.025),left=config.position.endsWith('left'),top=config.position.startsWith('top');
  return {x:left?inset:width-w-inset,y:top?inset:height-h-inset,width:w,height:h};
}

export function contained(ctx,image,x,y,width,height,crop=FULL_CROP,zoom=100,pan={x:0,y:0}){
  const iw=image.videoWidth||image.width,ih=image.videoHeight||image.height;
  if(!iw||!ih)return;
  const [sw,sh]=sourceSize(image,crop),{dx,dy,dw,dh}=containGeometry(image,width,height,crop,zoom,pan);
  ctx.save();ctx.beginPath();ctx.rect(x,y,width,height);ctx.clip();
  ctx.drawImage(image,iw*crop.x,ih*crop.y,sw,sh,x+dx,y+dy,dw,dh);
  ctx.restore();
}

export function compose(ctx,screen,portrait,width,height,config){
  ctx.fillStyle='#101014';ctx.fillRect(0,0,width,height);
  if(screen)contained(ctx,screen,0,0,width,height,config.screen_crop||FULL_CROP);
  if(!portrait)return;
  const crop=config.portrait_crop||FULL_CROP,view=portraitViewport(portrait,width,height,config,!!screen);
  contained(ctx,portrait,view.x,view.y,view.width,view.height,crop,config.portrait_zoom,config.portrait_pan);
}
