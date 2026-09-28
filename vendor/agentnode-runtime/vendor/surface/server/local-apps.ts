/** Workspace-scoped proxies for explicitly allowlisted loopback applications. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import http, { type IncomingMessage, type ServerResponse, type OutgoingHttpHeaders } from 'node:http';
import https from 'node:https';
import type { Duplex } from 'node:stream';
import { access, canView, trustedOrigin, type Access } from './auth.ts';

type Registration = {id:string; ws:string; component:string; upstream:string; presenterReadOnly:boolean};
const safe = new Set(['GET','HEAD']);
const hop = new Set(['connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade']);
const requestHeaders = new Set(['accept','accept-encoding','accept-language','content-type','content-length','content-encoding','range','if-range','if-match','if-none-match','if-modified-since','if-unmodified-since','cache-control','last-event-id','x-csrf-token','x-xsrf-token','x-requested-with','sec-websocket-key','sec-websocket-version','sec-websocket-protocol','sec-websocket-extensions']);
const prefix = (id:string) => `/local-apps/${id}/`;
const cookiePrefix = (id:string) => `surface_app_${id.replaceAll('-','')}_`;
function fail(res:ServerResponse, status:number, error:string) {
  if (res.headersSent) {res.destroy(); return;}
  res.writeHead(status, {'Content-Type':'application/json','Cache-Control':'no-store'});
  res.end(JSON.stringify({error}));
}

export class LocalAppHub {
  private registrations: Registration[] = [];
  private file: string;
  private allow: Set<string>;
  constructor(root:string, private attached:(ws:string, component:string, id:string)=>boolean) {
    this.file = path.join(root,'.surface-hub','local-apps.json');
    this.allow = new Set((process.env.SURFACE_LOCAL_APP_UPSTREAMS ?? '').split(',').filter(Boolean).map(s=>this.origin(s.trim())));
    if (fs.existsSync(this.file)) this.registrations = JSON.parse(fs.readFileSync(this.file,'utf8'));
  }
  private origin(value:string):string {
    const u = new URL(value);
    if (!['http:','https:'].includes(u.protocol) || !['127.0.0.1','[::1]'].includes(u.hostname) || !u.port || u.username || u.password || u.search || u.hash || u.pathname !== '/') throw new Error('Local apps require a literal loopback origin with a port and no path');
    return u.origin;
  }
  register(ws:string, component:string, upstream:string, presenterReadOnly=false) {
    const origin = this.origin(upstream);
    if (!this.allow.has(origin)) throw new Error('Upstream is not in SURFACE_LOCAL_APP_UPSTREAMS; an operator must allowlist it on this machine');
    let r = this.registrations.find(r=>r.ws===ws && r.component===component);
    if (!r) {r={id:crypto.randomUUID(),ws,component,upstream:origin,presenterReadOnly}; this.registrations.push(r);}
    r.upstream=origin; r.presenterReadOnly=presenterReadOnly;
    fs.mkdirSync(path.dirname(this.file), {recursive:true});
    const temp=this.file+'.tmp';fs.writeFileSync(temp,JSON.stringify(this.registrations),{mode:0o600});fs.renameSync(temp,this.file);
    return {localAppId:r.id, url:prefix(r.id)};
  }
  private lookup(req:IncomingMessage, grant:Access, websocket=false):Registration {
    const match=/^\/local-apps\/([0-9a-f-]{36})\//.exec(req.url ?? '');
    const r=this.registrations.find(r=>r.id===match?.[1]);
    if (!r || !this.attached(r.ws,r.component,r.id)) throw {status:404,message:'Local application is not registered in this workspace'};
    if (!canView(grant,r.ws)) throw {status:403,message:'Application is outside this presentation'};
    if (grant.role==='presenter' && (!r.presenterReadOnly || websocket || !safe.has(req.method ?? ''))) throw {status:403,message:'Presentation is read-only; this application operation is unavailable'};
    const protocol=('encrypted' in req.socket && req.socket.encrypted)?'https':'http';
    const origin=req.headers.origin;
    if (!trustedOrigin(req) || (origin && origin!==`${protocol}://${req.headers.host}`) || ((!safe.has(req.method ?? '') || websocket) && !origin)) throw {status:403,message:'A same-origin browser request is required'};
    if (!this.allow.has(r.upstream)) throw {status:503,message:'Application unavailable: upstream is no longer allowlisted'};
    if (['CONNECT','TRACE'].includes(req.method ?? '')) throw {status:405,message:'Method not allowed'};
    return r;
  }
  private target(req:IncomingMessage,r:Registration) {
    const raw=(req.url ?? '').slice(prefix(r.id).length);
    // Keep the target authority fixed even for //host, encoded slashes, or dot segments.
    const target=new URL(r.upstream);
    const split=raw.indexOf('?');
    target.pathname='/'+(split<0?raw:raw.slice(0,split));
    target.search=split<0?'':raw.slice(split);
    target.searchParams.delete('access'); // Surface tickets must never reach the app.
    return target;
  }
  private headers(req:IncomingMessage,r:Registration,websocket=false):OutgoingHttpHeaders {
    const headers:OutgoingHttpHeaders={};
    const blocked=new Set([...hop,...String(req.headers.connection ?? '').toLowerCase().split(',').map(x=>x.trim())]);
    for (const [k,v] of Object.entries(req.headers)) if (requestHeaders.has(k) && !blocked.has(k)) headers[k]=v;
    const tag=cookiePrefix(r.id);
    const cookies=String(req.headers.cookie ?? '').split(';').map(x=>x.trim()).filter(x=>x.startsWith(tag)).map(x=>x.slice(tag.length));
    if (cookies.length) headers.cookie=cookies.join('; ');
    headers.host=new URL(r.upstream).host;
    if (req.headers.origin) headers.origin=r.upstream; // Original Origin was checked at the proxy boundary.
    headers['x-forwarded-prefix']=prefix(r.id).slice(0,-1);
    headers['x-forwarded-host']=req.headers.host;
    headers['x-forwarded-proto']=('encrypted' in req.socket && req.socket.encrypted)?'https':'http';
    if (websocket) {headers.connection='Upgrade';headers.upgrade='websocket';}
    return headers;
  }
  private responseHeaders(source:IncomingMessage,r:Registration,target:URL,websocket=false):OutgoingHttpHeaders {
    const blocked=new Set([...hop,...String(source.headers.connection ?? '').toLowerCase().split(',').map(x=>x.trim()),'set-cookie','location','refresh','access-control-allow-origin','access-control-allow-credentials']);
    const headers:OutgoingHttpHeaders={};
    for (const [k,v] of Object.entries(source.headers)) if (!blocked.has(k)) headers[k]=v;
    headers['cache-control']='no-store';headers['referrer-policy']='no-referrer';
    if (source.headers.location) {
      const location=new URL(source.headers.location,target);
      if (location.origin!==r.upstream || location.username || location.password) throw new Error('Upstream redirect leaves the registered application');
      // Accept redirects from apps that already use the agreed external prefix.
      headers.location=(location.pathname.startsWith(prefix(r.id))?'':prefix(r.id).slice(0,-1))+location.pathname+location.search+location.hash;
    }
    const cookies=(source.headers['set-cookie'] ?? []).flatMap(value=>{
      const [pair,...attributes]=value.split(';');const equal=pair.indexOf('=');
      if (equal<1) return [];
      const name=pair.slice(0,equal).trim();
      if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name) || name.startsWith('surface_')) return [];
      const kept=attributes.filter(a=>!/^\s*(domain|path)\s*=/i.test(a));
      return [`${cookiePrefix(r.id)}${pair.trim()}; Path=${prefix(r.id)};${kept.join(';')}`];
    });
    if (cookies.length) headers['set-cookie']=cookies;
    if (websocket) {headers.connection='Upgrade';headers.upgrade='websocket';}
    return headers;
  }
  handle(req:IncomingMessage,res:ServerResponse,grant:Access):boolean {
    if (!(req.url ?? '').startsWith('/local-apps/')) return false;
    let r:Registration;
    try {r=this.lookup(req,grant);} catch(e) {const err=e as {status?:number;message:string};fail(res,err.status ?? 400,err.message);return true;}
    const target=this.target(req,r);
    const upstream=(target.protocol==='https:'?https:http).request(target,{method:req.method,headers:this.headers(req,r)},source=>{
      try {res.writeHead(source.statusCode ?? 502,this.responseHeaders(source,r,target));}
      catch {source.destroy();fail(res,502,'Application unavailable: invalid upstream redirect');return;}
      source.on('error',()=>res.destroy());source.pipe(res);
    });
    // Streaming bodies and responses use pipe backpressure; no proxy upload limit.
    upstream.setTimeout(120000,()=>upstream.destroy(new Error('upstream timed out')));
    upstream.on('error',()=>fail(res,502,'Application unavailable. The local service may be stopped; try again shortly.'));
    req.on('aborted',()=>upstream.destroy());res.on('close',()=>upstream.destroy());
    const expiry=grant.exp?setTimeout(()=>{upstream.destroy();res.destroy();},Math.max(1,grant.exp*1000-Date.now())):null;
    res.on('close',()=>{if(expiry)clearTimeout(expiry);});
    req.pipe(upstream);
    return true;
  }
  upgrade(req:IncomingMessage,socket:Duplex,head:Buffer) {
    const reject=(status:number,message:string)=>socket.end(`HTTP/1.1 ${status} Rejected\r\nConnection: close\r\nContent-Type: text/plain\r\n\r\n${message}`);
    const grant=access(req);
    if (!grant) {reject(401,'Unauthorized');return;}
    let r:Registration;
    try {r=this.lookup(req,grant,true);} catch(e) {const err=e as {status?:number;message:string};reject(err.status ?? 400,err.message);return;}
    if (req.headers.upgrade?.toLowerCase()!=='websocket') {reject(400,'Expected WebSocket');return;}
    const target=this.target(req,r);
    const upstream=(target.protocol==='https:'?https:http).request(target,{headers:this.headers(req,r,true)});
    const timer=setTimeout(()=>upstream.destroy(new Error('upgrade timeout')),10000);
    upstream.on('upgrade',(response,peer,upstreamHead)=>{
      clearTimeout(timer);
      let headers:OutgoingHttpHeaders;
      try {headers=this.responseHeaders(response,r,target,true);}
      catch {peer.destroy();reject(502,'Application unavailable: invalid upgrade headers');return;}
      socket.write('HTTP/1.1 101 Switching Protocols\r\n'+Object.entries(headers).flatMap(([k,v])=>(Array.isArray(v)?v:[v]).map(value=>`${k}: ${value}\r\n`)).join('')+'\r\n');
      if (upstreamHead.length) socket.write(upstreamHead);
      if (head.length) peer.write(head);
      const expiry=grant.exp?setTimeout(()=>{peer.destroy();socket.destroy();},Math.max(1,grant.exp*1000-Date.now())):null;
      peer.on('error',()=>socket.destroy());socket.on('error',()=>peer.destroy());
      socket.on('close',()=>{peer.destroy();if(expiry)clearTimeout(expiry);});peer.on('close',()=>socket.destroy());
      socket.pipe(peer);peer.pipe(socket);
    });
    upstream.on('response',response=>{clearTimeout(timer);response.resume();reject(502,'Application unavailable: WebSocket upgrade refused');});
    upstream.on('error',()=>{clearTimeout(timer);reject(502,'Application unavailable');});
    socket.on('close',()=>{clearTimeout(timer);upstream.destroy();});socket.on('error',()=>upstream.destroy());
    upstream.end();
  }
}
