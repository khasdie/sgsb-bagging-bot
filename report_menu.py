"""Report navigation only. No business record, upload or permission mutations."""
import os
import re
import threading

TITLE='SGSB REPORT MENU'
TEXT=TITLE+'\nChoose a report type. Existing drafts and records are kept.\n\nFLEET Report: outlet, OFFLOAD/CLEAR and photos.\nVideo Bagging Report: existing outlet, seal and media workflow.\n\nEach report stays in its own archive.'

class ReportMenu:
    def __init__(self,db,telegram,fleet,open_bagging):
        self.db,self.telegram,self.fleet,self.open_bagging=db,telegram,fleet,open_bagging

    def links(self,username):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username or ''): raise ValueError('Bot identity unconfirmed')
        base='https://t.me/'+username+'?start='
        return {'inline_keyboard':[[{'text':'FLEET Report','url':base+'fleet'}],[{'text':'Video Bagging Report','url':base+'bagging'}]]}

    def handle(self,update):
        if os.environ.get('REPORT_MENU_ENABLED','1')!='1': return False
        cb=update.get('callback_query');msg=(cb or {}).get('message') or update.get('message') or {}
        chat=msg.get('chat',{});user=(cb or msg).get('from',{}).get('id')
        if not user or chat.get('type')!='private': return False
        text=msg.get('text','').strip();parts=text.split();command=parts[0].split('@')[0] if parts else ''
        data=(cb or {}).get('data','')
        menu=not cb and (command=='/menu' and len(parts)==1 or command=='/start' and parts[1:] in ([],['reports']))
        bagging=not cb and command=='/start' and parts[1:]==['bagging']
        if not menu and not bagging and data not in ('reports:menu','reports:bagging','reports:fleet'): return False
        if cb and not cb.get('_btm_answered'): self.telegram('answerCallbackQuery',{'callback_query_id':cb['id']})
        if bagging or data=='reports:bagging': self.open_bagging(chat['id'],user)
        elif data=='reports:fleet': self.fleet.start(chat['id'],user,update['update_id'])
        else:
            self.telegram('sendMessage',{'chat_id':chat['id'],'text':TEXT,'reply_markup':{'inline_keyboard':[[{'text':'FLEET Report','callback_data':'reports:fleet'}],[{'text':'Video Bagging Report','callback_data':'reports:bagging'}]]}})
        return True

    def publish_channel(self,channel,markup):
        key='report_menu_launcher:'+str(channel)+':v1'
        rows=self.db.api('sgsb_sessions',params={'key':'eq.'+key,'select':'data','limit':1})
        if rows: state=rows[0]['data']
        else:
            claimed=self.db.api('sgsb_sessions','POST',{'key':key,'data':{'state':'SENDING'}},params={'on_conflict':'key'},prefer='resolution=ignore-duplicates,return=representation')
            if not claimed: return 'claimed-by-another-worker'
            sent=self.telegram('sendMessage',{'chat_id':channel,'text':TEXT,'reply_markup':markup,'disable_notification':True})
            mid=sent.get('result',{}).get('message_id') if sent.get('ok') else None
            state={'state':'SENT' if mid else 'UNKNOWN','messageId':mid,'pinned':False}
            self.db.api('sgsb_sessions','PATCH',{'data':state},params={'key':'eq.'+key})
        if not state.get('messageId'): return 'delivery-review-required'
        if not state.get('pinned'):
            pin=self.telegram('pinChatMessage',{'chat_id':channel,'message_id':state['messageId'],'disable_notification':True})
            if pin.get('ok'):
                state={**state,'pinned':True}
                self.db.api('sgsb_sessions','PATCH',{'data':state},params={'key':'eq.'+key})
        return 'posted-and-pinned' if state.get('pinned') else 'posted-pin-unconfirmed'

    def publish(self):
        if os.environ.get('REPORT_MENU_CHANNELS','1')!='1': return
        try:
            me=self.telegram('getMe',{})
            if not me.get('ok'): raise ValueError('Identity unavailable')
            markup=self.links(me['result'].get('username'))
            seen=set()
            for label,key in (('BAGGING','ARCHIVE_CHAT_ID'),('FLEET','FLEET_ARCHIVE_CHAT_ID')):
                configured=os.environ.get(key,'')
                if not re.fullmatch(r'(?:-100[1-9]\d+|@[A-Za-z][A-Za-z0-9_]{4,31})',configured):
                    print('Report menu '+label+': channel-unconfirmed',flush=True);continue
                channel=self.telegram('getChat',{'chat_id':configured})
                if not channel.get('ok') or channel['result'].get('type')!='channel':
                    print('Report menu '+label+': channel-unconfirmed',flush=True);continue
                cid=channel['result']['id']
                if cid in seen: raise ValueError('Report archives must remain separate')
                seen.add(cid)
                member=self.telegram('getChatMember',{'chat_id':cid,'user_id':me['result']['id']})
                role=member.get('result',{}) if member.get('ok') else {}
                if role.get('status')!='creator' and (role.get('status')!='administrator' or role.get('can_post_messages') is not True):
                    print('Report menu '+label+': posting-permission-unconfirmed',flush=True);continue
                print('Report menu '+label+': '+self.publish_channel(cid,markup),flush=True)
        except Exception:
            print('Report menu: verification incomplete; existing reports retained',flush=True)

def install(db,telegram,fleet,open_bagging):
    menu=ReportMenu(db,telegram,fleet,open_bagging)
    if os.environ.get('REPORT_MENU_ENABLED','1')=='1':
        threading.Thread(target=menu.publish,daemon=True,name='report-menu-launchers').start()
    return menu
