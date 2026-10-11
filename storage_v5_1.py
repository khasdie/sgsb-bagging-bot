"""Server-only Supabase storage. No Gist or local persistence fallback."""
import os
import requests
from collections.abc import MutableMapping
from datetime import datetime, timezone, timedelta

FRIENDLY = 'Database sementara tidak tersedia. Sila cuba semula; jangan padam mesej gambar/video atau Record Card.'
class StorageError(RuntimeError):
    pass
class DuplicateRecord(StorageError):
    pass

def api(path, method='GET', data=None, params=None, prefer=None):
    url=os.environ.get('SUPABASE_URL','').rstrip('/')
    key=os.environ.get('SUPABASE_SECRET_KEY','')
    if not url or not key: raise StorageError(FRIENDLY)
    headers={'apikey':key, 'Content-Type':'application/json'}
    if prefer: headers['Prefer']=prefer
    try:
        r=requests.request(method,url+'/rest/v1/'+path,headers=headers,json=data,params=params,timeout=25)
        if r.status_code==409: raise DuplicateRecord('Rekod Outlet + Seal + Tarikh sudah wujud.')
        if not r.ok:
            print('Supabase request failed:',path.split('?')[0],r.status_code,flush=True)
            raise StorageError(FRIENDLY)
        return r.json() if r.content else None
    except requests.RequestException:
        raise StorageError(FRIENDLY) from None

def rpc(name, data): return api('rpc/'+name,'POST',data)
def rows(params=None):
    params=dict(params or {}); params.update(select='data',order='bagging_date.asc,rid.asc')
    result=[]; offset=0
    while True:
        page=api('sgsb_records',params={**params,'offset':offset,'limit':500})
        result.extend(x['data'] for x in page)
        if len(page)<500: return result
        offset+=len(page)

def save(record):
    api('sgsb_records','POST',{'rid':record['rid'],'data':record},params={'on_conflict':'rid'},prefer='resolution=merge-duplicates,return=minimal')

def get_record(rid):
    result=api('sgsb_records',params={'rid':'eq.'+str(rid),'select':'data','limit':1})
    return result[0]['data'] if result else None

class RecordMap(MutableMapping):
    def __getitem__(self,key):
        value=get_record(key)
        if value is None: raise KeyError(key)
        return value
    def __setitem__(self,key,value): save(value)
    def __delitem__(self,key): pass  # runtime cleanup never deletes durable records
    def __iter__(self): return iter([r['rid'] for r in rows()])
    def __len__(self): return len(rows())
    def values(self): return rows()
    def items(self): return [(r['rid'],r) for r in rows()]
    def pop(self,key,default=None): return default

class SessionMap(MutableMapping):
    def __init__(self,name): self.name=name
    def _key(self,key): return self.name+':'+str(key)
    def __getitem__(self,key):
        result=api('sgsb_sessions',params={'key':'eq.'+self._key(key),'select':'data,updated_at'})
        if not result: raise KeyError(key)
        row=result[0]
        if self.name=='admin' and datetime.now(timezone.utc)-datetime.fromisoformat(row['updated_at'])>timedelta(hours=8):
            raise KeyError(key)
        return row['data']
    def __setitem__(self,key,value):
        api('sgsb_sessions','POST',{'key':self._key(key),'data':value,'updated_at':datetime.now(timezone.utc).isoformat()},params={'on_conflict':'key'},prefer='resolution=merge-duplicates,return=minimal')
    def __delitem__(self,key): api('sgsb_sessions','DELETE',params={'key':'eq.'+self._key(key)})
    def __iter__(self): return iter([])
    def __len__(self): return 0

class PersistentDict(dict):
    def __init__(self,value,save_callback):
        super().__init__(value); self.save_callback=save_callback
    def __setitem__(self,key,value):
        super().__setitem__(key,value); self.save_callback(dict(self))
    def pop(self,key,default=None):
        value=super().pop(key,default); self.save_callback(dict(self)); return value

_session_read=SessionMap.__getitem__
def _persistent_session_read(self,key):
    return PersistentDict(_session_read(self,key), lambda value:self.__setitem__(key,value))
SessionMap.__getitem__=_persistent_session_read
