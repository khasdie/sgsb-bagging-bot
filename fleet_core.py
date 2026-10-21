"""Fleet-only state transitions; no network at import, no Bagging writes."""
import copy
import re
from datetime import date

class FleetError(ValueError):
    pass

def transition(record, action, value, today):
    r = copy.deepcopy(record)
    if r.get('state') != 'DRAFT':
        raise FleetError('Report already submitted or delivery needs review. Do not resend.')
    if action == 'date':
        try:
            chosen = date.fromisoformat(value)
        except (ValueError, TypeError):
            raise FleetError('Use YYYY-MM-DD.') from None
        if chosen > date.fromisoformat(today):
            raise FleetError('Future report dates are not allowed.')
        r['reportDate'] = value
    elif action == 'outlet':
        if not re.fullmatch(r'SBH\d+', value or ''):
            raise FleetError('Choose an approved SBH outlet.')
        r['outletCode'] = value
    elif action == 'decision':
        if value not in ('OFFLOAD', 'CLEAR'):
            raise FleetError('Choose OFFLOAD or CLEAR.')
        r['decision'] = value
    elif action == 'photo':
        if not isinstance(value, dict) or not value.get('file_id') or not value.get('file_unique_id'):
            raise FleetError('Send an image as Photo/Gallery.')
        if value.get('file_size', 0) > 8 * 1024 * 1024:
            raise FleetError('Maximum 8 MB per photo.')
        old = r.setdefault('photos', [])
        if not any(p['file_unique_id'] == value['file_unique_id'] for p in old):
            if len(old) >= 5:
                raise FleetError('Maximum five photos; your existing photos are retained.')
            old.append(value)
    elif action == 'remark':
        if not isinstance(value, str) or len(value) > 2000:
            raise FleetError('Remark must be at most 2000 characters.')
        r['remark'] = value
    elif action == 'confirm':
        if not r.get('outletCode') or not r.get('reportDate') or r.get('decision') not in ('OFFLOAD','CLEAR') or not r.get('photos'):
            raise FleetError('Choose outlet, date, status and attach at least one photo first.')
        r['state'] = 'SENDING'
    else:
        raise FleetError('Unknown action.')
    return r

def project(record, archive_chat_id):
    """Only a successfully archived report is a verified dashboard report."""
    if record.get('state') != 'SENT':
        return None
    channel = str(archive_chat_id)
    ids = record.get('archiveMessageIds', [])
    if not re.fullmatch(r'-100[1-9]\d+', channel) or not ids or any(type(i) is not int or i < 1 for i in ids):
        raise FleetError('Archive confirmation invalid.')
    return {k:record.get(k) for k in ('id','outletCode','reportDate','decision','remark','createdAt','confirmedAt')} | {
        'source':'TELEGRAM', 'photoCount':len(record['photos']),
        'telegramUrl':'https://t.me/c/'+channel[4:]+'/'+str(ids[0]),
    }
