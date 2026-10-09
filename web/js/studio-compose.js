// 预览、录像与直播人物来源共用裁切和等比缩放；人物镜像已由共享处理帧完成。
export const FULL_CROP={x:0,y:0,width:1,height:1};

export function sourceSize(image,crop=FULL_CROP){
  return [(image.videoWidth||image.width)*crop.width,(image.videoHeight||image.height)*crop.height];
}

export function contained(ctx,image,x,y,width,height,crop=FULL_CROP,zoom=100){
  const iw=image.videoWidth||image.width,ih=image.videoHeight||image.height;
  if(!iw||!ih)return;
  const factor=Math.max(1,Math.min(4,(Number(zoom)||100)/100));
  const [sw,sh]=sourceSize(image,crop),scale=Math.min(width/sw,height/sh)*factor,dw=sw*scale,dh=sh*scale;
  ctx.save();ctx.beginPath();ctx.rect(x,y,width,height);ctx.clip();
  ctx.drawImage(image,iw*crop.x,ih*crop.y,sw,sh,x+(width-dw)/2,y+(height-dh)/2,dw,dh);
  ctx.restore();
}

export function compose(ctx,screen,portrait,width,height,config){
  ctx.fillStyle='#101014';ctx.fillRect(0,0,width,height);
  if(screen)contained(ctx,screen,0,0,width,height,config.screen_crop||FULL_CROP);
  if(!portrait)return;
  const crop=config.portrait_crop||FULL_CROP;
  if(!screen){contained(ctx,portrait,0,0,width,height,crop,config.portrait_zoom);return}
  const [sw,sh]=sourceSize(portrait,crop);
  const w=width*Math.max(.15,Math.min(.65,Number(config.size)/100)),h=Math.min(height*.75,w*sh/sw);
  const inset=Math.round(Math.min(width,height)*.025),left=config.position.endsWith('left'),top=config.position.startsWith('top');
  contained(ctx,portrait,left?inset:width-w-inset,top?inset:height-h-inset,w,h,crop,config.portrait_zoom);
}
