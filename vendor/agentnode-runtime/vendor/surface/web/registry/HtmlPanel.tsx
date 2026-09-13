import { memo, useEffect, useState } from 'react';
import type { SurfaceComponent } from '../../shared/protocol';

/**
 * Sandboxed one-off HTML. The bridge script is injected so window.surface
 * exists; the component id rides in via window.__SURFACE_ID (srcdoc has no
 * query string). Theme arrives from the shell on the bridge's __ready ping.
 */
let runtime:Promise<string>|null=null;
function bridge(){return runtime??=(fetch('/__surface/bridge.js').then(async r=>{if(!r.ok)throw Error('Surface runtime unavailable');return r.text();}).catch(error=>{runtime=null;throw error;}));}

export const HtmlPanel = memo(
  ({ comp, expanded }: { comp: SurfaceComponent; v: number; expanded: boolean }) => {
    const p = comp.props as { html?: string; height?: number | 'fill' };
    const [code,setCode]=useState<string|null>(null);
    const [failed,setFailed]=useState(false);
    useEffect(()=>{let active=true;bridge().then(text=>{if(active)setCode(text);}).catch(()=>{if(active)setFailed(true);});return()=>{active=false;};},[]);
    if(code===null)return <div role="status">{failed?'Surface runtime unavailable. Reload to reconnect.':'Loading panel…'}</div>;
    // Fetch authenticated runtime code in the parent; opaque srcdoc origins
    // cannot inherit its cookies or pass the gateway origin checks.
    const doc = `<script>window.__SURFACE_ID=${JSON.stringify(comp.id).replace(/</g,'\\u003c')};${code.replace(/<\/script/gi,'<\\/script')}</script>${p.html ?? ''}`;
    return (
      <iframe
        className="panel-frame"
          data-surface-id={comp.id}
        title={comp.title ?? comp.id}
        sandbox="allow-scripts"
        srcDoc={doc}
        onMouseEnter={(e) => e.currentTarget.contentWindow?.focus()}
        style={{ height: expanded || p.height === 'fill' ? '100%' : (p.height ?? 360) }}
      />
    );
  },
  // `v` is a scalar snapshot — comp itself is mutated in place by the patcher.
  (a, b) => a.v === b.v && a.expanded === b.expanded,
);
