// 统一设备入口：目录描述入口，连接、扫描和存储复用现有组件。
import {$,api,setText} from './core.js';
import {showSettingsPane,showView} from './shell.js';

let types=[],loading=false;
const states=new Map();
export function renderPeripheralState(id,text,values={}){
  states.set(id,{text,values});
  const card=document.querySelector(`[data-peripheral="${id}"]`);if(!card)return;
  const status=card.querySelector('[data-peripheral-status]');if(status)setText(status,text);
  for(const [key,value] of Object.entries(values)){
    const node=card.querySelector(`[data-peripheral-value="${key}"]`);if(node)setText(node,value);
  }
}
export function openPeripheral(id){
  const item=types.find(type=>type.id===id);if(!item)return;
  showView('devices');showSettingsPane('devices');
  const target=document.getElementById(item.target);if(!target)return;
  target.hidden=false;
  if(target.tagName==='DETAILS')target.open=true;
  (target.closest('.peripheral-card')||target).scrollIntoView({block:'start',behavior:'instant'});
  const focus=target.querySelector('select,input,button')||target;
  if(!focus.hasAttribute('tabindex')&&!focus.matches('select,input,button'))focus.tabIndex=-1;
  focus.focus({preventScroll:true});
}
function selection(){
  const type=types.find(item=>item.id===$('#addPeripheralType').value);
  setText($('#addPeripheralGuide'),type?.guide||'');
  setText($('#addPeripheralContinue'),type?.action||'继续');
  $('#addPeripheralContinue').disabled=!type;
  $('#addPeripheralBluetooth').hidden=type?.id!=='gunmouse';
}
export async function refreshPeripheralCatalog(){
  if(loading)return false;loading=true;
  try{
    const data=await api('/api/peripherals/catalog');
    if(!Array.isArray(data.types))throw new Error('未读到设备目录');
    types=data.types;
    const list=$('#peripheralList'),select=$('#addPeripheralType'),selected=select.value;
    select.replaceChildren();
    let index=0;
    for(const type of types){
      const target=document.getElementById(type.target);
      if(target){
        const card=target.closest('.peripheral-card')||target;
        card.dataset.peripheral=type.id;
        const state=states.get(type.id);
        renderPeripheralState(type.id,state?.text||type.description,state?.values);
        if(list.children[index]!==card)list.insertBefore(card,list.children[index]||null);
        index++;
      }
      select.add(new Option(type.name,type.id));
    }
    if(types.some(type=>type.id===selected))select.value=selected;
    selection();setText($('#peripheralCatalogStatus'),'');return true;
  }catch(error){setText($('#peripheralCatalogStatus'),error.message);return false}
  finally{loading=false}
}
$('#addPeripheralBtn').addEventListener('click',async()=>{
  $('#addPeripheralStatus').textContent='';$('#addPeripheralDialog').showModal();
  if(!types.length){$('#addPeripheralContinue').disabled=true;await refreshPeripheralCatalog()}
  selection();
});
$('#microphoneVoiceBtn').addEventListener('click',()=>{showView('devices');showSettingsPane('voice')});
for(const [button,target] of [['cameraScanBtn','cameraSettings'],['audioScanBtn','microphoneSettings']]){
  $('#'+button).addEventListener('click',()=>{$('#'+target).open=true});
}
$('#addPeripheralClose').addEventListener('click',()=>$('#addPeripheralDialog').close());
$('#addPeripheralType').addEventListener('change',selection);
$('#addPeripheralContinue').addEventListener('click',()=>{
  const id=$('#addPeripheralType').value;
  const type=types.find(item=>item.id===id);
  $('#addPeripheralDialog').close();openPeripheral(id);
  if(type?.add_control)document.getElementById(type.add_control)?.click();
});
