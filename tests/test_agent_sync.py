from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from agent_sync import AGENTS, acknowledge, send, sync


class AgentSyncTest(unittest.TestCase):
    def test_symlinked_outbox_does_not_interrupt_other_sender(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            unsafe = root/AGENTS['dsh']/'outbox'
            unsafe.parent.mkdir(parents=True)
            unsafe.symlink_to(root/'other',target_is_directory=True)
            sent = send(root,'opencode',recipients=['codex'],kind='INFO',task_id='NAS-005',
                        priority='P1',body='Valid independent message',artifacts=[],manifest_sha256='c'*64)
            received = sync(root,'codex')
            self.assertEqual([m['message_id'] for m in received['inbox']],[sent['message_id']])
            self.assertEqual(len(received['errors']),1)
            self.assertFalse(received['outboxes']['dsh']['present'])

    def test_atomic_publish_read_and_receipt(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()

            def assign(_):
                return send(root,'codex',recipients=['dsh'],kind='TASK_REQUEST',task_id='NAS-008',priority='P0',
                            body='Review checkpoint integrity',artifacts=[],reply_required=True,idempotency_key='test',
                            manifest_sha256='a'*64)

            with ThreadPoolExecutor(max_workers=6) as pool:
                assignments = list(pool.map(assign,range(12)))
            self.assertEqual(sum(row['created'] for row in assignments),1)
            id_, = {row['message_id'] for row in assignments}
            self.assertEqual(sync(root,'dsh')['inbox'][0]['message_id'],id_)
            self.assertFalse(sync(root,'opencode')['inbox'])
            with self.assertRaises(ValueError):
                acknowledge(root,'opencode',id_)
            self.assertTrue(acknowledge(root,'dsh',id_))
            self.assertFalse(acknowledge(root,'dsh',id_))
            self.assertEqual(sync(root,'dsh')['inbox'],[])
            self.assertTrue(sync(root,'dsh',include_read=True)['inbox'][0]['processed'])
            self.assertEqual([p.name for p in (root/AGENTS['codex']/'outbox').iterdir()],[id_+'.json'])

    def test_markdown_inbox_and_bad_message_isolation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            outbox = root/AGENTS['dsh']/'outbox'
            outbox.mkdir(parents=True)
            message = outbox/'legacy.md'
            message.write_text("""---
message_id: DSH-0001
from: DSH
to: [Codex, OpenCode]
time: 2026-11-11T21:00:00Z
type: INFO
task_id: G2.2
priority: normal
reply_required: true
---

Delivered evidence.
""")
            (outbox/'bad.json').write_text('{')
            oc = root/AGENTS['opencode']/'outbox'
            oc.mkdir(parents=True)
            (oc/'note.md').write_text("""# OC-0001
- Message ID: OC-0001
- Sender: OpenCode
- Recipients: Codex (primary), DSH (cc)
- Time: 2026-11-11T21:01:00Z
- Type: INFO + TASK_REQUEST
- Related task: OC-001 (bootstrap)
- Priority: high
- Reply required: yes

## Body
Review complete.
""")
            result = sync(root,'codex')
            self.assertEqual([r['message_id'] for r in result['inbox']],['OC-0001','DSH-0001'])
            self.assertEqual(len(result['errors']),1)
            self.assertTrue(acknowledge(root,'codex','DSH-0001'))

    def test_task_acceptance_has_a_single_claim(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            kwargs = dict(recipients=['codex'],kind='TASK_ACCEPTED',task_id='NAS-003',
                          priority='P1',body='Work in progress',artifacts=[],manifest_sha256='a'*64)
            first = send(root,'dsh',**kwargs)
            next_ = send(root,'dsh',**{**kwargs,'body':'Repeat after reconnect'})
            self.assertTrue(first['created'])
            self.assertFalse(next_['created'])
            self.assertEqual(first['message_id'],next_['message_id'])
            self.assertEqual(sync(root,'codex')['inbox'][0]['body'],'Work in progress')
