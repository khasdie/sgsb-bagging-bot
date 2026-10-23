import ast
import copy
import os
import types
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from report_menu import ReportMenu

class MenuTests(unittest.TestCase):
    def setUp(self):
        self.sent=[];self.bagging=[];self.fleet=[];self.storage={};self.posts=0
        self.bot=types.SimpleNamespace(start=lambda *a:self.fleet.append(a))
        self.menu=ReportMenu(types.SimpleNamespace(api=self.api),self.telegram,self.bot,lambda *a:self.bagging.append(a))
    def api(self,table,method='GET',data=None,params=None,prefer=None):
        self.assertEqual(table,'sgsb_sessions')
        if method=='GET': return [{'data':copy.deepcopy(self.storage[params['key'][3:]])}] if params['key'][3:] in self.storage else []
        if method=='POST':
            if data['key'] in self.storage: return []
            self.storage[data['key']]=copy.deepcopy(data['data']);return [data]
        self.storage[params['key'][3:]]=copy.deepcopy(data['data'])
    def telegram(self,method,payload):
        self.sent.append((method,copy.deepcopy(payload)))
        if method=='getMe': return {'ok':True,'result':{'id':1,'username':'SyntheticBot'}}
        if method=='getChat': return {'ok':True,'result':{'id':int(payload['chat_id']),'type':'channel'}}
        if method=='getChatMember': return {'ok':True,'result':{'status':'administrator','can_post_messages':True}}
        self.posts+=method=='sendMessage'
        return {'ok':True,'result':{'message_id':10+self.posts}}
    def msg(self,text,**extra):
        return {'update_id':5,'message':{'text':text,'from':{'id':7},'chat':{'id':7,'type':'private'},**extra}}
    def cb(self,data):
        return {'update_id':6,'callback_query':{'id':'cb','data':data,'from':{'id':7},'message':{'chat':{'id':7,'type':'private'}}}}
    def test_start_and_menu_show_two_options_without_writes_or_route_changes(self):
        for text in ('/start','/menu','/start reports'):
            self.assertTrue(self.menu.handle(self.msg(text)))
            buttons=self.sent[-1][1]['reply_markup']['inline_keyboard']
            self.assertEqual([row[0]['text'] for row in buttons],['FLEET Report','Video Bagging Report'])
        self.assertEqual(self.storage,{});self.assertEqual(self.fleet,[]);self.assertEqual(self.bagging,[])
    def test_each_choice_dispatches_original_flow_with_real_actor(self):
        self.menu.handle(self.cb('reports:fleet'));self.assertEqual(self.fleet,[(7,7,6)]);self.assertEqual(self.bagging,[])
        self.menu.handle(self.cb('reports:bagging'));self.assertEqual(self.bagging,[(7,7)])
        self.menu.handle(self.msg('/start bagging'));self.assertEqual(len(self.bagging),2)
    def test_existing_commands_media_and_record_callbacks_pass_through(self):
        for text in ('/start fleet','/new','/late','/admin','/fleet','seal123','/start unknown'):
            self.assertFalse(self.menu.handle(self.msg(text)))
        for data in ('fl:next','outlet:SBH307','new','media:abc:def','report:menu'):
            self.assertFalse(self.menu.handle(self.cb(data)))
        self.assertFalse(self.menu.handle(self.msg('',photo=[{'file_id':'synthetic'}])))
        self.assertEqual(self.sent,[]);self.assertEqual(self.storage,{})
    def test_archive_launchers_separate_pinned_and_no_duplicate_on_restart(self):
        with patch.dict(os.environ,{'ARCHIVE_CHAT_ID':'-1001111111111','FLEET_ARCHIVE_CHAT_ID':'-1002222222222'}):
            self.menu.publish();self.menu.publish()
        sends=[p for m,p in self.sent if m=='sendMessage'];self.assertEqual(len(sends),2)
        self.assertEqual({p['chat_id'] for p in sends},{-1001111111111,-1002222222222})
        self.assertEqual(sum(m=='pinChatMessage' for m,p in self.sent),2)
        self.assertTrue(all(v['pinned'] for v in self.storage.values()))
        self.assertTrue(all(len(p['reply_markup']['inline_keyboard'])==2 for p in sends))
    def test_ambiguous_send_never_reposted(self):
        original=self.menu.telegram
        self.menu.telegram=lambda m,p:{'ok':False,'ambiguous':True} if m=='sendMessage' else original(m,p)
        markup=self.menu.links('SyntheticBot')
        self.assertEqual(self.menu.publish_channel(-1001111111111,markup),'delivery-review-required')
        self.assertEqual(self.menu.publish_channel(-1001111111111,markup),'delivery-review-required')
        self.assertEqual(len(self.storage),1)
        self.assertEqual(next(iter(self.storage.values()))['state'],'UNKNOWN')
    def test_group_input_not_mistaken_for_private_report(self):
        u=self.msg('/menu');u['message']['chat']['type']='supergroup'
        self.assertFalse(self.menu.handle(u));self.assertEqual(self.sent,[])
    def test_disabled_menu_retains_legacy_start(self):
        with patch.dict(os.environ,{'REPORT_MENU_ENABLED':'0'}):
            self.assertFalse(self.menu.handle(self.msg('/start')))
        self.assertEqual(self.sent,[])
    def test_real_app_bagging_resume_never_resets_existing_builder(self):
        tree=ast.parse(Path('app.py').read_text(encoding='utf8'))
        f=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='open_bagging_from_menu')
        calls=[];builders={7:{'step':'seal','outlet':'SBH307','seal':None,'mode':'NORMAL'}}
        ns={'builders':builders,'start_builder':lambda *a,**k:calls.append(('start',a)),
            'send_message':lambda *a:calls.append(('send',a)),'safe_html':str,
            'outlet_keyboard':lambda x:x,'late_date_keyboard':lambda:'dates'}
        exec(compile(ast.Module(body=[f],type_ignores=[]),'app.py','exec'),ns)
        for step in ('outlet','seal','late_date','late_reason','unknown'):
            builders[7]['step']=step;before=copy.deepcopy(builders)
            ns['open_bagging_from_menu'](7,7);self.assertEqual(builders,before)
        self.assertFalse(any(c[0]=='start' for c in calls))
        ns['open_bagging_from_menu'](8,8);self.assertEqual(calls[-1][0],'start')

    def test_real_webhook_auth_menu_bagging_and_duplicate_delivery(self):
        sessions={};records={};done=set();sent=[];fleet=[];api_calls=[]
        db=types.ModuleType('storage_v5_1')
        db.RecordMap=lambda:records
        db.SessionMap=lambda name:sessions.setdefault(name,{})
        def api(path,**kw):
            api_calls.append(path)
            return [{'code':'SBH307'}] if path=='sgsb_outlets' else []
        db.api=api
        db.StorageError=RuntimeError;db.FRIENDLY='Synthetic storage error'
        def rpc(name,args):
            if name=='sgsb_claim_update': return 'done' if args['p_id'] in done else 'claimed'
            if name=='sgsb_finish_update' and args['p_done']: done.add(args['p_id'])
        db.rpc=rpc
        def post(url,json=None,**kwargs):
            self.assertTrue(url.startswith('https://api.telegram.org/botsynthetic-token/'))
            sent.append((url.rsplit('/',1)[-1],copy.deepcopy(json)));return types.SimpleNamespace(json=lambda:{'ok':True,'result':{'message_id':42}})
        fake_fleet=types.SimpleNamespace(handle=lambda u:False,start=lambda *a:fleet.append(a))
        with patch.dict('sys.modules',{'storage_v5_1':db}),patch.dict(os.environ,{'BOT_TOKEN':'synthetic-token','WEBHOOK_SECRET':'synthetic-webhook','FLEET_ENABLED':'0','REPORT_MENU_CHANNELS':'0'}),patch('fleet_bot.install',return_value=fake_fleet),patch('requests.post',side_effect=post):
            spec=importlib.util.spec_from_file_location('isolated_menu_app',Path('app.py'))
            app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
            app.http_local.session=types.SimpleNamespace(post=post)
            startup_outlet_reads=api_calls.count('sgsb_outlets')
            self.assertGreaterEqual(startup_outlet_reads,1)
            client=app.app.test_client();header={'X-Telegram-Bot-Api-Secret-Token':'synthetic-webhook'}
            self.assertEqual(client.post('/webhook',json=self.msg('/start')).status_code,403)
            self.assertEqual(client.post('/webhook',json=self.msg('/start'),headers=header).status_code,200)
            warm_outlet_reads=api_calls.count('sgsb_outlets')
            self.assertLessEqual(warm_outlet_reads,startup_outlet_reads+1)
            self.assertIn('SGSB REPORT MENU',sent[-1][1]['text']);self.assertEqual(records,{})
            self.assertEqual(client.post('/webhook',json=self.cb('reports:bagging'),headers=header).status_code,200)
            self.assertEqual(sessions['builder'][7]['step'],'outlet')
            messages=sum(method=='sendMessage' for method,payload in sent)
            self.assertTrue(client.post('/webhook',json=self.cb('reports:bagging'),headers=header).json['duplicate'])
            self.assertEqual(sum(method=='sendMessage' for method,payload in sent),messages)
            u=self.cb('reports:fleet');u['update_id']=7
            self.assertEqual(client.post('/webhook',json=u,headers=header).status_code,200)
            self.assertEqual(fleet,[(7,7,7)]);self.assertEqual(records,{})
            self.assertLessEqual(api_calls.count('sgsb_outlets'),startup_outlet_reads+1)
            self.assertIs(app.update_lock(self.msg('/start')),app.update_lock(self.cb('reports:fleet')))
            other=self.msg('/start');other['message']['from']['id']=8;other['message']['chat']['id']=8
            self.assertIsNot(app.update_lock(self.msg('/start')),app.update_lock(other))
            callback_acks=[payload for method,payload in sent if method=='answerCallbackQuery']
            self.assertGreaterEqual(len(callback_acks),3)

if __name__=='__main__': unittest.main()
