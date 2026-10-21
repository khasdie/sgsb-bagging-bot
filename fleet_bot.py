"""Additive SGSB FLEET transport. Credentials stay on the Render server."""
import base64
import hmac
import hashlib
import html
import os
import re
import uuid
import time
import threading
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import request, jsonify
import requests
from fleet_core import transition, project, FleetError

TZ=ZoneInfo('Asia/Kuala_Lumpur')
CODES=('SBH306','SBH307','SBH324','SBH327','SBH335','SBH403','SBH418','SBH458','SBH462','SBH475','SBH487','SBH496')

class FleetBot:
    def __init__(self,db,telegram,admin_sessions):
        self.db,self.telegram,self.admin_sessions=db,telegram,admin_sessions
        self.sessions=db.SessionMap('fleet')
        self.albums=db.SessionMap('fleet_album')
        self.pending_media=db.SessionMap('fleet_media')
        self.channel_cache=None
        self.bot_username=None
        self.access=db.SessionMap('fleet_access')

    def channel_id(self):
        configured=os.environ.get('FLEET_ARCHIVE_CHAT_ID','')
        if self.channel_cache and self.channel_cache[0]==configured and self.channel_cache[1]>time.monotonic(): return self.channel_cache[2]
        if not re.fullmatch(r'(?:-100[1-9]\d+|@[A-Za-z][A-Za-z0-9_]{4,31})',configured): return None
        channel=self.telegram('getChat',{'chat_id':configured});me=self.telegram('getMe',{})
        if not channel.get('ok') or not me.get('ok'): return None
        c=channel['result'];identity=str(c.get('id',''))
        if c.get('type')!='channel' or not re.fullmatch(r'-100[1-9]\d+',identity) or identity==str(os.environ.get('ARCHIVE_CHAT_ID')): return None
        member=self.telegram('getChatMember',{'chat_id':c['id'],'user_id':me['result']['id']})
        m=member.get('result',{}) if member.get('ok') else {}
        if m.get('status') not in ('administrator','creator') or m.get('status')!='creator' and m.get('can_post_messages') is not True: return None
        self.channel_cache=(configured,time.monotonic()+300,identity)
        self.bot_username=me['result'].get('username')
        return identity

    def enabled(self):
        return os.environ.get('FLEET_ENABLED')=='1' and len(os.environ.get('FLEET_BTM_READ_TOKEN',''))>=43 and bool(self.channel_id())

    def apply_owner_access(self):
        """Explicit operator-approved, one-time account mapping; preserve prior ACL."""
        raw=os.environ.get('FLEET_OWNER_ACCESS_REQUEST','')
        if not raw: return 'not-configured'
        if len(raw)>2000: raise FleetError('Invalid owner access request.')
        try: config=json.loads(raw)
        except (ValueError,TypeError): raise FleetError('Invalid owner access request.') from None
        if not isinstance(config,dict) or set(config)!={'requestId','userId','outlets'}:
            raise FleetError('Invalid owner access request.')
        uid=config['userId'];outlets=config['outlets'];request_id=config['requestId']
        if not isinstance(uid,str) or not re.fullmatch(r'[1-9][0-9]{4,19}',uid) or not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',request_id):
            raise FleetError('Invalid owner access request.')
        if not isinstance(outlets,list) or not outlets or len(outlets)>len(CODES) or any(c not in CODES for c in outlets) or len(set(outlets))!=len(outlets):
            raise FleetError('Invalid owner outlet scope.')
        key='fleet_owner_access:'+request_id
        fingerprint=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
        existing=self.db.api('sgsb_sessions',params={'key':'eq.'+key,'select':'data','limit':1})
        if existing:
            if existing[0]['data'].get('requestHash')!=fingerprint or existing[0]['data'].get('state')!='APPLIED':
                raise FleetError('Owner access request needs review.')
            # Never regrant after a subsequent administrator revocation/change.
            return 'already-applied'
        previous=self.access.get(int(uid))
        now=datetime.now(TZ).isoformat()
        access={'outlets':sorted(outlets),'source':'OWNER_CONFIRMED_REQUEST','requestId':request_id,'updatedAt':now}
        marker={'state':'APPLIED','requestHash':fingerprint,'userId':uid,'previousAccess':dict(previous) if previous is not None else None,'assignedOutlets':sorted(outlets),'appliedAt':now}
        # PostgREST writes this array in one transaction: backup + account mapping.
        self.db.api('sgsb_sessions','POST',[{'key':'fleet_access:'+uid,'data':access,'updated_at':now},{'key':key,'data':marker,'updated_at':now}],params={'on_conflict':'key'},prefer='resolution=merge-duplicates,return=minimal')
        actual=self.access.get(int(uid)) or {}
        if actual.get('outlets')!=sorted(outlets) or actual.get('requestId')!=request_id:
            raise FleetError('Owner access read-back unconfirmed.')
        return 'applied-and-verified-'+str(len(outlets))+'-outlets'

    def publish_launcher(self):
        """One durable claim before publishing; an ambiguous send is never repeated."""
        if not self.enabled(): return False
        channel=self.channel_id();key='fleet_launcher:'+channel+':v1'
        existing=self.db.api('sgsb_sessions',params={'key':'eq.'+key,'select':'data','limit':1})
        if existing:
            data=existing[0]['data'];mid=data.get('messageId')
            if mid and not data.get('pinned'):
                pinned=self.telegram('pinChatMessage',{'chat_id':channel,'message_id':mid,'disable_notification':True})
                if pinned.get('ok'):
                    self.db.api('sgsb_sessions','PATCH',{'data':{**data,'pinned':True}},params={'key':'eq.'+key})
            return bool(mid)
        claim=self.db.api('sgsb_sessions','POST',{'key':key,'data':{'state':'SENDING'}},params={'on_conflict':'key'},prefer='resolution=ignore-duplicates,return=representation')
        if not claim: return False
        result=self.telegram('sendMessage',{'chat_id':channel,'text':'SGSB FLEET\n1. Tap Start FLEET\n2. Choose your outlet\n3. Choose OFFLOAD or CLEAR\n4. Send photos, review and confirm\n5. Continue with the next outlet\n\nPhotos remain in Telegram. BTM updates automatically. Only assigned outlets can submit.', 'reply_markup':{'inline_keyboard':[[{'text':'Start FLEET','url':'https://t.me/'+self.bot_username+'?start=fleet'}]]},'disable_notification':True})
        mid=result.get('result',{}).get('message_id') if result.get('ok') else None
        data={'state':'SENT' if mid else 'UNKNOWN','messageId':mid,'pinned':False}
        self.db.api('sgsb_sessions','PATCH',{'data':data},params={'key':'eq.'+key})
        if mid:
            pinned=self.telegram('pinChatMessage',{'chat_id':channel,'message_id':mid,'disable_notification':True})
            if pinned.get('ok'):
                self.db.api('sgsb_sessions','PATCH',{'data':{**data,'pinned':True}},params={'key':'eq.'+key})
        return bool(mid)

    def activate(self):
        try:
            active=self.enabled()
            print('FLEET activation:', 'ready' if active else 'not-connected', flush=True)
            if active and os.environ.get('FLEET_OWNER_ACCESS_REQUEST'):
                print('FLEET owner access:',self.apply_owner_access(),flush=True)
            if active and os.environ.get('FLEET_CHANNEL_START','1')=='1':
                print('FLEET channel launcher:', 'confirmed' if self.publish_launcher() else 'review-required', flush=True)
        except Exception:
            print('FLEET activation: temporarily-unavailable; existing data retained',flush=True)

    def allowed(self,user_id):
        admin=self.admin_sessions.get(user_id)
        if admin and admin.get('authenticated'): return list(CODES)
        # Possession of an outlet code or a Telegram username is not permission.
        access=self.access.get(user_id) or {}
        return sorted({c for c in access.get('outlets',[]) if c in CODES})

    def row(self,rid):
        rows=self.db.api('sgsb_fleet_reports',params={'id':'eq.'+str(rid),'select':'version,data','limit':1})
        return rows[0] if rows else None

    def save(self,event,rid,owner,version,data):
        return self.db.rpc('sgsb_fleet_cas',{'p_event':str(event),'p_id':rid,'p_owner':owner,'p_expected':version,'p_data':data})

    def send(self,chat,text,buttons=None):
        payload={'chat_id':chat,'text':text,'parse_mode':'HTML'}
        if buttons: payload['reply_markup']={'inline_keyboard':buttons}
        result=self.telegram('sendMessage',payload)
        if not result.get('ok'): raise FleetError('Telegram response unconfirmed; keep the original report.')
        return result['result']['message_id']

    def card(self,r):
        rid=r['id']; esc=lambda x:html.escape(str(x or ''))
        text='<b>FLEET REPORT</b>\n'+esc(r['outletCode'])+' · '+esc(r['reportDate'])+'\nStatus: '+esc(r.get('decision','Not selected'))+'\nPhotos: '+str(len(r['photos']))+'\nRemark: '+esc(r.get('remark',''))+'\nRecord: <code>'+rid+'</code>\nReply to this card with photos or a remark. Review then confirm.'
        return text,[[{'text':'OFFLOAD','callback_data':'fl:OFFLOAD:'+rid},{'text':'CLEAR','callback_data':'fl:CLEAR:'+rid}],[{'text':'Confirm report','callback_data':'fl:confirm:'+rid}]]

    def start(self,chat,user,event):
        if not self.enabled():
            self.send(chat,'FLEET is not enabled yet. Bagging continues normally.');return
        codes=self.allowed(user)
        if not codes:
            self.send(chat,'FLEET outlet access has not been verified.\nYour Telegram ID: <code>'+str(user)+'</code>\nSend this ID and your outlet code to your administrator for Fleet access.\nExisting bot administrators can sign in using /admin, then open /fleet again. Never share your passcode in another chat.');return
        current=self.sessions.get(user)
        if current and current.get('id'):
            row=self.row(current['id'])
            if row and row['data']['state']=='DRAFT':
                self.send(chat,*self.card(row['data']));return
        self.send(chat,'FLEET · Choose your outlet for today. Bagging records remain separate.',[[{'text':c,'callback_data':'fl:quick:'+c}] for c in codes]+[[{'text':'Report for another date','callback_data':'fl:backdated'}]])

    def handle(self,update):
        cb=update.get('callback_query');msg=(cb or {}).get('message') or update.get('message') or {}
        user=(cb or msg).get('from',{}).get('id');chat=msg.get('chat',{}).get('id')
        text=msg.get('text','').strip();data=(cb or {}).get('data','')
        if not user or msg.get('chat',{}).get('type')!='private': return False
        command=text.split('@')[0].split(' ')[0]
        deep_link=command=='/start' and text.split()[1:]==['fleet']
        is_fleet=bool(cb and data.startswith('fl:')) or command in ('/fleet','/fleet_id','/fleet_cancel','/fleet_access') or deep_link
        reply=msg.get('reply_to_message',{}).get('text','')
        match=re.search(r'Record: ([0-9a-f-]{36})',reply) if 'FLEET REPORT' in reply else None
        date_reply=reply.startswith('FLEET DATE')
        group=msg.get('media_group_id');album_key=hashlib.sha256(f'{user}:{chat}:album:{group}'.encode()).hexdigest()[:16] if group else None
        if not cb and msg.get('photo') and not match and not reply and self.enabled():
            bound=self.albums.get(album_key) if album_key else None
            if bound and bound.get('ownerId')==user:
                match=re.fullmatch(r'([0-9a-f-]{36})',bound['id'])
            else:
                # An existing Bagging album keeps its original handler and binding.
                if album_key and self.db.SessionMap('media').get(album_key): return False
                active=self.sessions.get(user)
                row=self.row(active['id']) if active and active.get('id') else None
                if row and row['data'].get('state')=='DRAFT':
                    token=hashlib.sha256(f'{user}:{chat}:{msg.get("message_id",update["update_id"])}'.encode()).hexdigest()[:16]
                    self.pending_media[token]={'ownerId':user,'chatId':chat,'photo':msg['photo'][-1],'albumKey':album_key}
                    self.send(chat,'Photo retained without assigning it to a report. Confirm its original outlet/date before attaching. Do not upload again.',[[{'text':'Attach to FLEET '+row['data']['outletCode']+' '+row['data']['reportDate'],'callback_data':'fl:attach:'+row['data']['id']+':'+token}]])
                    return True
        if not is_fleet and not match and not date_reply: return False
        session=self.sessions.get(user)
        # Never steal Bagging photos or remark replies, even with a Fleet draft.
        try:
            if cb: self.telegram('answerCallbackQuery',{'callback_query_id':cb['id']})
            if command=='/fleet_id':
                self.send(chat,'Your Telegram ID: <code>'+str(user)+'</code>\nThis identifies your own Telegram account; it does not grant access or change your role.');return True
            if command=='/fleet_access':
                if not (self.admin_sessions.get(user) or {}).get('authenticated'): raise FleetError('Sign in with the existing /admin flow first.')
                parts=text.split()
                if len(parts)!=3 or not parts[1].isdigit(): raise FleetError('Use /fleet_access TelegramUserId SBH307,SBH458 (or NONE to revoke Fleet only).')
                outlets=[] if parts[2]=='NONE' else parts[2].upper().split(',')
                if len(outlets)>12 or any(c not in CODES for c in outlets): raise FleetError('Choose approved SBH outlet codes only.')
                self.access[int(parts[1])]={'outlets':sorted(set(outlets)),'source':'ADMIN_ASSIGNMENT','updatedAt':datetime.now(TZ).isoformat()}
                self.send(chat,'FLEET access assignment saved. Bagging and BTM account roles are unchanged.');return True
            if command=='/fleet_cancel':
                self.sessions.pop(user,None);self.send(chat,'FLEET draft retained. Use /fleet to start again; Bagging is unchanged.');return True
            if command=='/fleet' or deep_link: self.start(chat,user,update['update_id']);return True
            if not self.enabled(): raise FleetError('FLEET is not enabled yet.')
            if data=='fl:next': self.start(chat,user,update['update_id']);return True
            if data=='fl:backdated':
                self.send(chat,'Choose an outlet, then the original report date.',[[{'text':c,'callback_data':'fl:outlet:'+c}] for c in self.allowed(user)])
                return True
            event='fleet:'+str(update['update_id'])
            memo=self.db.api('sgsb_fleet_events',params={'event_id':'eq.'+event,'select':'result','limit':1})
            if memo:
                # The same accepted update never performs an archive side effect again.
                self.send(chat,'FLEET input already retained. Use the original report; do not upload again.')
                return True
            codes=self.allowed(user)
            if data.startswith('fl:quick:'):
                outlet=data.split(':')[2]
                if outlet not in codes: raise FleetError('Outlet access denied.')
                current=self.sessions.get(user)
                old=self.row(current['id']) if current and current.get('id') else None
                if old and old['data']['state']=='DRAFT':
                    self.send(chat,*self.card(old['data']));return True
                rid=str(uuid.uuid5(uuid.NAMESPACE_URL,'sgsb-fleet:'+event))
                r={'id':rid,'ownerId':user,'chatId':chat,'outletCode':outlet,'state':'DRAFT','photos':[],'remark':'','createdAt':datetime.now(TZ).isoformat(),'reportDate':datetime.now(TZ).date().isoformat()}
                self.save(event,rid,user,0,r);self.sessions[user]={'step':'report','id':rid}
                self.send(chat,*self.card(r));return True
            if data.startswith('fl:outlet:'):
                outlet=data.split(':')[2]
                if outlet not in codes: raise FleetError('Outlet access denied.')
                # No record is created until a date has been selected.
                self.sessions[user]={'step':'date','outlet':outlet,'seed':event}
                self.send(chat,'Choose the FLEET report date.',[[{'text':'Today','callback_data':'fl:date:today'},{'text':'Yesterday','callback_data':'fl:date:yesterday'}],[{'text':'Other date','callback_data':'fl:date:custom'}]])
                return True
            if data.startswith('fl:date:') or date_reply:
                if not session or session.get('step')!='date' or session.get('outlet') not in codes: raise FleetError('Select your outlet using /fleet first.')
                if data=='fl:date:custom':
                    self.telegram('sendMessage',{'chat_id':chat,'text':'FLEET DATE · Reply with YYYY-MM-DD','reply_markup':{'force_reply':True,'input_field_placeholder':'YYYY-MM-DD'}})
                    return True
                today=datetime.now(TZ).date();value=text
                if data=='fl:date:today': value=today.isoformat()
                elif data=='fl:date:yesterday': value=(today-timedelta(days=1)).isoformat()
                rid=str(uuid.uuid5(uuid.NAMESPACE_URL,'sgsb-fleet:'+session['seed']))
                r={'id':rid,'ownerId':user,'chatId':chat,'outletCode':session['outlet'],'state':'DRAFT','photos':[],'remark':'','createdAt':datetime.now(TZ).isoformat()}
                r=transition(r,'date',value,today.isoformat());self.save(session['seed'],rid,user,0,r)
                self.sessions[user]={'step':'report','id':rid}
                self.send(chat,*self.card(r));return True
            rid=match.group(1) if match else data.split(':')[2]
            try: uuid.UUID(rid)
            except ValueError: raise FleetError('Use the original FLEET card.') from None
            row=self.row(rid)
            if not row or row['data']['ownerId']!=user or row['data']['outletCode'] not in codes: raise FleetError('Report access denied.')
            r=row['data']
            if r['state']!='DRAFT':
                self.send(chat,'Report '+rid+' · '+r['state']+'. Already submitted or awaiting delivery review. Do not resend.');return True
            action=data.split(':')[1] if cb else 'photo' if msg.get('photo') else 'remark'
            if action=='attach':
                token=data.split(':')[3] if len(data.split(':'))==4 else ''
                held=self.pending_media.get(token)
                if not held or held.get('ownerId')!=user or held.get('chatId')!=chat: raise FleetError('Retained photo access denied.')
                action,value='photo',held['photo']
                if held.get('albumKey'): self.albums[held['albumKey']]={'id':rid,'ownerId':user}
            elif not cb and msg.get('photo') and album_key:
                old=self.albums.get(album_key)
                if old and old.get('id')!=rid: raise FleetError('Album already belongs to another report. Existing photos are retained.')
                self.albums[album_key]={'id':rid,'ownerId':user}
            if cb and action in ('OFFLOAD','CLEAR'): action,value='decision',action
            elif action=='confirm': value=None
            elif msg.get('photo'): value=msg['photo'][-1]
            elif data.startswith('fl:attach:'): pass
            elif msg.get('document') or msg.get('video'): raise FleetError('FLEET accepts Photo/Gallery images. Bagging videos belong on their Bagging card.')
            else: value=text
            changed=transition(r,action,value,datetime.now(TZ).date().isoformat())
            saved=self.save(event,rid,user,row['version'],changed)
            if data.startswith('fl:attach:'): self.pending_media.pop(token,None)
            if action=='confirm': self.archive(saved,event)
            else:
                self.send(chat,*self.card(changed))
                if action=='decision':
                    self.telegram('sendMessage',{'chat_id':chat,'text':'FLEET REPORT · '+changed['outletCode']+' · '+changed['decision']+'\nRecord: '+rid+'\nSend the outlet photo here. Then tap Confirm report on the card above.', 'reply_markup':{'force_reply':True,'selective':True}})
            return True
        except FleetError as exc:
            self.send(chat,html.escape(str(exc)));return True

    def archive(self,row,event):
        r=row['data'];chat=r['chatId'];channel=self.channel_id()
        if not channel: raise FleetError('FLEET channel permissions could not be verified. Do not resend.')
        caption='FLEET | '+r['outletCode']+' | '+r['reportDate']+' | '+r['decision']+'\nRecord: '+r['id']+'\n'+r.get('remark','')
        # sendPhoto/file_id and sendMediaGroup reuse Telegram media; no upload copy.
        try:
            if len(r['photos'])==1:
                response=self.telegram('sendPhoto',{'chat_id':channel,'photo':r['photos'][0]['file_id'],'caption':caption[:1024]})
                messages=[response['result']] if response.get('ok') else []
            else:
                media=[{'type':'photo','media':p['file_id'],**({'caption':caption[:1024]} if i==0 else {})} for i,p in enumerate(r['photos'])]
                response=self.telegram('sendMediaGroup',{'chat_id':channel,'media':media});messages=response.get('result',[]) if response.get('ok') else []
            if not messages or any(type(m.get('message_id')) is not int for m in messages): raise FleetError('Archive response unconfirmed.')
            done={**r,'state':'SENT','archiveChatId':channel,'archiveMessageIds':[m['message_id'] for m in messages],'confirmedAt':datetime.now(TZ).isoformat()}
            self.save(event+':sent',r['id'],r['ownerId'],row['version'],done)
        except Exception:
            # A timeout may follow a successful send. Never blindly send again.
            self.save(event+':unknown',r['id'],r['ownerId'],row['version'],{**r,'state':'UNKNOWN'})
            self.send(chat,'FLEET delivery needs administrator review. Your photos and report are retained. Do not upload or confirm again.');return
        self.send(chat,'FLEET ARCHIVE SUCCESS · '+r['outletCode']+' · '+r['reportDate']+' · '+r['decision']+'\nBTM updates automatically. Record: '+r['id'],[[{'text':'Next outlet','callback_data':'fl:next'}]])

    def read_http(self):
        key=os.environ.get('FLEET_BTM_READ_TOKEN','');given=request.headers.get('x-btm-fleet-key','')
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}
        def reply(body,status=200): return jsonify(body),status,headers
        if request.headers.get('Origin') or len(key)<43 or not re.fullmatch(r'[-_A-Za-z0-9]{43,128}',given) or not hmac.compare_digest(key,given): return reply({'success':False},401)
        if set(request.args)=={'channelCheck'} and request.args.get('channelCheck')=='1':
            channel=self.telegram('getChat',{'chat_id':os.environ.get('FLEET_SETUP_CHANNEL','')})
            me=self.telegram('getMe',{})
            if not channel.get('ok') or not me.get('ok'): return reply({'success':False,'code':'CHANNEL_UNCONFIRMED'},503)
            c=channel['result'];member=self.telegram('getChatMember',{'chat_id':c['id'],'user_id':me['result']['id']})
            m=member.get('result',{}) if member.get('ok') else {}
            ok=c.get('type')=='channel' and m.get('status') in ('administrator','creator') and (m.get('status')=='creator' or m.get('can_post_messages') is True)
            return reply({'success':ok,'channel':{'id':c['id'],'title':c.get('title'),'username':c.get('username'),'botCanPost':ok},'botUsername':me['result'].get('username')})
        if not self.enabled(): return reply({'success':False,'code':'FLEET_NOT_CONNECTED'},503)
        try:
            q=request.args
            if any(len(q.getlist(k))!=1 for k in q): raise FleetError('Repeated parameters are not allowed.')
            if set(q)-{'dateFrom','dateTo','outlets','recordId','photoIndex'}: raise FleetError('Invalid query.')
            codes=q.get('outlets','').split(',')
            if not codes or len(codes)>len(CODES) or any(c not in CODES for c in codes): raise FleetError('Invalid outlet scope.')
            if q.get('recordId'):
                row=self.row(str(uuid.UUID(q['recordId'])))
                if not row or row['data'].get('state')!='SENT' or row['data'].get('outletCode') not in codes: return reply({'success':False},404)
                i=int(q.get('photoIndex','0'));photos=row['data']['photos']
                if i<0 or i>=len(photos): raise FleetError('Invalid photo.')
                return reply({'success':True,'data':self.image(photos[i])})
            start=date_value(q.get('dateFrom'));end=date_value(q.get('dateTo'));today=datetime.now(TZ).date()
            if start>end or end>today or (end-start).days>365: raise FleetError('Invalid date range.')
            rows=self.db.rpc('sgsb_fleet_read',{'p_from':start.isoformat(),'p_to':end.isoformat(),'p_codes':codes})
            if len(rows)>2000: raise FleetError('Range too large.')
            data=[project(r['data'],r['data'].get('archiveChatId') or self.channel_id()) for r in rows]
            bot_url='https://t.me/'+self.bot_username+'?start=fleet' if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',self.bot_username or '') else None
            return reply({'success':True,'data':{'source':'TELEGRAM_FLEET','completeRead':True,'codes':codes,'records':data,'botUrl':bot_url,'channelId':self.channel_id(),'checkedAt':datetime.now(TZ).isoformat()}})
        except (FleetError,ValueError,KeyError): return reply({'success':False,'code':'FLEET_INVALID_INPUT'},400)

    def image(self,photo):
        result=self.telegram('getFile',{'file_id':photo['file_id']})
        path=result.get('result',{}).get('file_path','') if result.get('ok') else ''
        if not re.fullmatch(r'photos/[A-Za-z0-9_.-]+',path): raise FleetError('Photo temporarily unavailable.')
        url='https://api.telegram.org/file/bot'+os.environ['BOT_TOKEN']+'/'+path
        # Never return/log a token-bearing URL, redirect, or raw Telegram error.
        try:
            with requests.get(url,stream=True,timeout=(5,20),allow_redirects=False) as response:
                if response.status_code!=200: raise FleetError('Photo temporarily unavailable.')
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>8*1024*1024: raise FleetError('Photo exceeds preview size limit.')
                    chunks.append(chunk)
                content=b''.join(chunks)
        except requests.RequestException: raise FleetError('Photo temporarily unavailable.') from None
        if not content.startswith(b'\xff\xd8\xff'): raise FleetError('Unsupported photo format.')
        return {'mimeType':'image/jpeg','base64':base64.b64encode(content).decode('ascii')}

def date_value(value):
    from datetime import date
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value or ''): raise FleetError('Use YYYY-MM-DD.')
    return date.fromisoformat(value or '')

def install(app,db,telegram,admin_sessions):
    bot=FleetBot(db,telegram,admin_sessions)
    app.add_url_rule('/btm/fleet','btm_fleet',bot.read_http,methods=['GET'])
    if os.environ.get('FLEET_ENABLED')=='1':
        threading.Thread(target=bot.activate,daemon=True,name='fleet-activation').start()
    return bot
