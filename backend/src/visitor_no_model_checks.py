"""Isolated deployment checks. Never calls an LLM or alters an archived case."""
import asyncio, hashlib, json, os, subprocess, sys, uuid
from pathlib import Path

root=Path(os.getenv('COZE_WORKSPACE_PATH','/workspace/projects'))
def case_hashes():
 return {str(f.relative_to(root)):hashlib.sha256(f.read_bytes()).hexdigest() for tid in ('T-016','T-017','T-018') for f in (root/'assets/opportunity'/tid).rglob('*') if f.is_file()}
before=case_hashes()
events=[]
def check(name,passed):
 assert passed,name
 events.append({'check':name,'passed':True})
def run(bundle,op,payload=None):
 p=subprocess.run([sys.executable,str(root/'src/visitor_worker.py')],input=json.dumps({'operation':op,'payload':payload or {},'bundle':bundle},ensure_ascii=False),text=True,capture_output=True,timeout=45)
 if p.returncode:raise RuntimeError(p.stderr[-1800:])
 return json.loads(p.stdout)
health=run({},'health')
check('原规则 v1.4 与业务资料可读取，不调用模型',health.get('error') is None and health['result']['model_called'] is False)
a=run({},'start',{'mode':'消费者捕手','scope':'仅模拟','simulation':True})
check('访客 A 创建独立任务',not a['error'] and a['result']['task_id']=='T-001')
a=run(a['bundle'],'input',{'raw_text':'部署隔离自测：仅测试登记与保存，不作为消费者证据，不运行模型。','material_kind':'simulation','simulation':True})
check('访客 A 登记输入',len(a['result']['inputs'])==1)
b=run({},'start',{'mode':'热点捕手'})
check('访客 B 不继承 A 的材料',b['result']['inputs']==[])
bad=run(b['bundle'],'activate',{'task_id':'T-016'})
check('访客不能激活原归档任务',bool(bad['error']))
reloaded=run(a['bundle'],'state')
check('从持久化快照恢复完整输入',reloaded['result']['inputs']==a['result']['inputs'])
old_inputs=a['bundle']['T-001/inputs.json']
a2=run(a['bundle'],'start',{'mode':'企业灵感捕手'})
check('开始新任务清空临时输入并保留旧任务',a2['result']['task_id']=='T-002' and not a2['result']['inputs'] and a2['bundle']['T-001/inputs.json']==old_inputs)
check('新任务仍可读取公共资料',len(a2['result']['materials']['sources'])==9)
sw=run(a2['bundle'],'switch_mode',{'mode':'市场缺口捕手'})
check('四入口可选择且切换不新建任务',sw['result']['task_id']=='T-002' and sw['result']['active_task']['mode']=='市场缺口捕手')
empty_analysis=run(b['bundle'],'analyze')
check('空材料分析在模型调用前拒绝',bool(empty_analysis['error']))

# Exercise only the dedicated visitor table. Delete only rows created here.
sys.path.insert(0,str(root/'src'))
import visitor_api as bridge
from sqlalchemy import text
bridge.migrate()
owners=[hashlib.sha256(('deploy-check-'+uuid.uuid4().hex).encode()).hexdigest() for _ in range(2)]
try:
 row,token=bridge.lock_session(owners[0],'start')
 bridge.finish(owners[0],token,a,'start')
 restored,_=bridge.lock_session(owners[0],'state')
 check('PostgreSQL 保存后重读一致',restored['bundle']==a['bundle'])
 other,_=bridge.lock_session(owners[1],'state')
 check('数据库按访客隔离',other['bundle']=={})
 _,lock=bridge.lock_session(owners[0],'input')
 blocked=False
 try:bridge.lock_session(owners[0],'start')
 except Exception as e:blocked=getattr(e,'status_code',0)==409
 check('并发写入被阻止',blocked)
 bridge.release(owners[0],lock,'无模型自测完成')
finally:
 with bridge.engine().begin() as conn:
  for owner in owners:
   conn.execute(text('DELETE FROM mengniu_visitor_sessions_v1 WHERE owner_id=:owner'),{'owner':owner})

after=case_hashes()
check('三个原案例全部文件校验值保持一致',before==after)
report={'model_calls':0,'results':events,'rules':health['result'],'archived_file_hashes_before':before,'archived_file_hashes_after':after,'test_data':'仅独立临时工作目录和自建访客测试行；未写原机会卡目录'}
path=root/'visitor_no_model_report.json'
path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'checks_passed':len(events),'model_calls':0,'archived_cases_unchanged':before==after,'report':str(path)},ensure_ascii=False))
