import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import {createServer} from 'node:http';

test('standalone backend address can be configured without contacting production', async t => {
  const realFetch=globalThis.fetch;
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    assert.ok(String(url).startsWith('http://127.0.0.1:'),'test refuses any external request');
    return realFetch(url,options);
  });
  const upstream=createServer((req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ready:true,model_called:false}));});
  await new Promise(r=>upstream.listen(0,'127.0.0.1',r));
  const previous=process.env.MENGNIU_AGENT_UPSTREAM_URL;
  process.env.MENGNIU_AGENT_UPSTREAM_URL=`http://127.0.0.1:${upstream.address().port}/visitor/v1`;
  const {makeServer}=await import('./server.mjs');
  const app=makeServer({apiToken:'test-only-not-live'});
  await new Promise(r=>app.listen(0,'127.0.0.1',r));
  t.after(()=>{if(previous===undefined)delete process.env.MENGNIU_AGENT_UPSTREAM_URL;else process.env.MENGNIU_AGENT_UPSTREAM_URL=previous;app.closeAllConnections();app.close();upstream.closeAllConnections();upstream.close();});
  const r=await fetch(`http://127.0.0.1:${app.address().port}/api/bridge`);
  assert.equal(r.status,200,'configured standalone health endpoint must be used');
  assert.equal((await r.json()).model_called,false);
});

test('Coze bridge exists and preserves the public contract without model calls', async t => {
  const exists = await fs.access(new URL('./server.mjs',import.meta.url)).then(()=>true,()=>false);
  assert.ok(exists,'Coze-compatible HTTP bridge has not been implemented');
  const {makeServer} = await import('./server.mjs');
  const requests=[];
  const upstream=createServer(async(req,res)=>{
    let raw='';for await(const b of req)raw+=b;
    requests.push({path:req.url,headers:req.headers,body:raw?JSON.parse(raw):null});
    res.setHeader('Content-Type','application/json');
    res.end(JSON.stringify(req.url.endsWith('/health')?{ready:true,model_called:false}:{task_id:'T-001',inputs:[{input_id:'IN-001',raw_text:'仅测试保存'}]}));
  });
  await new Promise(r=>upstream.listen(0,'127.0.0.1',r));
  const app=makeServer({apiToken:'unit-test-only-not-a-real-token',upstream:`http://127.0.0.1:${upstream.address().port}/visitor/v1`});
  await new Promise(r=>app.listen(0,'127.0.0.1',r));
  t.after(()=>{app.closeAllConnections();app.close();upstream.closeAllConnections();upstream.close();});
  const base=`http://127.0.0.1:${app.address().port}`;
  const post=(body,extra={})=>fetch(base+'/api/bridge',{method:'POST',headers:{Origin:base,'Content-Type':'application/json',...extra},body:typeof body==='string'?body:JSON.stringify(body)});
  let r=await fetch(base+'/');assert.equal(r.status,200);assert.match(await r.text(),/四种捕手|消费者捕手/);
  r=await fetch(base+'/server.mjs');assert.equal(r.status,404);
  r=await fetch(base+'/api/bridge');assert.equal((await r.json()).ready,true);assert.equal(requests.at(-1).headers.authorization,'Bearer unit-test-only-not-a-real-token');
  r=await post({operation:'input',payload:{raw_text:'仅测试保存'}},{'X-Mengniu-Visitor':'f'.repeat(64),'oai-authenticated-user-id':'spoofed'});
  assert.equal(r.status,200);assert.equal((await r.json()).inputs[0].raw_text,'仅测试保存');
  const cookie=r.headers.get('set-cookie');assert.match(cookie,/HttpOnly/);assert.match(cookie,/Secure/);assert.match(cookie,/SameSite=Strict/);
  assert.ok(!cookie.includes('unit-test-only'));
  const owner=requests.at(-1).headers['x-mengniu-visitor'];assert.match(owner,/^[a-f0-9]{64}$/);assert.notEqual(owner,'f'.repeat(64));
  await post({operation:'state'},{Cookie:cookie.split(';')[0]});assert.equal(requests.at(-1).headers['x-mengniu-visitor'],owner);
  await post({operation:'state'});assert.notEqual(requests.at(-1).headers['x-mengniu-visitor'],owner);
  const n=requests.length;
  r=await post({operation:'state'},{Origin:'https://foreign.invalid'});assert.equal(r.status,403);
  r=await post({operation:'delete_all'});assert.equal(r.status,400);
  r=await post('{bad');assert.equal(r.status,400);
  r=await post({operation:'state',padding:'x'.repeat(100001)});assert.equal(r.status,413);
  assert.equal(requests.length,n,'invalid requests must not reach backend');
  assert.ok(requests.every(q=>q.body?.operation!=='analyze'),'no analysis is executed by this test');
});
