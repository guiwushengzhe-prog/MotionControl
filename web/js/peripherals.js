// 统一设备入口：目录描述入口，连接、扫描和存储复用现有组件。
import {$,api,setText} from './core.js';
import {showSettingsPane,showView} from './shell.js';

let types=[],loading=false;
const states=new Map();
export function renderPeripheralState(id,text){
  states.set(id,text);
  const node=document.querySelector(`[data-peripheral-status="${id}"]`);if(node)setText(node,text);
}
export function openPeripheral(id){
  const item=types.find(type=>type.id===id);if(!item)return;
  showView('devices');showSettingsPane('devices');
  const target=document.getElementById(item.target);if(!target)return;
  target.hidden=false;
  if(target.tagName==='DETAILS')target.open=true;
  target.scrollIntoView({block:'start',behavior:'instant'});
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
    list.replaceChildren();select.replaceChildren();
    for(const type of types){
      const row=document.createElement('div');row.className='row';
      const main=document.createElement('div');main.className='row-main';
      const name=document.createElement('span');name.className='name';name.textContent=type.name;
      const status=document.createElement('span');status.className='sub';status.dataset.peripheralStatus=type.id;
      status.textContent=states.get(type.id)||type.description;
      main.append(name,status);
      const button=document.createElement('button');button.className='btn';button.type='button';button.textContent='管理';
      button.dataset.peripheral=type.id;button.addEventListener('click',()=>openPeripheral(type.id));
      row.append(main,button);list.append(row);select.add(new Option(type.name,type.id));
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
$('#addPeripheralClose').addEventListener('click',()=>$('#addPeripheralDialog').close());
$('#addPeripheralType').addEventListener('change',selection);
$('#addPeripheralContinue').addEventListener('click',()=>{
  const id=$('#addPeripheralType').value;
  const type=types.find(item=>item.id===id);
  $('#addPeripheralDialog').close();openPeripheral(id);
  if(type?.add_control)document.getElementById(type.add_control)?.click();
});
