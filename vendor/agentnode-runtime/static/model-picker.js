/* Native dropdowns with a custom entry; no provider keys or model calls. */
(() => {
  class ModelPicker {
    constructor(select, custom, backend, getCatalog) {
      Object.assign(this,{select,custom,backend,getCatalog});this.generation=0;
      select.onchange=()=>this.syncCustom();
      backend.addEventListener('change',()=>this.refresh());
    }
    syncCustom(){this.custom.hidden=this.select.value!=='__custom';this.custom.required=!this.custom.hidden;}
    value(){if(this.select.value==='__custom'&&!this.custom.value.trim())throw new Error('Enter a custom model ID.');return this.select.value==='__custom'?this.custom.value.trim():this.select.value;}
    async refresh(selected=''){
      const generation=++this.generation;
      // Clear old-backend choices immediately. A slow response cannot restore them.
      this.render(null,selected);
      try{
        const data=await this.getCatalog();if(generation!==this.generation)return;
        const choice=this.select.value, custom=this.custom.value;
        this.render(data,choice==='__custom'?'':choice);
        if(choice==='__custom'){this.select.value=choice;this.custom.value=custom;this.syncCustom();}
      }
      catch{if(generation===this.generation)this.select.title='Model list unavailable. Enter a custom model or use the node default.';}
    }
    render(data,selected){
      const backend=this.backend.value||data?.backend||'claude', entry=data?.backends?.[backend];
      this.select.replaceChildren(new Option(entry?.default==='auto'?'Automatic (Codex recommendation)':entry?.default?`Node default (${entry.default})`:'Node default',''));
      const models=new Set(entry?.models||[]);if(selected)models.add(selected);
      for(const model of [...models].sort())this.select.add(new Option(model==='auto'?'Automatic (Codex recommendation)':model,model));
      this.select.add(new Option('Custom model…','__custom'));this.select.value=selected;
      this.select.title=data?.note||'Models configured on this machine';
      this.custom.value='';this.custom.placeholder=backend==='opencode'?'provider/model':'Model ID';this.syncCustom();
    }
  }
  window.ConductorModelPicker=ModelPicker;
})();
