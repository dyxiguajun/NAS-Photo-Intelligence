"""Local append-only exchange for the three NAS-Photo-Intelligence agents."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
AGENTS = {
    'codex':Path('reports/agent-coordination/codex'),
    'dsh':Path('reports/datarestore-audit/dsh/coordination'),
    'opencode':Path('reports/agent-coordination/opencode'),
}
CENTRAL = Path('reports/agent-coordination')
KINDS = {'INFO','TASK_REQUEST','TASK_ACCEPTED','TASK_COMPLETED','BLOCKED','BUG_REPORT',
         'REVIEW_REQUEST','DECISION_PROPOSAL','DECISION_CONFIRMED'}
KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')
TASK = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}')
DIGEST = re.compile(r'[a-f0-9]{64}')
MAX_BYTES = 64*1024


def encode(item):
    return (json.dumps(item,sort_keys=True,ensure_ascii=False,allow_nan=False)+'\n').encode()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def within(project, path):
    target = project/path
    if target.resolve()!=target:
        raise ValueError(f'Symlink in coordination path: {path}')
    return target


def publish_file(path, data):
    """Commit a whole message once, even with concurrent writers."""
    path.parent.mkdir(parents=True,exist_ok=True)
    with NamedTemporaryFile(prefix='.draft-',dir=path.parent,delete=False) as staging:
        temp = Path(staging.name)
        try:
            staging.write(data)
            staging.flush()
            os.fsync(staging.fileno())
            try:
                os.link(temp,path)
            except FileExistsError:
                return False
            fd = os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        finally:
            temp.unlink()


def validate(item, sender, filename):
    required = {'message_id','sender','recipients','time','type','task_id',
                'priority','body','artifacts','reply_required'}
    optional = {'in_reply_to','manifest_sha256','related_task_ids','related_reply_ids'}
    if not isinstance(item,dict) or item.keys()-required-optional or not required<=item.keys():
        raise ValueError('Missing or unsupported message fields')
    if (item['sender']!=sender or not KEY.fullmatch(item['message_id'])
            or (filename.endswith('.json') and filename!=item['message_id']+'.json')):
        raise ValueError('Message identity does not match its outbox')
    recipients = item['recipients']
    if (not isinstance(recipients,list) or not recipients or not set(recipients)<=AGENTS.keys()-{sender}
            or len(set(recipients))!=len(recipients)):
        raise ValueError('Recipients must be distinct project agents')
    if datetime.fromisoformat(item['time']).utcoffset() is None:
        raise ValueError('Message time must include a timezone')
    if item['type'] not in KINDS or item['priority'] not in ('P0','P1','P2','P3') or not TASK.fullmatch(item['task_id']):
        raise ValueError('Unsupported type, priority or task ID')
    if (not isinstance(item['body'],str) or not item['body'].strip()
            or type(item['reply_required']) is not bool or not isinstance(item['artifacts'],list)):
        raise ValueError('Message body, attachments or reply flag is invalid')
    for link in item['artifacts']:
        path = PurePosixPath(link)
        if path.is_absolute() or str(path)!=link or link=='.' or '..' in path.parts:
            raise ValueError('Artifact paths must stay inside the project')
    if 'in_reply_to' in item and not KEY.fullmatch(item['in_reply_to']):
        raise ValueError('Invalid reply identity')
    if 'manifest_sha256' in item and not DIGEST.fullmatch(item['manifest_sha256']):
        raise ValueError('Invalid frozen manifest digest')
    for related, primary, pattern in (('related_task_ids','task_id',TASK),('related_reply_ids','in_reply_to',KEY)):
        if related not in item:
            continue
        values = item[related]
        if (not isinstance(values,list) or primary not in item or not values
                or any(not isinstance(value,str) or not pattern.fullmatch(value) for value in values)
                or len(set(values))!=len(values) or item[primary] in values):
            raise ValueError(f'Invalid {related}')


def legacy_list(value):
    if value.startswith('[') and value.endswith(']'):
        value = value[1:-1]
    return [part.strip() for part in value.split(',')]


def send(project, sender, *, recipients, kind, task_id, priority, body, artifacts, manifest_sha256,
         reply_required=False, in_reply_to=None, idempotency_key=None):
    if kind=='TASK_ACCEPTED':
        idempotency_key = f'accept:{task_id}'
    if idempotency_key:
        token = sha256(encode([sender,kind,task_id,idempotency_key])).hexdigest()[:24]
        message_id = f'{sender}-{token}'
    else:
        message_id = f'{sender}-{uuid4().hex}'
    item = {'message_id':message_id,'sender':sender,'recipients':sorted(recipients),'time':now(),
            'type':kind,'task_id':task_id,'priority':priority,'body':body.strip(),
            'artifacts':list(artifacts),'reply_required':reply_required,'manifest_sha256':manifest_sha256}
    if in_reply_to:
        item['in_reply_to'] = in_reply_to
    path = within(project,AGENTS[sender]/'outbox'/(message_id+'.json'))
    validate(item,sender,path.name)
    data = encode(item)
    if len(data)>MAX_BYTES:
        raise ValueError('Message exceeds the 64 KiB limit')
    created = publish_file(path,data)
    if not created:
        existing = json.loads(path.read_bytes())
        validate(existing,sender,path.name)
        if kind!='TASK_ACCEPTED' and {**existing,'time':item['time']}!=item:
            raise ValueError('Idempotency key is already assigned to different content')
    return {'message_id':message_id,'created':created,'path':str(path.relative_to(project))}


def read_message(path):
    if path.suffix=='.json':
        return json.loads(path.read_bytes())
    text = path.read_text()
    if not text.startswith('---\n'):
        return read_markdown_note(text)
    header, separator, body = text[4:].partition('\n---\n')
    if not separator:
        raise ValueError('Unclosed Markdown message header')
    fields = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(':')
        if not sep or key in fields:
            raise ValueError('Malformed Markdown message header')
        fields[key] = value.strip()
    priority = fields['priority']
    reply = fields['reply_required'].lower()
    if reply not in ('true','false'):
        raise ValueError('Invalid Markdown reply flag')
    task,*related_tasks = legacy_list(fields['task_id'])
    item = {'message_id':fields['message_id'],'sender':fields['from'].lower(),
            'recipients':[name.lower() for name in legacy_list(fields['to'])],
            'time':fields['time'],'type':fields['type'],'task_id':task,
            'priority':{'urgent':'P0','high':'P1','normal':'P2','low':'P3'}.get(priority.lower(),priority),
            'body':body.strip(),'artifacts':[],'reply_required':reply=='true'}
    if related_tasks:
        item['related_task_ids'] = related_tasks
    if 'in_reply_to' in fields:
        reply_to,*related_replies = legacy_list(fields['in_reply_to'])
        item['in_reply_to'] = reply_to
        if related_replies:
            item['related_reply_ids'] = related_replies
    if 'manifest_sha256' in fields:
        item['manifest_sha256'] = fields['manifest_sha256']
    return item


def read_markdown_note(text):
    """Read the compact header used by OpenCode's initial handoff."""
    fields = {}
    header, _, body = text.partition('\n## ')
    for line in header.splitlines():
        if line.startswith('- '):
            key, sep, value = line[2:].partition(':')
            if not sep or key in fields:
                raise ValueError('Malformed Markdown message header')
            fields[key] = value.strip()
    recipients = [name.split()[0].lower() for name in fields['Recipients'].split(',')]
    priority = fields['Priority']
    reply = fields['Reply required'].split()[0].lower()
    if reply not in ('yes','no'):
        raise ValueError('Invalid Markdown reply flag')
    item = {'message_id':fields['Message ID'],'sender':fields['Sender'].lower(),
            'recipients':recipients,'time':fields['Time'],'type':fields['Type'].split()[0],
            'task_id':fields['Related task'].split()[0],'body':body.strip(),'artifacts':[],
            'priority':{'urgent':'P0','high':'P1','normal':'P2','low':'P3'}.get(priority.lower(),priority),
            'reply_required':reply=='yes'}
    if 'Manifest SHA-256' in fields:
        item['manifest_sha256'] = fields['Manifest SHA-256']
    return item


def read_messages(project, agent):
    messages, errors = {}, []
    for sender, area in AGENTS.items():
        if sender==agent:
            continue
        try:
            outbox = within(project,area/'outbox')
        except (OSError,ValueError) as error:
            errors.append({'path':str(area/'outbox'),'error':str(error)})
            continue
        candidates = [*outbox.glob('*.json'), *(path for path in outbox.glob('*.md') if path.name!='README.md')]
        for path in sorted(candidates):
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_BYTES:
                    raise ValueError('Expected a small regular message file')
                item = read_message(path)
                validate(item,sender,path.name)
                if agent in item['recipients']:
                    msg_id = item['message_id']
                    if msg_id in messages and messages[msg_id]!=item:
                        raise ValueError('Conflicting message identity')
                    messages[msg_id] = item
            except (OSError,ValueError,TypeError,LookupError) as error:
                errors.append({'path':str(path.relative_to(project)),'error':str(error)})
    return messages, errors


def acknowledge(project, agent, message_id):
    messages, _ = read_messages(project,agent)
    if message_id not in messages:
        raise ValueError('Cannot acknowledge a message outside your inbox')
    old = within(project,AGENTS[agent]/'receipts'/(message_id+'.json'))
    if old.is_file():
        return False
    path = within(project,AGENTS[agent]/'inbox'/('receipt-'+message_id+'.json'))
    return publish_file(path,encode({'message_id':message_id,'recipient':agent,'processed_at':now(),
                                     'manifest_sha256':messages[message_id].get('manifest_sha256')}))


def sync(project, agent, include_read=False):
    messages, errors = read_messages(project,agent)
    receipts = within(project,AGENTS[agent]/'receipts')
    inbox_path = within(project,AGENTS[agent]/'inbox')
    inbox = []
    for item in sorted(messages.values(),key=lambda x:(x['priority'],x['time'],x['message_id'])):
        receipt = inbox_path/('receipt-'+item['message_id']+'.json')
        if not receipt.exists():
            receipt = receipts/(item['message_id']+'.json')
        processed = False
        if receipt.exists():
            try:
                if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size>MAX_BYTES:
                    raise ValueError('Expected a small regular receipt file')
                row = json.loads(receipt.read_bytes())
                if (row['message_id']!=item['message_id'] or row['recipient']!=agent
                        or datetime.fromisoformat(row['processed_at']).utcoffset() is None):
                    raise ValueError('Receipt identity or time mismatch')
                processed = True
            except (OSError,ValueError,TypeError,KeyError) as error:
                errors.append({'path':str(receipt.relative_to(project)),'error':str(error)})
        if include_read or not processed:
            inbox.append({**item,'processed':processed,'version_status':
                          'manifest_declared' if item.get('manifest_sha256') else 'legacy_manifest_unspecified'})
    outboxes = {}
    for sender,area in AGENTS.items():
        directory = area/'outbox'
        try:
            present = within(project,directory).is_dir()
        except (OSError,ValueError) as error:
            present = False
            if sender==agent:
                errors.append({'path':str(directory),'error':str(error)})
        outboxes[sender] = {'path':str(directory),'present':present}
    return {'agent':agent,
            'project_status':str(CENTRAL/'PROJECT_STATUS.md'),'task_board':str(CENTRAL/'TASK_BOARD.md'),
            'decision_log':str(CENTRAL/'DECISIONS.md'),
            'authoritative_state':str(CENTRAL/'STATE.json'),
            'outboxes':outboxes,
            'inbox':inbox,'errors':errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=AGENTS)
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('sync').add_argument('--all',action='store_true')
    ack = sub.add_parser('ack')
    ack.add_argument('message_id')
    publish = sub.add_parser('send')
    publish.add_argument('--to',required=True,choices=AGENTS,action='append')
    publish.add_argument('--type',required=True,choices=sorted(KINDS))
    publish.add_argument('--task',required=True)
    publish.add_argument('--priority',choices=('P0','P1','P2','P3'),default='P2')
    publish.add_argument('--body-file',type=Path)
    publish.add_argument('--artifact',action='append',default=[])
    publish.add_argument('--reply-required',action='store_true')
    publish.add_argument('--in-reply-to')
    publish.add_argument('--key')
    publish.add_argument('--manifest-sha256')
    args = parser.parse_args()
    if args.command=='sync':
        result = sync(PROJECT,args.agent,args.all)
    elif args.command=='ack':
        result = {'message_id':args.message_id,'created':acknowledge(PROJECT,args.agent,args.message_id)}
    else:
        manifest_sha = args.manifest_sha256
        if not manifest_sha:
            state = within(PROJECT,CENTRAL/'STATE.json')
            manifest_sha = json.loads(state.read_bytes())['manifest']['sha256']
        body = args.body_file.read_text() if args.body_file else sys.stdin.read(MAX_BYTES+1)
        result = send(PROJECT,args.agent,recipients=args.to,kind=args.type,task_id=args.task,
                      priority=args.priority,body=body,artifacts=args.artifact,reply_required=args.reply_required,
                      in_reply_to=args.in_reply_to,idempotency_key=args.key,manifest_sha256=manifest_sha)
    print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__=='__main__':
    main()
