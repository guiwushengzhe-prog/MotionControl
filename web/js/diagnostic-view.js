// 复用原有诊断数据，用名称和值分列；中文沿用界面字体，数字保持等宽对齐。
import {setText} from './core.js';

export function renderDiagnosticLines(target,lines){
  if(!target)return;
  const content=lines.filter(Boolean),signature=JSON.stringify(content);
  if(target.dataset.diagnosticContent===signature)return;
  target.dataset.diagnosticContent=signature;
  target.classList.add('diag-readout');
  const nodes=[];
  for(const line of content){
    let last=null;
    for(const part of String(line).split(' · ')){
      const at=part.indexOf('：');
      if(at>0){
        const field=document.createElement('dl'),label=document.createElement('dt'),value=document.createElement('dd');
        field.className='diag-field';label.textContent=part.slice(0,at+1);value.textContent=part.slice(at+1);
        if(part.length>75)field.classList.add('wide');
        field.append(label,value);nodes.push(field);last=value;
      }else if(last){last.append(document.createTextNode(' · '+part))}
      else{const note=document.createElement('p');note.className='diag-note';note.textContent=part;nodes.push(note)}
    }
  }
  target.replaceChildren(...nodes);
}

export function renderDiagnosticMetrics(target,items){
  if(!target)return;
  if(target.children.length!==items.length){
    target.replaceChildren(...items.map(()=>{
      const item=document.createElement('div'),label=document.createElement('span'),value=document.createElement('strong');
      item.append(label,value);return item;
    }));
  }
  items.forEach(([label,value],index)=>{
    setText(target.children[index].firstChild,label);setText(target.children[index].lastChild,value);
  });
}
