/* Native dropdowns with a custom entry; no provider keys or model calls.
   An optional effort dropdown lists the CLI's reasoning levels and hides itself for CLIs without any. */
(() => {
  class ModelPicker {
    constructor(select, custom, backend, getCatalog, effort=null, effortRow=null) {
      Object.assign(this,{select,custom,backend,getCatalog,effort,effortRow:effortRow||effort});this.generation=0;
      select.onchange=()=>this.syncCustom();
      backend.addEventListener('change',()=>this.refresh());
    }
    syncCustom(){this.custom.hidden=this.select.value!=='__custom';this.custom.required=!this.custom.hidden;}
    value(){if(this.select.value==='__custom'&&!this.custom.value.trim())throw new Error('Enter a custom model ID.');return this.select.value==='__custom'?this.custom.value.trim():this.select.value;}
    effortValue(){return this.effort&&!this.effortRow.hidden&&this.effort.value?this.effort.value:undefined;}
    async refresh(selected='',effort=''){
      const generation=++this.generation;
      // Clear old-backend choices immediately. A slow response cannot restore them.
      this.render(null,selected,effort);
      try{
        const data=await this.getCatalog();if(generation!==this.generation)return;
        const choice=this.select.value, custom=this.custom.value, level=this.effort?.value||'';
        this.render(data,choice==='__custom'?'':choice,level);
        if(choice==='__custom'){this.select.value=choice;this.custom.value=custom;this.syncCustom();}
      }
      catch{if(generation===this.generation)this.select.title='Model list unavailable. Enter a custom model or use the node default.';}
    }
    render(data,selected,effort=''){
      const backend=this.backend.value||data?.backend||'claude', entry=data?.backends?.[backend];
      this.select.replaceChildren(new Option(entry?.default==='auto'?'Automatic (Codex recommendation)':entry?.default?`Node default (${entry.default})`:'Node default',''));
      const models=new Set(entry?.models||[]);if(selected)models.add(selected);
      for(const model of [...models].sort())this.select.add(new Option(model==='auto'?'Automatic (Codex recommendation)':model,model));
      this.select.add(new Option('Custom model…','__custom'));this.select.value=selected;
      this.select.title=data?.note||'Models configured on this machine';
      this.custom.value='';this.custom.placeholder=backend==='opencode'?'provider/model':'Model ID';this.syncCustom();
      if(!this.effort)return;
      const levels=entry?.efforts||[];if(effort&&!levels.includes(effort))levels.push(effort);
      this.effort.replaceChildren(new Option(entry?.effort_default?`Node default (${entry.effort_default})`:'Node default',''));
      for(const level of levels)this.effort.add(new Option(level,level));
      this.effort.value=effort;this.effort.title='Reasoning effort passed to the CLI; unset keeps its default';
      this.effortRow.hidden=!levels.length;this.effortRow.style.display=levels.length?'':'none';  // .row flex styling would otherwise override hidden
    }
  }
  window.ConductorModelPicker=ModelPicker;
})();
