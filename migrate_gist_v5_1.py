"""Offline-only, idempotent importer. Never imported by the production bot.
Run with server-side env configured; optionally provide JSON backup paths.
Gist failures stop only this importer, without modifying production runtime.
"""
import os,json,sys,time
from pathlib import Path
from urllib.parse import urlparse
import requests
import storage_v5_1 as db

def fetch_legacy():
    token=os.environ['GITHUB_TOKEN'];gist=os.environ['OUTLET_GIST_ID']
    headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'}
    r=requests.get('https://api.github.com/gists/'+gist,headers=headers,timeout=30)
    if not r.ok:
        print('Gist unavailable; HTTP',r.status_code,'Retry migration later. Production unaffected.')
        raise SystemExit(2)
    snapshot=r.json();rows=[]
    for name,item in snapshot.get('files',{}).items():
        if not name.startswith('sgsb_records_') or not name.endswith('.json'):continue
        url=item['raw_url']
        if urlparse(url).hostname!='gist.githubusercontent.com':raise RuntimeError('Unexpected raw Gist host')
        raw=requests.get(url,headers=headers,timeout=60);raw.raise_for_status()
        data=raw.json();rows.extend(data.get('records',[]) if isinstance(data,dict) else data)
    return rows

def migrate(rows):
    # Existing rows always win; importing never overwrites new production media.
    existing={r['rid']:r for r in db.rows()}; pending={r['rid']:r for r in rows if r['rid'] not in existing}
    inserted=0;conflicts=0
    batch=list(pending.values())
    for pos in range(0,len(batch),100):
        group=batch[pos:pos+100]
        try:
            db.api('sgsb_records','POST',[{'rid':r['rid'],'data':r} for r in group],params={'on_conflict':'rid'},prefer='resolution=ignore-duplicates,return=minimal')
            inserted+=len(group)
        except db.DuplicateRecord:
            for row in group:
                try:
                    db.api('sgsb_records','POST',{'rid':row['rid'],'data':row},params={'on_conflict':'rid'},prefer='resolution=ignore-duplicates,return=minimal');inserted+=1
                except db.DuplicateRecord:
                    db.api('sgsb_migration_conflicts','POST',{'rid':row['rid'],'data':row,'reason':'Existing Outlet + Seal + Date; preserved for review'},params={'on_conflict':'rid'},prefer='resolution=ignore-duplicates,return=minimal');conflicts+=1
        print('Migration progress:',min(pos+100,len(batch)),'/',len(batch),flush=True)
    stored={r['rid']:r for r in db.rows()}
    verified=sum(1 for r in rows if r['rid'] in stored)
    sample=[]
    for row in rows[::max(1,len(rows)//10)]:
        got=stored.get(row['rid'])
        if got:sample.append({'rid':row['rid'],'match':all(got.get(k)==row.get(k) for k in ['outlet','seal','bagging_date','photo','video','status'])})
    return {'legacy_records':len(rows),'inserted':inserted,'already_present':len(rows)-len(pending),'preserved_conflicts':conflicts,'verified_rids':verified,'sample':sample}

if __name__=='__main__':
    rows=[]
    if len(sys.argv)>1:
        for filename in sys.argv[1:]:
            data=json.loads(Path(filename).read_text());rows.extend(data.get('records',[]) if isinstance(data,dict) else data)
    else:rows=fetch_legacy()
    result=migrate(rows);print(json.dumps(result,indent=2))
