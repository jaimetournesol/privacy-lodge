/* Privacy Lodge product shell. Chat, history and Surface keep their existing owners. */
const lodgeUI = {
  view:'conductor', machine:null, filter:'', generation:0, auth:new Map(), authBusy:false,
  follow:true, stageChoice:null, ready:false, detail:null,
};
function lodgeElement(tag, text, cls) {
  const el=document.createElement(tag); if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;
}
function lodgeButton(text, action, cls='') {
  const el=lodgeElement('button',text,cls);el.type='button';el.onclick=action;return el;
}
function lodgeName(node) { return node==='control'?'Conductor machine':node==='worker'?'Docker worker':node; }
function lodgeStatus(project) {
  if(project.approval==='pending')return 'Needs approval';
  if(project.approval==='revoked')return 'Approval removed';
  if(project.status==='working')return project.tool?'Working · '+shortTool(project.tool):'Working';
  return project.status==='error'?'Needs attention':project.running||project.alive?'Ready':'Stopped';
}
function lodgeDialog(title) {
  const dialog=lodgeElement('dialog',undefined,'lodge-dialog');
  const heading=lodgeElement('h2',title);heading.id='lodge-dialog-'+crypto.randomUUID();dialog.setAttribute('aria-labelledby',heading.id);
  const top=lodgeElement('div',undefined,'lodge-row');top.append(heading,lodgeButton('Close',()=>dialog.close()));dialog.append(top);
  dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);return dialog;
}
function lodgeNotice(root, text, action) {
  const box=lodgeElement('div',undefined,'lodge-notice');box.append(lodgeElement('p',text));if(action)box.append(action);root.append(box);
}
function lodgeField(form, text, kind='text', value='') {
  const label=lodgeElement('label',text,'lodge-field'), input=document.createElement(kind==='select'?'select':'input');
  if(kind!=='select')input.type=kind;input.value=value;label.append(input);form.append(label);return input;
}
async function lodgeRun(button, status, action) {
  if(button.disabled)return;button.disabled=true;status.textContent='Saving…';
  try{await action();}catch(error){status.textContent=error.message||'Could not save. Try again.';}finally{button.disabled=false;}
}
function lodgeNavigate(view, machine=null) {
  saveDraft();lodgeUI.view=view;lodgeUI.machine=machine;lodgeUI.generation++;
  document.body.dataset.lodgeView=view;
  document.querySelectorAll('#lodgeNav button').forEach(b=>{
    if(b.dataset.view===(view==='conversation'?'chats':view))b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');
  });
  const browse=view==='machines'||view==='chats';
  document.querySelector('main').inert=browse;
  document.getElementById('lodgeBrowse').hidden=!browse;
  document.getElementById('chatPane').inert=browse||view==='stage';
  if(view==='stage')document.getElementById('input').blur();
  if(view==='conductor'){
    const c=conductorSel();if(c&&(chatSel?.node!==c.node||chatSel?.project!==c.project))selectProject(c.node,c.project,false);
  }
  lodgeUI.follow=view!=='stage'||!lodgeUI.stageChoice;
  document.getElementById('lodgeResetStage').hidden=view!=='conductor';
  document.getElementById('lodgeChooseStage').hidden=view!=='stage';
  if(!browse){
    if(lodgeUI.follow)lodgeFollowStage();
    else applyPresentationTiles(lodgeUI.stageChoice);
  }
  if(browse){lodgeRenderBrowse();document.getElementById('lodgeBrowseTitle').focus();}
  else {requestAnimationFrame(()=>{layoutStage(false);lodgeSyncStageControls();});lodgeSyncChat();}
}
function lodgeOpenChat(node,project,agent) {
  // Reading a conversation must never switch the running agent on the server.
  if(agent)viewedAgents.set(node+'/'+project,agent);
  lodgeNavigate(isConductor(node,project)?'conductor':'conversation');
  selectProject(node,project,false);
}
function lodgeSyncChat() {
  if(!lodgeUI.ready)return;
  const project=chatSel&&projectOf(chatSel.node,chatSel.project), agent=agentsCache.items.find(a=>a.id===viewedAgentId);
  const title=document.getElementById('lodgeChatTitle'), subtitle=document.getElementById('lodgeChatSubtitle');
  title.textContent=project?.conductor?'Conductor':agent?.name||project?.name||'Choose a conversation';
  subtitle.textContent=project?.conductor?'Coordinates your machines':chatSel?lodgeName(chatSel.node)+' · '+(project?.name||chatSel.project):'';
  document.getElementById('input').placeholder=project?.conductor?'Message Conductor…':'Message '+(agent?.name||project?.name||'this agent')+'…';
  const status=lodgeUI.auth.get(chatSel?.node);
  const gate=document.getElementById('lodgeChatGate');gate.replaceChildren();
  let blocked=false;
  if(!nodeByName(chatSel?.node)?.reachable){blocked=true;gate.append(lodgeElement('span','Machine unavailable. Your draft is saved.'),lodgeButton('Retry',()=>loadTree()));}
  else if(status!=='ready'){
    blocked=true;gate.append(lodgeElement('span',status==='unknown'?'Checking Codex connection…':'Connect Codex on '+lodgeName(chatSel?.node||'control')+' to send.'),lodgeButton('Connect',()=>lodgeOpenAccounts(chatSel?.node)));
  } else if(agent?.approval!=='approved'||viewedAgentId!==agentsCache.active){
    blocked=true;gate.append(lodgeElement('span',agent?.approval!=='approved'?'This agent needs your approval.':'You’re reading a saved conversation. Activate it to continue.'),lodgeButton('Review agent',lodgeManageAgent));
  }
  gate.hidden=!blocked;document.getElementById('btnSend').disabled=blocked||!agent;
  document.getElementById('lodgeStop').hidden=project?.status!=='working';
}
function lodgeCanSend() {
  lodgeSyncChat();if(document.getElementById('btnSend').disabled){toast('Your draft is saved. Complete the connection or approval shown above.');return false;}return true;
}
function lodgeRenderBrowse() {
  if(!lodgeUI.ready||!['machines','chats'].includes(lodgeUI.view))return;
  const machines=lodgeUI.view==='machines', node=lodgeUI.machine&&nodeByName(lodgeUI.machine);
  const title=document.getElementById('lodgeBrowseTitle');title.textContent=node?lodgeName(node.name):machines?'Your machines':'Conversations';
  document.getElementById('lodgeBrowseHint').textContent=node?'Workspaces keep related files and agent conversations together.':machines?'Conductor coordinates the agents running on these machines.':'Browse by machine. Opening a chat leaves running work unchanged.';
  document.getElementById('lodgeBack').hidden=!lodgeUI.machine;
  const actions=document.getElementById('lodgeBrowseActions');actions.replaceChildren();
  actions.append(lodgeButton('Add agent',()=>lodgeAddAgent(node?.name),'primary'));
  if(machines&&!node)actions.append(lodgeButton('Connect machine',lodgeAddMachine));
  const select=document.getElementById('lodgeMachineFilter');
  if(!select.matches(':focus')){
    select.replaceChildren(new Option('All machines',''));
    for(const n of tree.nodes)select.add(new Option(lodgeName(n.name),n.name));select.value=lodgeUI.machine||'';
  }
  const list=document.getElementById('lodgeBrowseList');list.replaceChildren();
  const query=lodgeUI.filter.toLowerCase();
  for(const nd of tree.nodes.filter(n=>!lodgeUI.machine||n.name===lodgeUI.machine)){
    const projects=nd.projects.filter(p=>!query||[nd.name,p.name,p.dir].some(s=>String(s||'').toLowerCase().includes(query)));
    if(query&&!projects.length)continue;
    const card=lodgeElement('article',undefined,'lodge-machine');
    const head=lodgeElement('div',undefined,'lodge-row');
    const text=lodgeElement('div');text.append(lodgeElement('h3',lodgeName(nd.name)),lodgeElement('p',nd.reachable?'Connected · '+(nd.local?'Conductor':nd.info?.host_tools===false?'Isolated worker':'Remote machine'):'Offline · conversations will reconnect when available','lodge-muted'));
    head.append(text,lodgeElement('span',nd.reachable?'Online':'Offline','lodge-badge'+(nd.reachable?'':' offline')));card.append(head);
    if(machines&&!node){
      const count=projects.reduce((sum,p)=>sum+(p.agents||1),0);
      card.append(lodgeElement('p',projects.length+' workspace'+(projects.length===1?'':'s')+' · '+count+' agent'+(count===1?'':'s'),'lodge-muted'));
      const buttons=lodgeElement('div',undefined,'lodge-actions');buttons.append(lodgeButton('View agents & chats',()=>lodgeNavigate('machines',nd.name)),lodgeButton('Add agent',()=>lodgeAddAgent(nd.name)));card.append(buttons);
    }else{
      for(const p of projects){
        const row=lodgeElement('div',undefined,'lodge-workspace');
        const info=lodgeElement('div');info.append(lodgeElement('strong',p.name),lodgeElement('span',lodgeStatus(p)+' · '+(p.agents||1)+' agent'+((p.agents||1)===1?'':'s'),'lodge-muted'));row.append(info);
        row.append(lodgeButton('Chats',()=>lodgeShowAgents(nd.name,p)));
        if(node)row.append(lodgeButton('Add agent',()=>lodgeAddAgent(nd.name,p.id)));
        card.append(row);
      }
      if(!projects.length)card.append(lodgeElement('p','No workspaces yet. Add an agent to create your first one.','lodge-muted'));
      if(node){
        const buttons=lodgeElement('div',undefined,'lodge-actions');buttons.append(lodgeButton('Codex connection',()=>lodgeOpenAccounts(nd.name)));
        if(!nd.local&&nd.name!=='worker')buttons.append(lodgeButton('Disconnect machine',()=>lodgeDisconnect(nd)));
        card.append(buttons);
      }
    }
    list.append(card);
  }
  if(!list.children.length)lodgeNotice(list,query?'No matching workspaces. Try another name.':'No machines are available yet.',lodgeButton('Refresh',()=>loadTree()));
}
async function lodgeShowAgents(node, project) {
  const dialog=lodgeDialog(project.name+' · chats');
  dialog.append(lodgeElement('p',lodgeName(node)+' · '+project.dir,'lodge-muted'));
  const list=lodgeElement('div',undefined,'lodge-list');list.setAttribute('aria-live','polite');dialog.append(list);
  const refresh=async()=>{
    list.replaceChildren(lodgeElement('p','Loading conversations…'));
    try{
      const result=await apiJson(`${nodePrefix(node)}/api/projects/${encodeURIComponent(project.id)}/agents`);
      if(!dialog.isConnected)return;list.replaceChildren();
      for(const a of result.items||[]){
        const card=lodgeElement('div',undefined,'lodge-workspace'),info=lodgeElement('div');
        info.append(lodgeElement('strong',a.name),lodgeElement('span',(a.id===result.active?'Active agent':'Saved conversation')+' · '+(a.approval==='approved'?'Approved':'Needs approval'),'lodge-muted'));card.append(info);
        card.append(lodgeButton('Open chat',()=>{dialog.close();lodgeOpenChat(node,project.id,a.id);}));list.append(card);
      }
      if(!result.items?.length)list.append(lodgeElement('p','No conversations yet.'));
    }catch{list.replaceChildren();lodgeNotice(list,'Could not load conversations. Check the machine’s connection.',lodgeButton('Retry',refresh));}
  };
  dialog.append(lodgeButton('Add agent here',()=>{dialog.close();lodgeAddAgent(node,project.id);},'primary'));dialog.showModal();await refresh();
}
function lodgeAddAgent(selectedNode, selectedProject) {
  const dialog=lodgeDialog('Add an agent');
  dialog.append(lodgeElement('p','Choose where it works. Each agent has its own conversation; agents in the same workspace take turns using it.','lodge-muted'));
  const form=document.createElement('form');dialog.append(form);
  const machine=lodgeField(form,'1. Machine','select');
  for(const n of tree.nodes){const option=new Option(lodgeName(n.name)+(n.reachable?'':' · offline'),n.name);option.disabled=!n.reachable;machine.add(option);}
  machine.value=selectedNode||tree.nodes.find(n=>n.name==='worker'&&n.reachable)?.name||tree.nodes.find(n=>n.reachable)?.name||'';
  const workspace=lodgeField(form,'2. Workspace','select');
  const folder=lodgeField(form,'Folder on this machine');folder.placeholder='/data/agentnode/projects/my-project';folder.maxLength=4096;
  const name=lodgeField(form,'3. Agent name');name.placeholder='e.g. UI reviewer';name.required=true;name.maxLength=200;
  const details=document.createElement('details');details.append(lodgeElement('summary','Model & instructions'));form.append(details);
  const backend=document.createElement('select');backend.add(new Option('Codex','codex'));backend.hidden=true;details.append(backend);
  const model=lodgeField(details,'Codex model','select'),custom=lodgeField(details,'Custom model ID');custom.hidden=true;custom.parentElement.hidden=true;
  const picker=new ConductorModelPicker(model,custom,backend,()=>apiJson(`${nodePrefix(machine.value)}/api/models`));
  model.addEventListener('change',()=>custom.parentElement.hidden=custom.hidden);
  const instructions=lodgeField(details,'Instructions for a new workspace');instructions.maxLength=12000;instructions.placeholder='What should this agent focus on?';
  const updateWorkspace=()=>{const fresh=workspace.value==='__new';folder.parentElement.hidden=!fresh;folder.required=fresh;instructions.parentElement.hidden=!fresh;};
  const updateMachine=()=>{workspace.replaceChildren();for(const p of nodeByName(machine.value)?.projects||[])workspace.add(new Option(p.name,p.id));workspace.add(new Option('Create a new workspace…','__new'));workspace.value=selectedProject&&[...workspace.options].some(o=>o.value===selectedProject)?selectedProject:workspace.options[0]?.value;updateWorkspace();picker.refresh();};
  workspace.onchange=updateWorkspace;machine.onchange=updateMachine;updateMachine();
  const approval=lodgeElement('label',undefined,'lodge-check'),check=document.createElement('input');check.type='checkbox';approval.append(check,lodgeElement('span','Allow this agent to run commands and edit files available to its runtime. Codex sends prompts and tool results to OpenAI.'));form.append(approval);
  form.append(lodgeElement('p','Only the selected workspace’s active agent can run. You can activate this agent after reviewing its chat.','lodge-muted'));
  const status=lodgeElement('p',undefined,'lodge-form-status');status.setAttribute('role','status');form.append(status);
  const submit=lodgeElement('button','Create agent','primary');submit.type='submit';form.append(submit);
  let created=null;
  form.onsubmit=e=>{e.preventDefault();lodgeRun(submit,status,async()=>{
    if(!machine.value)throw new Error('Choose a connected machine.');
    // If approval fails, retry it without creating a second agent/workspace.
    if(!created){
      const base=nodePrefix(machine.value);let pid=workspace.value,agent;
      const chosen=picker.value();
      if(pid==='__new'){
        const result=await apiJson(base+'/api/projects',{method:'POST',body:{name:name.value.trim(),dir:folder.value.trim(),backend:'codex',model:chosen||undefined,host_tools:false,instructions:instructions.value.trim()||undefined}});
        pid=result.project.id;agent=result.agent;
      }else{
        const result=await apiJson(base+'/api/projects/'+encodeURIComponent(pid)+'/agents',{method:'POST',body:{name:name.value.trim(),backend:'codex',model:chosen||undefined}});agent=result.agent;
      }
      created={node:machine.value,project:pid,agent:agent.id};
      for(const field of form.querySelectorAll('input,select'))if(field!==check)field.disabled=true;
    }
    if(check.checked)await apiJson(`${nodePrefix(created.node)}/api/projects/${encodeURIComponent(created.project)}/agents/${encodeURIComponent(created.agent)}/approval`,{method:'POST',body:{approval:'approved'}});
    await loadTree();dialog.close();lodgeOpenChat(created.node,created.project,created.agent);toast(check.checked?'Agent created. Activate it when you’re ready.':'Agent created. Review its permissions before running.',5000);
  });};dialog.showModal();
}
function lodgeManageAgent() {
  if(!chatSel||!viewedAgentId)return;
  const target={...chatSel,agent:viewedAgentId},a=agentsCache.items.find(x=>x.id===target.agent);if(!a)return;
  const active=agentsCache.active,dialog=lodgeDialog(a.name),base=`${nodePrefix(target.node)}/api/projects/${encodeURIComponent(target.project)}`;
  dialog.append(lodgeElement('p',lodgeName(target.node)+' · '+projectOf(target.node,target.project)?.name,'lodge-muted'));
  dialog.append(lodgeElement('p','Codex · '+(a.model||'Machine default')+' · '+(a.approval==='approved'?'Approved':'Needs approval')));
  const status=lodgeElement('p',undefined,'lodge-form-status');status.setAttribute('role','status');
  const actions=lodgeElement('div',undefined,'lodge-actions');dialog.append(actions,status);
  const action=(label,path,body,method='POST',close=true)=>{
    const button=lodgeButton(label,()=>lodgeRun(button,status,async()=>{await apiJson(base+path,{method,body});await loadTree();await loadAgents();if(close)dialog.close();}));actions.append(button);return button;
  };
  if(a.approval!=='approved'){
    dialog.insertBefore(lodgeElement('p','Approval allows commands and file changes within this runtime. Prompts and tool results go to OpenAI. Approving does not start a task.','lodge-muted'),actions);
    action('Approve agent','/agents/'+encodeURIComponent(a.id)+'/approval',{approval:'approved'}).classList.add('primary');
  }else if(target.agent!==active){
    dialog.insertBefore(lodgeElement('p','Activating stops the current agent in this workspace. Its conversation is kept so you can return to it.','lodge-muted'),actions);
    action('Activate this agent','/agents/'+encodeURIComponent(a.id)+'/switch',{expected_agent:active}).classList.add('primary');
  }else{
    action('Start','/start',{expected_agent:target.agent});action('Stop work','/stop',{expected_agent:target.agent});
  }
  if(a.approval==='approved')action('Remove approval','/agents/'+encodeURIComponent(a.id)+'/approval',{approval:'revoked'});
  const form=document.createElement('form');const rename=lodgeField(form,'Agent name','text',a.name);rename.required=true;rename.maxLength=200;
  const save=lodgeElement('button','Save name');save.type='submit';form.append(save);
  form.onsubmit=e=>{e.preventDefault();lodgeRun(save,status,async()=>{await apiJson(base+'/agents/'+encodeURIComponent(a.id),{method:'PATCH',body:{name:rename.value.trim()}});await loadAgents();dialog.close();});};dialog.append(form);dialog.showModal();
}
function lodgeAddMachine() {
  const dialog=lodgeDialog('Connect a machine');
  dialog.append(lodgeElement('p','Your Docker worker is already installed. To add another computer, install Agentnode there, then connect it to this Conductor.','lodge-muted'));
  const setup=document.createElement('details');setup.append(lodgeElement('summary','Set up Agentnode on another computer'));
  setup.append(lodgeElement('p','In the Agentnode checkout on that computer, with Codex installed:'));
  setup.append(lodgeElement('pre','./install.sh --role worker --backend codex --lodge --services\n./venv/bin/python -m agentnode token'));
  setup.append(lodgeElement('p','Use its address reachable from the Conductor, its connection token, and its TLS certificate for HTTPS. Keep the token private. Sign in to Codex on that runtime.','lodge-muted'));dialog.append(setup);
  const form=document.createElement('form');dialog.append(form);
  const name=lodgeField(form,'Machine name');name.required=true;name.maxLength=80;name.pattern='[a-zA-Z0-9][a-zA-Z0-9_-]*';name.placeholder='e.g. studio';
  const url=lodgeField(form,'Agentnode address','url');url.required=true;url.placeholder='https://studio:8443';
  const token=lodgeField(form,'Connection token','password');token.required=true;token.autocomplete='off';
  const extra=document.createElement('details');extra.append(lodgeElement('summary','TLS certificate'));form.append(extra);
  const cert=lodgeElement('textarea');cert.setAttribute('aria-label','Machine TLS certificate in PEM format');cert.rows=5;cert.placeholder='-----BEGIN CERTIFICATE-----';extra.append(cert);
  const status=lodgeElement('p',undefined,'lodge-form-status');status.setAttribute('role','status');form.append(status);
  const button=lodgeElement('button','Check & connect','primary');button.type='submit';form.append(button);
  form.onsubmit=e=>{e.preventDefault();lodgeRun(button,status,async()=>{await apiJson('/api/control/nodes',{method:'POST',body:{name:name.value.trim(),url:url.value.trim(),token:token.value.trim(),ca:cert.value.trim()}});token.value='';await loadTree();dialog.close();lodgeNavigate('machines',name.value.trim());});};dialog.showModal();
}
function lodgeDisconnect(node) {
  const dialog=lodgeDialog('Disconnect '+lodgeName(node.name)+'?');dialog.append(lodgeElement('p','Conductor will stop reaching this machine. Its agents, conversations and files stay on that machine.'));
  const status=lodgeElement('p');status.setAttribute('role','status');const button=lodgeButton('Disconnect',()=>lodgeRun(button,status,async()=>{await apiJson('/api/control/nodes/'+encodeURIComponent(node.name),{method:'DELETE'});await loadTree();dialog.close();lodgeNavigate('machines');}));dialog.append(button,status);dialog.showModal();
}
async function lodgeAccounts() {
  if(lodgeUI.authBusy)return;lodgeUI.authBusy=true;
  try{
    await Promise.allSettled(tree.nodes.map(async node=>{
      try{const state=await apiJson(nodePrefix(node.name)+'/api/lodge/auth',{signal:AbortSignal.timeout(12000)});lodgeUI.auth.set(node.name,state.state);lodgeUI.auth.set(node.name+':detail',state);}
      catch{lodgeUI.auth.set(node.name,'unavailable');}
    }));
    lodgeSyncChat();lodgeRenderAccounts();
  }finally{lodgeUI.authBusy=false;lodgeUI.lastAuth=Date.now();}
}
function lodgeOpenAccounts(node) {
  const existing=document.getElementById('lodgeAccountsDialog');if(existing){existing.focus();return;}
  const dialog=lodgeDialog('Codex connections');dialog.id='lodgeAccountsDialog';dialog.dataset.node=node||'';
  dialog.append(lodgeElement('p','Authorize each machine once. Prompts, supplied files and tool results are sent to OpenAI. Credentials stay in that machine’s private runtime storage.','lodge-muted'));
  const accounts=lodgeElement('div');accounts.id='lodgeAccounts';dialog.append(accounts);dialog.showModal();lodgeRenderAccounts();lodgeAccounts();
}
function lodgeRenderAccounts() {
  const root=document.querySelector('#lodgeAccountsDialog #lodgeAccounts');if(!root)return;
  const wanted=root.closest('dialog').dataset.node;
  for(const node of tree.nodes.filter(n=>!wanted||n.name===wanted)){
    const state=lodgeUI.auth.get(node.name+':detail')||{state:'unknown'},key=lodgeUI.auth.get(node.name);
    let row=[...root.children].find(el=>el.dataset.node===node.name);
    const signature=JSON.stringify({key,code:state.code});if(row?.dataset.signature===signature)continue;
    if(!row){row=lodgeElement('section',undefined,'lodge-account');row.dataset.node=node.name;root.append(row);}row.dataset.signature=signature;row.replaceChildren(lodgeElement('h3',lodgeName(node.name)));
    const prefix=nodePrefix(node.name)+'/api/lodge/auth';
    const status=lodgeElement('p');status.setAttribute('role','status');
    if(key==='ready')row.append(lodgeElement('span','Codex connected','lodge-badge'));
    else if(key==='awaiting_authorization'){
      row.append(lodgeElement('p','Enter this one-time code on the Codex sign-in page.'));
      const code=lodgeElement('code',state.code);code.setAttribute('aria-label','Authorization code '+state.code);row.append(code);
      const link=lodgeElement('a','Open Codex sign-in','lodge-link');link.href='https://auth.openai.com/codex/device';link.target='_blank';link.rel='noopener noreferrer';row.append(link);
      const cancel=lodgeButton('Cancel sign-in',()=>lodgeRun(cancel,status,async()=>{await apiJson(prefix,{method:'DELETE'});await lodgeAccounts();}));row.append(cancel);
    }else if(key==='unavailable'){
      row.append(lodgeElement('p','Machine unavailable or missing Lodge sign-in support. Check its connection, or run codex login --device-auth on that machine.'));
    }else{
      const connect=lodgeButton(key==='starting'?'Preparing sign-in…':'Connect Codex',()=>lodgeRun(connect,status,async()=>{await apiJson(prefix,{method:'POST'});await lodgeAccounts();}),'primary');connect.disabled=key==='starting';row.append(connect);
      if(['failed','expired'].includes(key))row.append(lodgeElement('p','Sign-in did not finish. Try again.'));
    }row.append(status);
  }
}
function lodgeFollowStage() {
  applyPresentationTiles(stagePresentation.latest?.tiles||[]);
  if(!stageTiles.length)ambientShow();
  // The presentation controller rechecks canFollow after fetching: a late
  // response cannot replace a workspace selected while this request is pending.
  stagePresentation.refresh();
}
async function lodgeResetStage() {
  const button=document.getElementById('lodgeResetStage');
  if(button.disabled)return;
  button.disabled=true;button.setAttribute('aria-busy','true');
  try{await stagePresentation.reset();}
  catch(error){toast(error.message||'Could not reset the stage. Try again.',5000);}
  finally{button.disabled=false;button.removeAttribute('aria-busy');}
}
function lodgeExploreStage() {
  const dialog=lodgeDialog('Choose stage');
  dialog.append(lodgeElement('p','Open an agent’s workspace or a machine’s screen. Use × to close a view; its agent keeps running.','lodge-muted'));
  const choose=(label,tiles)=>{
    const button=lodgeButton(label,()=>{lodgeUI.stageChoice=tiles;lodgeUI.follow=!tiles;if(tiles)applyPresentationTiles(tiles);else lodgeFollowStage();dialog.close();});
    button.setAttribute('aria-pressed',String(JSON.stringify(lodgeUI.stageChoice)===JSON.stringify(tiles)));
    dialog.append(button);
  };
  choose('Conductor stage',null);
  const c=conductorSel();if(c)choose('Fleet',[{node:c.node,project:c.project,kind:'fleet',pinned:false}]);
  for(const node of tree.nodes){
    if(nodeHasScreen(node.name))choose(lodgeName(node.name)+' · Screen',[{node:node.name,project:null,kind:'screen',pinned:false}]);
    if(nodeHasHub(node.name))for(const p of node.projects){
      choose(p.name+' · '+lodgeName(node.name),[{node:node.name,project:p.id,kind:'surface',pinned:false}]);
    }
  }
  dialog.showModal();
}
function lodgeCloseStageTile(tile) {
  if(lodgeUI.view!=='stage'||!stageTiles.includes(tile))return;
  // Freeze this local composition before removing anything. Live presentation
  // updates must not resurrect a closed view, or overwrite the remaining ones.
  lodgeUI.stageChoice=presentationSpecs().filter(spec=>!sameTile(spec,tile));
  lodgeUI.follow=false;
  applyPresentationTiles(lodgeUI.stageChoice);
  (document.querySelector('.lodge-tile-close')||document.getElementById('lodgeChooseStage')).focus();
}
function lodgeSyncStageControls() {
  if(!lodgeUI.ready)return;
  for(const tile of stageTiles){
    let close=tile.el.querySelector('.lodge-tile-close');
    if(!close){close=lodgeButton('×',()=>lodgeCloseStageTile(tile),'lodge-tile-close');tile.el.append(close);}
    const name=tile.kind==='fleet'?'Fleet':(projectOf(tile.node,tile.project)?.name||lodgeName(tile.node))+' '+(tile.kind==='surface'?'surface':'screen');
    close.setAttribute('aria-label','Close '+name);close.title='Close '+name;
  }
  if(lodgeUI.view==='stage'&&!stageTiles.length){
    const empty=lodgeElement('div',undefined,'lodge-empty-stage');
    empty.append(lodgeElement('h2','No open stages'),lodgeElement('p','Choose an agent workspace or screen to open here.','lodge-muted'),lodgeButton('Choose stage',lodgeExploreStage,'primary'));
    document.getElementById('stage').replaceChildren(empty);
  }
}
function lodgeAfterTree() {if(lodgeUI.ready){lodgeRenderBrowse();lodgeSyncChat();}}
function enterLodge() {
  document.getElementById('boot').style.display='none';
  document.body.classList.add('lodge','showchat');voice.tts=false;voice.sounds=false;
  for(const option of [...document.querySelectorAll('#newAgentBackend option')])if(option.value!=='codex')option.remove();
  document.getElementById('newAgentBackend').value='codex';
  document.getElementById('lodgeAccount').remove();
  document.querySelector('header h1 span').textContent='Agents';
  const settings=document.getElementById('btnSettings');settings.textContent='Connections';settings.onclick=()=>lodgeOpenAccounts();
  const stageActions=lodgeElement('div',undefined,'lodge-stage-actions');
  const reset=lodgeButton('Fleet',lodgeResetStage);reset.id='lodgeResetStage';reset.title='Reset Conductor stage to Fleet';reset.setAttribute('aria-label','Reset Conductor stage to Fleet');
  const choose=lodgeButton('Choose stage',lodgeExploreStage);choose.id='lodgeChooseStage';choose.setAttribute('aria-haspopup','dialog');
  stageActions.append(reset,choose,settings);document.querySelector('header').append(stageActions);
  const head=document.getElementById('chatHead'),identity=lodgeElement('div',undefined,'lodge-chat-identity');
  const title=lodgeElement('strong','Conductor');title.id='lodgeChatTitle';const subtitle=lodgeElement('span');subtitle.id='lodgeChatSubtitle';identity.append(title,subtitle);
  head.prepend(identity);head.append(lodgeButton('Manage',lodgeManageAgent));
  const stop=lodgeButton('Stop',()=>sendCmd({type:'interrupt'}));stop.id='lodgeStop';head.append(stop);
  const gate=lodgeElement('div',undefined,'lodge-chat-gate');gate.id='lodgeChatGate';gate.setAttribute('role','status');document.getElementById('composer').before(gate);
  const browse=lodgeElement('section');browse.id='lodgeBrowse';browse.hidden=true;browse.setAttribute('aria-label','Browse agents');
  const top=lodgeElement('div',undefined,'lodge-browse-top'),back=lodgeButton('All machines',()=>lodgeNavigate(lodgeUI.view));back.id='lodgeBack';top.append(back);
  const heading=lodgeElement('h2');heading.id='lodgeBrowseTitle';heading.tabIndex=-1;top.append(heading);
  const hint=lodgeElement('p');hint.id='lodgeBrowseHint';top.append(hint);
  const actions=lodgeElement('div',undefined,'lodge-actions');actions.id='lodgeBrowseActions';top.append(actions);
  const search=lodgeField(top,'Find a workspace','search');search.placeholder='Search by workspace or machine';search.oninput=()=>{lodgeUI.filter=search.value;lodgeRenderBrowse();};
  const filter=lodgeField(top,'Machine','select');filter.id='lodgeMachineFilter';filter.onchange=()=>{lodgeUI.machine=filter.value||null;lodgeRenderBrowse();};
  const list=lodgeElement('div');list.id='lodgeBrowseList';browse.append(top,list);document.body.append(browse);
  const nav=lodgeElement('nav');nav.id='lodgeNav';nav.setAttribute('aria-label','Agents navigation');
  for(const [view,label,icon] of [['conductor','Conductor','◉'],['machines','Machines','▦'],['chats','Chats','☷'],['stage','Stage','▣']]){
    const button=lodgeButton('',()=>lodgeNavigate(view));button.dataset.view=view;const symbol=lodgeElement('span',icon);symbol.setAttribute('aria-hidden','true');button.append(symbol,lodgeElement('span',label));nav.append(button);
  }document.body.append(nav);
  setChat(true);lodgeUI.ready=true;
  // Native/mobile users choose when to focus the composer; do not summon the keyboard on entry.
  document.getElementById('input').blur();
  lodgeNavigate('conductor');applyPresentationTiles(stagePresentation.latest?.tiles||[]);if(!stageTiles.length)ambientShow();
  lodgeAccounts();setInterval(()=>{if(!document.hidden&&(document.getElementById('lodgeAccountsDialog')||Date.now()-(lodgeUI.lastAuth||0)>15000))lodgeAccounts();},3000);
}
