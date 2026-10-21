import copy
import os
import unittest
from unittest.mock import patch
from flask import Flask
from fleet_bot import FleetBot

class FakeDB:
    def __init__(self): self.rows={};self.events={};self.sessions={'fleet_access':{7:{'outlets':['SBH307']},8:{'outlets':['SBH307']}}};self.reads=[]
    def SessionMap(self,name): return self.sessions.setdefault(name,{})
    def api(self,table,params):
        self.reads.append(table)
        if table=='sgsb_records': return [{'outlet':'SBH307'}]
        if table=='sgsb_fleet_events':
            event=params['event_id'][3:];return [{'result':self.events[event]}] if event in self.events else []
        if table=='sgsb_fleet_reports':
            if 'id' in params:
                rid=params['id'][3:];return [copy.deepcopy(self.rows[rid])] if rid in self.rows else []
            return [{'data':copy.deepcopy(r['data'])} for r in self.rows.values() if r['data']['state']=='SENT']
        raise AssertionError('Unexpected table '+table)
    def rpc(self,name,args):
        if name=='sgsb_fleet_read': return [{'data':copy.deepcopy(r['data'])} for r in self.rows.values() if r['data']['state']=='SENT' and r['data']['outletCode'] in args['p_codes'] and args['p_from']<=r['data']['reportDate']<=args['p_to']]
        assert name=='sgsb_fleet_cas'
        rid=args['p_id'];old=self.rows.get(rid)
        assert (old['version'] if old else 0)==args['p_expected']
        row={'version':args['p_expected']+1,'data':copy.deepcopy(args['p_data'])}
        self.rows[rid]=row;self.events[args['p_event']]=row
        return copy.deepcopy(row)

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'FLEET_ENABLED':'1','FLEET_ARCHIVE_CHAT_ID':'-1001234567890','ARCHIVE_CHAT_ID':'-1009876543210','FLEET_BTM_READ_TOKEN':'s'*43})
        self.env.start();self.addCleanup(self.env.stop)
        self.db=FakeDB();self.sent=[];self.ambiguous=False
        def telegram(method,payload):
            self.sent.append((method,copy.deepcopy(payload)))
            if method=='getChat': return {'ok':True,'result':{'id':-1001234567890,'type':'channel','title':'ILLUSTRATION FLEET','username':'SgsbFleet'}}
            if method=='getMe': return {'ok':True,'result':{'id':1234,'username':'SyntheticBot'}}
            if method=='getChatMember': return {'ok':True,'result':{'status':'administrator','can_post_messages':True}}
            if method=='sendPhoto' and self.ambiguous: return {'ok':False,'ambiguous':True}
            return {'ok':True,'result':{'message_id':42}}
        self.bot=FleetBot(self.db,telegram,{})
        self.seq=0
    def msg(self,text='',photo=False,rid=None,user=7):
        self.seq+=1;m={'chat':{'id':user,'type':'private'},'from':{'id':user},'text':text}
        if photo: m['photo']=[{'file_id':'synthetic-private','file_unique_id':'same','file_size':100}]
        if rid: m['reply_to_message']={'text':'FLEET REPORT\nRecord: '+rid}
        return {'update_id':self.seq,'message':m}
    def cb(self,data,user=7):
        self.seq+=1;return {'update_id':self.seq,'callback_query':{'id':str(self.seq),'from':{'id':user},'data':data,'message':{'chat':{'id':user,'type':'private'}}}}
    def draft(self):
        self.assertTrue(self.bot.handle(self.msg('/fleet')))
        self.bot.handle(self.cb('fl:outlet:SBH307'));self.bot.handle(self.cb('fl:date:today'))
        rid=next(iter(self.db.rows));self.bot.handle(self.cb('fl:OFFLOAD:'+rid));self.bot.handle(self.msg(photo=True,rid=rid));self.bot.handle(self.msg('ILLUSTRATION',rid=rid));return rid
    def test_end_to_end_photo_remark_confirm_and_replay_archive_once(self):
        rid=self.draft();self.bot.handle(self.msg(photo=True,rid=rid));event=self.cb('fl:confirm:'+rid)
        self.bot.handle(event);self.bot.handle(event);self.bot.handle(self.cb('fl:confirm:'+rid))
        r=self.db.rows[rid]['data'];self.assertEqual(r['state'],'SENT');self.assertEqual(len(r['photos']),1)
        self.assertEqual(r['remark'],'ILLUSTRATION');self.assertEqual(sum(m=='sendPhoto' for m,p in self.sent),1)
    def test_timeout_is_review_no_duplicate_retry(self):
        rid=self.draft();self.ambiguous=True;self.bot.handle(self.cb('fl:confirm:'+rid));self.bot.handle(self.cb('fl:confirm:'+rid))
        self.assertEqual(self.db.rows[rid]['data']['state'],'UNKNOWN');self.assertEqual(sum(m=='sendPhoto' for m,p in self.sent),1)
    def test_wrong_owner_cannot_change_and_bagging_media_never_intercepted(self):
        rid=self.draft();before=copy.deepcopy(self.db.rows)
        self.bot.handle(self.cb('fl:CLEAR:'+rid,user=8));self.assertEqual(self.db.rows,before)
        bagging=self.msg(photo=True);bagging['message']['reply_to_message']={'text':'Original BAGGING card'}
        self.db.reads=[];self.assertFalse(self.bot.handle(bagging));self.assertEqual(self.db.reads,[])
        self.assertFalse(self.bot.handle(self.cb('outlet:SBH307')));self.assertEqual(self.db.reads,[])
    def test_metadata_http_auth_projection_and_media_scope(self):
        rid=self.draft();self.bot.handle(self.cb('fl:confirm:'+rid));app=Flask(__name__);app.add_url_rule('/btm/fleet',view_func=self.bot.read_http)
        client=app.test_client();self.db.reads=[]
        self.assertEqual(client.get('/btm/fleet').status_code,401);self.assertEqual(self.db.reads,[])
        header={'x-btm-fleet-key':'s'*43};day=self.db.rows[rid]['data']['reportDate']
        r=client.get('/btm/fleet?outlets=SBH307&dateFrom='+day+'&dateTo='+day,headers=header)
        self.assertEqual(r.status_code,200);self.assertNotIn('synthetic-private',r.get_data(as_text=True));self.assertEqual(r.json['data']['records'][0]['photoCount'],1)
        self.assertEqual(client.get('/btm/fleet?outlets=SBH306&recordId='+rid,headers=header).status_code,404)
        self.assertEqual(client.get('/btm/fleet',headers={**header,'Origin':'https://external.invalid'}).status_code,401)
    def test_disabled_feature_does_not_change_bagging_routes(self):
        with patch.dict(os.environ,{'FLEET_ENABLED':'0'}):
            self.assertFalse(self.bot.handle(self.msg('/start')));self.assertEqual(self.db.reads,[])
            self.assertTrue(self.bot.handle(self.msg('/fleet')));self.assertEqual(self.db.rows,{})
    def test_unassigned_fleet_photo_retained_until_owner_selection_no_reupload(self):
        rid=self.draft();p=self.msg(photo=True);p['message']['photo'][0]['file_unique_id']='retained'
        self.bot.handle(p);self.assertEqual(len(self.db.rows[rid]['data']['photos']),1)
        pending=self.db.sessions['fleet_media'];token=next(iter(pending))
        self.bot.handle(self.cb('fl:attach:'+rid+':'+token,user=8));self.assertEqual(len(self.db.rows[rid]['data']['photos']),1)
        self.bot.handle(self.cb('fl:attach:'+rid+':'+token));self.assertEqual(len(self.db.rows[rid]['data']['photos']),2)
        self.assertNotIn(token,pending)
    def test_later_album_photo_uses_durable_fleet_binding_not_bagging(self):
        rid=self.draft();first=self.msg(photo=True,rid=rid);first['message']['media_group_id']='synthetic-album'
        self.bot.handle(first)
        later=self.msg(photo=True);later['message']['media_group_id']='synthetic-album';later['message']['photo'][0]['file_unique_id']='second'
        self.assertTrue(self.bot.handle(later));self.assertEqual(len(self.db.rows[rid]['data']['photos']),2)
    def test_historical_bagging_rows_never_grant_new_fleet_access(self):
        self.assertEqual(self.bot.allowed(99),[])
        self.bot.handle(self.cb('fl:outlet:SBH307',user=99));self.assertEqual(self.db.rows,{})
        self.bot.handle(self.msg('/fleet_access 99 SBH307'));self.assertEqual(self.bot.allowed(99),[])

    def test_unassigned_user_can_identify_self_without_grant_or_other_user_data(self):
        self.bot.handle(self.msg('/fleet',user=99))
        self.assertIn('Your Telegram ID: <code>99</code>',self.sent[-1][1]['text'])
        self.assertEqual(self.bot.allowed(99),[]);self.assertEqual(self.db.rows,{})
        self.bot.handle(self.msg('/fleet_id',user=99))
        self.assertIn('<code>99</code>',self.sent[-1][1]['text'])
        self.assertEqual(self.bot.allowed(99),[])

    def test_existing_authenticated_admin_uses_original_access_and_expiry_denies(self):
        self.bot.admin_sessions[99]={'authenticated':True}
        self.bot.handle(self.msg('/fleet',user=99))
        self.assertIn('fl:quick:SBH307',str(self.sent[-1]))
        self.bot.admin_sessions.pop(99)
        self.assertEqual(self.bot.allowed(99),[])
    def test_channel_must_differ_from_bagging_and_require_bot_post_permission(self):
        self.assertTrue(self.bot.enabled())
        with patch.dict(os.environ,{'ARCHIVE_CHAT_ID':'-1001234567890'}):
            self.bot.channel_cache=None;self.assertFalse(self.bot.enabled())
        with patch.dict(os.environ,{'FLEET_ARCHIVE_CHAT_ID':'@SgsbFleet'}):
            self.bot.channel_cache=None;self.assertTrue(self.bot.enabled())
        self.bot.channel_cache=None;original=self.bot.telegram
        self.bot.telegram=lambda m,p:{'ok':True,'result':{'status':'member'}} if m=='getChatMember' else original(m,p)
        self.assertFalse(self.bot.enabled())

    def test_quick_today_flow_guides_photo_and_next_outlet_preserves_history(self):
        self.bot.handle(self.msg('/start fleet'))
        self.assertTrue(any('fl:quick:SBH307' in str(p) for m,p in self.sent))
        self.bot.handle(self.cb('fl:quick:SBH307'));rid=next(iter(self.db.rows))
        self.bot.handle(self.cb('fl:CLEAR:'+rid))
        self.assertTrue(any(p.get('reply_markup',{}).get('force_reply') for m,p in self.sent))
        self.bot.handle(self.msg(photo=True,rid=rid));self.bot.handle(self.cb('fl:confirm:'+rid))
        self.assertTrue(any('fl:next' in str(p) for m,p in self.sent))
        self.bot.handle(self.cb('fl:next'));self.bot.handle(self.cb('fl:quick:SBH307'))
        self.assertEqual(len(self.db.rows),2)
        self.assertEqual(self.db.rows[rid]['data']['state'],'SENT')
        self.assertEqual(sum(m=='sendPhoto' for m,p in self.sent),1)

    def test_quick_selection_cannot_overwrite_existing_draft_or_grant_outlet(self):
        rid=self.draft();before=copy.deepcopy(self.db.rows)
        self.bot.handle(self.cb('fl:quick:SBH306'));self.assertEqual(before,self.db.rows)
        self.bot.handle(self.cb('fl:quick:SBH307'));self.assertEqual(before,self.db.rows)
        self.bot.handle(self.cb('fl:backdated'))
        self.assertTrue(any('fl:outlet:SBH307' in str(p) for m,p in self.sent))

    def test_channel_launcher_claim_pin_and_restarts_send_once(self):
        storage={};original=self.db.api
        def api(table,method='GET',data=None,params=None,prefer=None):
            if table!='sgsb_sessions': return original(table,params)
            if method=='GET': return [{'data':storage[params['key'][3:]]}] if params['key'][3:] in storage else []
            if method=='POST':
                if data['key'] in storage: return []
                storage[data['key']]=data['data'];return [data]
            storage[params['key'][3:]]=data['data'];return None
        self.db.api=api
        self.assertTrue(self.bot.publish_launcher());self.assertTrue(self.bot.publish_launcher())
        posts=[p for m,p in self.sent if m=='sendMessage' and str(p['chat_id']).startswith('-100')]
        self.assertEqual(len(posts),1);self.assertIn('?start=fleet',str(posts))
        self.assertTrue(next(iter(storage.values()))['pinned'])

    def test_ambiguous_launcher_is_not_posted_again(self):
        storage={};original=self.db.api
        def api(table,method='GET',data=None,params=None,prefer=None):
            if method=='GET': return [{'data':v} for v in storage.values()]
            if method=='POST': storage[data['key']]=data['data'];return [data]
            storage[params['key'][3:]]=data['data'];return None
        self.db.api=api;telegram=self.bot.telegram
        self.bot.telegram=lambda m,p: {'ok':False,'ambiguous':True} if m=='sendMessage' else telegram(m,p)
        self.assertFalse(self.bot.publish_launcher());self.assertFalse(self.bot.publish_launcher())
        self.assertEqual(next(iter(storage.values()))['state'],'UNKNOWN')

if __name__=='__main__': unittest.main()
