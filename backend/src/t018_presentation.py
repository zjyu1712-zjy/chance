"""T-018 read-only presentation. Existing input/card text stays unchanged."""
import json,re,hashlib
from pathlib import Path


def is_review(card):
    return bool(card and card.get('task_id')=='T-018' and card.get('card_id')=='OC-001' and card.get('status')=='暂不形成机会' and card.get('version',0)>=5)


def build_review(card,inputs):
    if not is_review(card): return None
    raw=next((x.get('raw_text','') for x in inputs if x.get('input_id')=='IN-001'),'')
    sections={}
    for tag in ('C3-S01','C3-S02','C3-S03'):
        match=re.search(r'【'+tag+r'[^】]*】\n(.*?)(?=\n【|\Z)',raw,re.S)
        block=match.group(1) if match else ''
        def field(name):
            m=re.search(r'^'+re.escape(name)+r'：(.+)$',block,re.M)
            return m.group(1).strip() if m else ''
        sections[tag]={'source_id':tag,'publisher':field('发布方'),'date':field('发布日期'),'url':field('链接'),'source_excerpt':field('事实摘要'),'kind':field('材料性质')}
    sections['I-C3-01']={'source_id':'I-C3-01','publisher':'参赛团队研究设想','date':'未单独记录','url':'','kind':'研究设想，不是消费者证据或蒙牛内部研发项目'}
    ev=[]
    for e in card.get('support_evidence',[]):
        s=e.get('summary','')
        tag=('C3-S01' if 'C3-P01' in s else 'C3-S02' if 'C3-P02' in s else 'C3-S03' if '英敏特' in s else 'I-C3-01' if '参赛团队' in s else '')
        ev.append(dict(sections.get(tag,{}),summary=s,limitation=e.get('limitation',''),locator=e.get('record_id','')+'|'+e.get('field_path','')))
    judgments=[]
    labels=[('demand_support','新增需求依据','本轮未取得直接需求证据'),('supply_gap','双吃形式的已有供给','已有产品覆盖该形式'),('verify_value','新增方向的验证价值','当前不建议仅以双吃形式进入新品验证')]
    for k,label,result in labels:
        tc=card.get('three_conditions',{}).get(k,{})
        judgments.append({'label':label,'result':result,'note':tc.get('note',''),'original_degree':tc.get('degree'),'key':k})
    products=[]
    for p in card.get('comparison',[]):
        tag={'C3-P01':'C3-S01','C3-P02':'C3-S02'}.get(p.get('product_id'))
        products.append(dict(p,source=sections.get(tag,{}),locator=p.get('evidence','')))
    return {'task_id':'T-018','card_id':card['card_id'],'version':card['version'],'status':card['status'],'summary':card.get('summary_conclusion',card.get('demand_hypothesis','')),'reason':card.get('未形成机会的原因',''),'reevaluate':card.get('后续重新评估条件',''),'scope':card.get('input_scope_summary',''),'sale':card.get('sale_display',''),'evidence':ev,'judgments':judgments,'products':products}


def exact_diff(prev,cur,path=''):
    """Index-preserving, complete field diff; never merges four IN-001 references."""
    if prev==cur: return []
    if isinstance(prev,dict) and isinstance(cur,dict):
        out=[]
        for k in sorted(set(prev)|set(cur)):
            # change_log is redundant version metadata, not a business field.
            if not path and k in ('change_log','host_meta','updated_at','version','created_at'): continue
            out+=exact_diff(prev.get(k),cur.get(k),f'{path}.{k}' if path else k)
        return out
    if isinstance(prev,list) and isinstance(cur,list) and len(prev)==len(cur):
        return [r for i,(a,b) in enumerate(zip(prev,cur)) for r in exact_diff(a,b,f'{path}[{i}]')]
    return [{'field':path,'from':prev,'to':cur,'note':'根据已保存版本逐字段读取比较；不是模型运行日志'}]


def render_review_text(v):
    if not v:return ''
    out=[f"## 构想核查结果｜{v['task_id']} {v['card_id']} v{v['version']}",'本展示据已保存卡片整理；经人工复核修订，非模型原始回复。','业务状态：'+v['status'],v['summary'],'未形成机会的原因：'+v['reason'],'后续重新评估条件：'+v['reevaluate'],'材料范围：'+v['scope'],'在售情况：'+v['sale'],'\n### 核查依据']
    for e in v['evidence']:
        out += [f"{e.get('source_id','未匹配')}｜{e['locator']}：{e['summary']}",f"发布方：{e.get('publisher','')}｜日期：{e.get('date','')}｜性质：{e.get('kind','')}",'链接：'+(e.get('url') or '不适用'),'局限：'+e['limitation']]
    out+=['\n### 核查摘要（人工复核展示，原始程度另存）']
    for j in v['judgments']: out += [j['label']+'：'+j['result'],j['note']]
    out+=['\n### 已有产品与构想对照']
    for p in v['products']:
        out += [p.get('product_name','')+'｜'+p.get('product_id',''),p.get('form_verified',''),p.get('match_result',''),p.get('review_scope',''),'来源：'+p['source'].get('source_id','')+'｜'+p['locator']+'｜'+p['source'].get('url','')]
    return '\n'.join(out)


def read_reply_archive(base):
    root=Path(base)/'assets/opportunity/T-018'
    text_path=root/'raw_reply_v1.txt';meta_path=root/'raw_reply_v1.meta.json'
    if not(text_path.is_file() and meta_path.is_file()):return {'exists':False,'reason':'尚无完整回复及补存溯源记录'}
    data=text_path.read_bytes();meta=json.loads(meta_path.read_text(encoding='utf-8'))
    if hashlib.sha256(data).hexdigest()!=meta.get('sha256'):return {'exists':False,'reason':'原文校验值与归档记录不一致'}
    return dict(meta,exists=True,content=data.decode('utf-8'))
