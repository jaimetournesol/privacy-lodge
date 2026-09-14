/** Surface authenticates direct listeners as well as gateway traffic. */
import crypto from 'node:crypto';
import type { IncomingMessage } from 'node:http';

export type Access = {role:'operator'|'presenter'; ws?:string[]; exp?:number; aud?:string};
const secret=process.env.SURFACE_AUTH_TOKEN ?? '';
const equal=(a:string,b:string)=>{const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&crypto.timingSafeEqual(x,y);};
export function ticket(value:string):Access|null {
  if(!secret||!value)return null;
  if(equal(value,secret))return {role:'operator'};
  try{
    const [payload,sig]=value.split('.');
    if(!sig||!equal(sig,crypto.createHmac('sha256',secret).update(payload).digest('hex')))return null;
    const data=JSON.parse(Buffer.from(payload,'base64url').toString());
    if(data.aud!=='surface'||!['operator','presenter'].includes(data.role)||data.exp<=Date.now()/1000||data.exp>Date.now()/1000+21660)return null;
    return data;
  }catch{return null;}
}
export function trustedOrigin(req:IncomingMessage):boolean {
  if(req.headers['sec-fetch-site']==='cross-site')return false;
  if(!req.headers.origin)return true;
  try{return new URL(req.headers.origin).host===req.headers.host;}catch{return false;}
}
export function access(req:IncomingMessage):Access|null {
  if(!trustedOrigin(req))return null;
  const url=new URL(req.url??'/','http://local');
  const explicit=url.searchParams.get('access')||req.headers['x-agentnode-token'];
  if(explicit)return ticket(String(explicit));
  const cookies=String(req.headers.cookie??'').split(';').map(x=>x.trim().split('='));
  const grants=cookies.filter(([name])=>name.startsWith('surface_access_')).map(([,v])=>ticket(v)).filter((x):x is Access=>!!x);
  const operators=grants.filter(g=>g.role==='operator');
  if(operators.length)return {role:'operator',exp:Math.min(...operators.map(g=>g.exp??Date.now()/1000+21600))};
  if(grants.length)return {role:'presenter',ws:[...new Set(grants.flatMap(g=>g.ws??[]))],exp:Math.min(...grants.map(g=>g.exp??0))};
  // Internal service compatibility is limited to a real, unforwarded loopback request.
  const local=['127.0.0.1','::1','::ffff:127.0.0.1'].includes(req.socket.remoteAddress??'');
  const host=['127.0.0.1','localhost','[::1]'].includes(urlHost(req.headers.host??''));
  if(process.env.LODGE_MODE !== '1'&&local&&host&&!req.headers['x-forwarded-for']&&!req.headers.forwarded&&!req.headers.origin)return {role:'operator'};
  return null;
}
function urlHost(host:string){try{return new URL('http://'+host).hostname;}catch{return '';}}
export const canView=(a:Access,id:string)=>a.role==='operator'||!!a.ws?.includes(id);
export function cookie(value:string){
  const grant=ticket(value);
  const scope=grant?.role==='operator'?'operator':JSON.stringify([...(grant?.ws??[])].sort());
  const key=crypto.createHash('sha256').update(secret+'|'+scope).digest('hex').slice(0,12);
  return `surface_access_${key}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=21600`;
}
export const loginPage=`<!doctype html><meta name="referrer" content="no-referrer"><title>Connect Surface</title><p id="status">Open this workspace from Conductor to connect.</p><script>
const token=new URLSearchParams(location.hash.slice(1)).get('access');
if(token)fetch('/api/auth/session',{method:'POST',headers:{'X-AgentNode-Token':token}}).then(r=>{if(!r.ok)throw Error('Access expired. Reopen from Conductor.');location.reload();}).catch(e=>document.querySelector('#status').textContent=e.message);
</script>`;
