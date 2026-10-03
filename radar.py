"""Deterministic tools for an LLM hosted in Copilot or Claude Code. Python 3.10+."""
import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DIMENSIONS = ('experience', 'education', 'work_authorization', 'career_fit')

def today():
    try:
        return datetime.now(ZoneInfo('America/Chicago')).date().isoformat()
    except ZoneInfoNotFoundError:
        raise ValueError('Timezone data missing. Install tzdata or supply --date YYYY-MM-DD.')

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(temp, path)

def empty():
    return {'jobs': {}, 'applications': {}, 'radars': {}, 'events': []}

def key(job):
    # A requisition is unique within a company; otherwise use its source URL.
    identity = job.get('job_id') or job.get('source_url')
    if not identity:
        raise ValueError('Provide job_id or source_url; do not guess either.')
    value = job['company'].strip().casefold() + '|' + identity.strip().casefold()
    return hashlib.sha256(value.encode()).hexdigest()[:16]

def ingest(state, job, day):
    for field in ('company', 'role', 'text', 'source_url'):
        if not isinstance(job.get(field), str) or not job[field].strip():
            raise ValueError('Missing field: '+field)
    if job.get('source_kind') not in ('demo', 'employer', 'user_copy'):
        raise ValueError('source_kind must be demo, employer, or user_copy')
    k = key(job)
    old = state['jobs'].get(k)
    if old and old['posting'] != job:
        raise ValueError('Different snapshot for existing role. Keep history; use a new state for reevaluation in v0.1.')
    if not old:
        state['jobs'][k] = {'posting': job, 'date_found': day, 'assessment': None}
        state['events'].append({'date': day, 'action': 'ingest', 'key': k})
    return {'key': k, 'duplicate': old is not None}

def assess(state, k, assessment, day):
    entry = state['jobs'][k]
    text = entry['posting']['text']
    for dim in DIMENSIONS:
        item = assessment.get(dim, {})
        if item.get('status') not in ('pass', 'uncertain', 'fail'):
            raise ValueError('Invalid status for '+dim)
        if not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ValueError('Explain '+dim)
        quote = item.get('quote', '')
        if not isinstance(quote, str) or (quote and quote not in text):
            raise ValueError('Evidence not found verbatim in posting: '+dim)
        if item['status'] != 'uncertain' and not quote.strip():
            raise ValueError('Pass/fail requires exact evidence: '+dim)
    if assessment.get('fit') not in ('GOOD FIT', 'STRETCH BUT REASONABLE', 'POOR FIT'):
        raise ValueError('Invalid fit classification')
    if not assessment.get('next_action'):
        raise ValueError('Provide next_action')
    # Keep prior assessments for a reviewable record.
    entry.setdefault('assessment_history', []).append({'date': day, 'value': assessment})
    entry['assessment'] = assessment
    entry['assessed_on'] = day
    state['events'].append({'date': day, 'action': 'assess', 'key': k})
    return {'key': k, 'decision': decision(entry, day)}

def decision(entry, day):
    p, a = entry['posting'], entry['assessment']
    if p.get('open') is False:
        return 'SKIP'
    if p.get('deadline'):
        if date.fromisoformat(p['deadline']) < date.fromisoformat(day):
            return 'SKIP'
    if not a:
        return 'VERIFY'
    if a['fit'] == 'POOR FIT' or any(a[d]['status'] == 'fail' for d in DIMENSIONS):
        return 'SKIP'
    if any(a[d]['status'] == 'uncertain' for d in DIMENSIONS):
        return 'VERIFY'
    if p.get('open') is not True or p.get('verified_on') != day:
        return 'VERIFY'
    # Employer snapshot and human verification for real targets. The flag is
    # an attestation, not a security boundary or independent eligibility check.
    if p['source_kind'] != 'demo' and not p.get('human_verified'):
        return 'VERIFY'
    return 'RECOMMEND'

def shortlist(state, day):
    if day in state['radars']:
        keys = state['radars'][day]
        return {'date': day, 'locked': True, 'targets': keys,
                'target_status': {k: ('APPLIED' if k in state['applications'] else decision(state['jobs'][k], day)) for k in keys},
                'note': 'Same-day list preserved. Do not apply to a target now marked SKIP or VERIFY.'}
    candidates = [k for k,v in state['jobs'].items()
                  if k not in state['applications'] and decision(v, day) == 'RECOMMEND']
    # Use transparent coarse ordering, not an invented probability of hiring.
    candidates.sort(key=lambda k: (state['jobs'][k]['assessment']['fit'] != 'GOOD FIT',
                                   state['jobs'][k]['date_found'], k))
    state['radars'][day] = candidates[:2]
    state['events'].append({'date': day, 'action': 'shortlist', 'keys': candidates[:2]})
    return {'date': day, 'locked': False, 'targets': candidates[:2],
            'note': 'At most two. Zero is a valid result. Demo roles are fictional.'}

def confirm(state, k, day, confirmed):
    if not confirmed:
        raise ValueError('Requires explicit user confirmation; use --user-confirmed only after submission.')
    if state['jobs'][k]['posting']['source_kind'] == 'demo':
        raise ValueError('Fictional demo roles cannot count as applications.')
    if k in state['applications']:
        return {'counted': False, 'reason': 'Already recorded'}
    state['applications'][k] = {'date_applied': day, 'result': None}
    state['events'].append({'date': day, 'action': 'user_confirmed_application', 'key': k})
    return {'counted': True}

def summary(state, day):
    return {'today_confirmed': sum(v['date_applied'] == day for v in state['applications'].values()),
            'total_confirmed_in_this_tracker': len(state['applications']),
            'note': 'Historical applications are not imported automatically. This is not your lifetime total.'}

def export(state, path, day):
    fields = ['Company','Role','Job ID','Date Found','Fit','Work Authorization','Status','Date Applied','Result','Source URL','Decision']
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path,'w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for k,e in state['jobs'].items():
            p,a=e['posting'],e['assessment'] or {};app=state['applications'].get(k,{})
            row={'Company':p['company'],'Role':p['role'],'Job ID':p.get('job_id',''),
                 'Date Found':e['date_found'],'Fit':a.get('fit',''),
                 'Work Authorization':a.get('work_authorization',{}).get('status','uncertain'),
                 'Status':'APPLIED' if app else decision(e,day),'Date Applied':app.get('date_applied',''),
                 'Result':app.get('result') or '', 'Source URL':p['source_url'],'Decision':decision(e,day)}
            # Protect spreadsheet consumers from formula-like values.
            w.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
    return {'exported':str(path),'rows':len(state['jobs'])}

def main():
    # Real posting text can contain Unicode punctuation (e.g. narrow no-break
    # spaces) that a Windows console's default codepage cannot print, even
    # though the JSON itself is stored correctly on disk. Force UTF-8 stdout
    # so `read`/`list`/etc. don't crash on real-world content.
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',default='private/state.json')
    p.add_argument('--date',help='Dallas calendar date YYYY-MM-DD; default America/Chicago')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('list');sub.add_parser('shortlist');sub.add_parser('summary')
    q=sub.add_parser('ingest');q.add_argument('file')
    q=sub.add_parser('read');q.add_argument('key')
    q=sub.add_parser('assess');q.add_argument('key');q.add_argument('file')
    q=sub.add_parser('confirm');q.add_argument('key');q.add_argument('--user-confirmed',action='store_true')
    q=sub.add_parser('export');q.add_argument('file')
    args=p.parse_args()
    try:
        day=args.date or today();date.fromisoformat(day)
        state=read(args.state) if Path(args.state).exists() else empty()
        if args.command=='ingest': result=ingest(state,read(args.file),day)
        elif args.command=='read': result=state['jobs'][args.key]
        elif args.command=='assess': result=assess(state,args.key,read(args.file),day)
        elif args.command=='shortlist': result=shortlist(state,day)
        elif args.command=='confirm': result=confirm(state,args.key,day,args.user_confirmed)
        elif args.command=='summary': result=summary(state,day)
        elif args.command=='export': result=export(state,args.file,day)
        else: result=[{'key':k,'company':v['posting']['company'],'role':v['posting']['role'],'decision':decision(v,day)} for k,v in state['jobs'].items()]
        if args.command in ('ingest','assess','shortlist','confirm'): write(args.state,state)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,KeyError,OSError) as e:
        p.exit(1,'Error: '+str(e)+'\n')

if __name__=='__main__':
    main()
