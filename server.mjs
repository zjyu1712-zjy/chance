import {createServer} from 'node:http';
import {randomBytes,createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {fileURLToPath,pathToFileURL} from 'node:url';

const COOKIE='__Host-mengniu-visitor';
const OPS=new Set(['state','start','input','switch_mode','activate','versions','export','export_card','exclude','restore','model_reply','analyze']);
const FILES=new Map([
 ['/', ['application.html','text/html; charset=utf-8']],
 ['/application', ['application.html','text/html; charset=utf-8']],
 ['/application.html', ['application.html','text/html; charset=utf-8']],
 ['/cases.json',['cases.json','application/json; charset=utf-8']],
 ['/favicon.svg',['favicon.svg','image/svg+xml']],
]);

function json(res,data,status=200,guest){
 res.statusCode=status;
 res.setHeader('Content-Type','application/json; charset=utf-8');
 if(guest)res.setHeader('Set-Cookie',`${COOKIE}=${guest}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=7776000`);
 res.end(JSON.stringify(data));
}
function validOrigin(req,publicOrigin){
 const origin=req.headers.origin;
 if(!origin)return false;
 if(publicOrigin)return origin===publicOrigin;
 try{
  const u=new URL(origin);
  const host=String(req.headers['x-forwarded-host']||req.headers.host||'').split(',')[0].trim();
  return u.host===host&&(u.protocol==='https:'||(u.protocol==='http:'&&['127.0.0.1','localhost'].includes(u.hostname)));
 }catch{return false;}
}

export function makeServer(options={}){
 const apiToken=options.apiToken??process.env.MENGNIU_AGENT_API_TOKEN;
 const upstream=options.upstream??process.env.MENGNIU_AGENT_UPSTREAM_URL??'https://mengniu-agent.coze.site/visitor/v1';
 const publicOrigin=options.publicOrigin??process.env.PUBLIC_ORIGIN;
 return createServer(async(req,res)=>{
  res.setHeader('Cache-Control','no-store');
  res.setHeader('X-Content-Type-Options','nosniff');
  res.setHeader('Referrer-Policy','strict-origin-when-cross-origin');
  let pathname;
  try{pathname=new URL(req.url,'http://localhost').pathname;}catch{return json(res,{error:'无效请求'},400);}
  if((req.method==='GET'||req.method==='HEAD')&&FILES.has(pathname)){
   const [file,type]=FILES.get(pathname);
   try{const bytes=await readFile(new URL('./public/'+file,import.meta.url));res.setHeader('Content-Type',type);return res.end(req.method==='HEAD'?undefined:bytes);}
   catch{return json(res,{error:'网页资源暂不可用'},503);}
  }
  if(pathname==='/healthz'&&req.method==='GET')return json(res,{ready:true,service:'mengniu-public-web',model_called:false});
  if(pathname!=='/api/bridge')return json(res,{error:'未找到页面'},404);
  if(!['GET','POST'].includes(req.method))return json(res,{error:'不支持的方法'},405);
  if(req.method==='POST'&&!validOrigin(req,publicOrigin))return json(res,{error:'不允许跨站提交'},403);
  if(!apiToken)return json(res,{ready:false,message:'分析服务凭证尚未配置；归档案例可正常浏览。',error:'分析服务尚未接入，未调用模型。'},503);
  if(req.method==='GET'){
   try{
    const r=await fetch(upstream+'/health',{headers:{Authorization:`Bearer ${apiToken}`},signal:AbortSignal.timeout(45000),redirect:'error'});
    if(!r.ok)return json(res,{ready:false,message:'正式分析服务暂不可用，归档案例仍可浏览。'},503);
    return json(res,await r.json());
   }catch{return json(res,{ready:false,message:'正式服务连接超时，未发起模型分析。'},503);}
  }
  let body;
  try{
   let raw='',size=0;
   for await(const part of req){size+=part.length;if(size>250000)return json(res,{error:'输入过长，请分批提交'},413);raw+=part;}
   if(raw.length>100000)return json(res,{error:'输入过长，请分批提交'},413);
   body=JSON.parse(raw);
  }catch{return json(res,{error:'请求格式错误'},400);}
  if(!body||typeof body!=='object'||!OPS.has(body.operation))return json(res,{error:'不支持的操作'},400);
  if(body.payload!=null&&(typeof body.payload!=='object'||Array.isArray(body.payload)))return json(res,{error:'材料格式错误'},400);
  const supplied=String(req.headers.cookie||'').split(';').map(x=>x.trim()).find(x=>x.startsWith(COOKIE+'='))?.slice(COOKIE.length+1);
  const guest=supplied&&/^[a-f0-9]{64}$/.test(supplied)?supplied:randomBytes(32).toString('hex');
  // Never accept caller-supplied owner/account headers. Each browser has its own opaque identity.
  const owner=createHash('sha256').update('coze-web-visitor:'+guest).digest('hex');
  try{
   const r=await fetch(upstream+'/dispatch',{
    method:'POST',redirect:'error',
    headers:{Authorization:`Bearer ${apiToken}`,'Content-Type':'application/json','X-Mengniu-Visitor':owner},
    body:JSON.stringify({operation:body.operation,payload:body.payload||{}}),
    signal:AbortSignal.timeout(body.operation==='analyze'?640000:50000),
   });
   if(!r.headers.get('content-type')?.includes('json'))return json(res,{error:'正式服务返回异常，未确认保存成功。'},502,guest);
   const data=await r.json();
   return json(res,r.ok?data:{error:data.detail||data.error||'服务处理失败，未确认保存成功'},r.status,guest);
  }catch{return json(res,{error:'请求连接中断。请先点击“刷新”检查保存状态，不要立即重复提交分析。'},504,guest);}
 });
}

if(process.argv[1]&&pathToFileURL(process.argv[1]).href===import.meta.url){
 const port=Number(process.env.PORT||process.env.DEPLOY_RUN_PORT||5000);
 const server=makeServer();server.requestTimeout=660000;server.headersTimeout=60000;
 server.listen(port,'0.0.0.0',()=>console.log(`Mengniu public web ready on port ${port}; startup model calls: 0`));
}
