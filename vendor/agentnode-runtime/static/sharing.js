/* Shared-agent permissions are enforced by the gateway; these controls explain them. */
function sharedSelection(){return chatSel&&nodeByName(chatSel.node)?.shared?nodeByName(chatSel.node):null;}
function sharedControls(){
  const node=sharedSelection(),shared=!!node;
  document.body.classList.toggle('shared-agent',shared);
  document.body.classList.toggle('shared-observer',node?.role==='observer');
  document.body.classList.toggle('shared-manager',node?.role==='manager');
  const note=document.getElementById('sharedAgentNote');
  note.hidden=!shared;
  note.textContent=shared&&node.error?node.error:shared?`Shared agent · ${node.role} · messages sent as ${node.recipient}. ${node.role==='observer'?'You can read the conversation and Surface.':'Everyone with access shares this conversation and its command queue.'}`:'';
  document.getElementById('btnShareAgent').hidden=shared||!chatSel||READ_ONLY;
}
async function renderSharing(){
  const box=document.getElementById('sharingList'),state=document.getElementById('sharingStatus');
  try{
    const data=await apiJson('/api/sharing');
    state.textContent=data.transport.url?'Tor sharing is running. Invitations expire after 15 minutes.':data.transport.running?'Tor is publishing your sharing address…':data.transport.installed?'Enable Tor to share or import agents.':'Install Tor on this Conductor to enable sharing.';
    document.getElementById('sharingEnable').hidden=!!data.transport.running;
    box.replaceChildren();
    for(const [kind,items] of [['exports',data.exports],['imports',data.imports]])for(const item of items){
      const row=document.createElement('div');row.className='row';
      const label=document.createElement('span');label.textContent=(kind==='exports'?'Shared with ':'Imported as ')+item.recipient+' · '+item.name+' · '+item.role+(item.revoked?' · revoked':' · expires '+new Date(item.expires*1000).toLocaleDateString());row.append(label);
      if(!item.revoked){const button=document.createElement('button');button.className='small';button.textContent=kind==='exports'?'Revoke':'Remove';button.onclick=async()=>{try{await apiJson('/api/sharing/'+kind+'/'+item.id,{method:'DELETE'});await renderSharing();await loadTree();}catch(e){toast(e.message);}};row.append(button);}
      box.append(row);
    }
  }catch(e){state.textContent=e.message;}
}
async function mountSharedSurface(t){
  const tb=t.el.querySelector('.tb');
  for(const selector of ['.ws','.wsnew','.wsopen']){const el=t.el.querySelector(selector);if(el)el.hidden=true;}
  const generation=(t.sharedGeneration||0)+1;t.sharedGeneration=generation;
  try{
    const node=nodeByName(t.node);
    const data=READ_ONLY?await apiJson('/api/control/presenter/surface-access',{method:'POST',body:{node:t.node,ws:t.workspace||projectOf(t.node,t.project)?.surface_ws}}):await apiJson('/api/sharing/imports/'+node.shared_id+'/surface',{method:'POST',body:{}});
    if(!stageTiles.includes(t)||t.sharedGeneration!==generation)return;
    let port=location.protocol==='https:'?data.tls_port:data.port;
    if(LODGE){
      const bridge=location.port==='18787'||new URLSearchParams(location.search).get('tunnel')==='1';
      port=bridge?Number(location.port)+18+data.slot:8900+data.slot+(Number(new URLSearchParams(location.search).get('surface_offset'))||0);
    }
    if(!port)throw Error('This Conductor needs HTTPS configured for shared Surface.');
    const url=`${location.protocol}//${location.hostname}:${port}/?embed=1&view=presenter&ws=${encodeURIComponent(data.workspace)}&presentation=shared`;
    if(tb.dataset.src===url)return;
    const frame=document.createElement('iframe');frame.title='Shared agent Surface';
    frame.src=url+'#access='+encodeURIComponent(data.access);frame.sandbox='allow-scripts allow-same-origin allow-downloads';
    tb.replaceChildren(frame);tb.dataset.src=url;
  }catch(e){tb.dataset.src='';const note=document.createElement('div');note.className='off';note.textContent=e.message;const retry=document.createElement('button');retry.textContent='Retry Surface';retry.onclick=()=>mountSharedSurface(t);note.append(document.createElement('br'),retry);tb.replaceChildren(note);}
}
function openShareAgent(){
  if(!chatSel||!viewedAgentId||sharedSelection())return;
  const dialog=document.getElementById('shareAgentDialog');
  dialog.dataset.node=chatSel.node;dialog.dataset.project=chatSel.project;dialog.dataset.agent=viewedAgentId;
  document.getElementById('shareAgentTarget').textContent=projectOf(chatSel.node,chatSel.project)?.name+' / '+viewedAgentId;
  document.getElementById('shareAgentCode').value='';document.getElementById('shareAgentHistory').checked=false;
  document.getElementById('shareAgentError').textContent='';dialog.showModal();document.getElementById('shareAgentRecipient').focus();
}
function initializeSharing(){
  document.getElementById('btnShareAgent').onclick=openShareAgent;
  document.getElementById('shareAgentClose').onclick=()=>document.getElementById('shareAgentDialog').close();
  document.getElementById('sharingEnable').onclick=async()=>{try{await apiJson('/api/sharing/enable',{method:'POST',body:{}});await renderSharing();}catch(e){toast(e.message,7000);}};
  document.getElementById('sharingRefresh').onclick=renderSharing;
  document.getElementById('sharingImport').onclick=async()=>{
    const button=document.getElementById('sharingImport');button.disabled=true;
    try{await apiJson('/api/sharing/imports',{method:'POST',body:{code:document.getElementById('sharingCode').value}});document.getElementById('sharingCode').value='';await renderSharing();await loadTree();toast('Shared agent imported. Select it in the fleet.');}
    catch(e){document.getElementById('sharingStatus').textContent=e.message;}finally{button.disabled=false;}
  };
  document.getElementById('shareAgentCreate').onclick=async()=>{
    const dialog=document.getElementById('shareAgentDialog'),button=document.getElementById('shareAgentCreate');button.disabled=true;
    try{
      const data=await apiJson('/api/sharing/exports',{method:'POST',body:{...dialog.dataset,recipient:document.getElementById('shareAgentRecipient').value,role:document.getElementById('shareAgentRole').value,days:Number(document.getElementById('shareAgentDays').value),include_history:document.getElementById('shareAgentHistory').checked}});
      document.getElementById('shareAgentCode').value=data.code;
      document.getElementById('shareAgentError').textContent='Send this one-time invitation to its recipient through your private chat. It expires in 15 minutes.';
    }catch(e){document.getElementById('shareAgentError').textContent=e.message;}finally{button.disabled=false;}
  };
  document.getElementById('shareAgentCopy').onclick=async()=>{try{const field=document.getElementById('shareAgentCode');if(!field.value)return;await navigator.clipboard.writeText(field.value);toast('Invitation copied');}catch{document.getElementById('shareAgentCode').select();}};
}
