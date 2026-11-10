import base64
import copy
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from subtitle.core.authorization import canonical, verify

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
def ts(value): return value.strftime('%Y-%m-%dT%H:%M:%SZ')

def sign(obj, key, domain):
    payload = dict(obj)
    payload.pop('signature', None)
    payload['signature'] = base64.b64encode(key.sign(domain + canonical(payload))).decode('ascii')
    return json.dumps(payload, ensure_ascii=False).encode()

class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.grant_key, self.status_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        self.g = dict(schema_version='1', grant_id='grant-1', job_id='job-1', issuer_id='owner', key_id='g1',
            issued_at=ts(NOW-timedelta(hours=1)), expires_at=ts(NOW+timedelta(hours=1)),
            source_ref=dict(provider='test', id='source-1', revision='r1', sha256='a'*64),
            style_id='viralpop', style_overrides={}, allowed_actions=['render_candidate','deliver_to_owner_review_destination','transcribe_using_allowed_processor'],
            allowed_processors=['groq'], review_destination=dict(provider='test', id='review-1', owner_id='philip'), retry_allowance=2)
        self.s = dict(schema_version='1',grant_id='grant-1',job_id='job-1',issuer_id='status',key_id='s1',checked_at=ts(NOW),status='active')
        self.args = dict(trusted_grant_keys={('owner','g1'):self.grant_key.public_key()}, trusted_status_keys={('status','s1'):self.status_key.public_key()}, now=NOW,
            job_id='job-1',source_ref=copy.deepcopy(self.g['source_ref']),style_id='viralpop',style_overrides={},
            review_destination=copy.deepcopy(self.g['review_destination']),action='render_candidate',processor=None,existing_binding=None)
        self.renderer, self.delivery = Mock(), Mock()
    def run_gate(self, g=None, s=None, args=None, raw=None, status_raw=None):
        decision = verify(raw if raw is not None else sign(g or self.g, self.grant_key, b'ThaiSubburn JobGrant v1\x00'),
            status_raw if status_raw is not None else sign(s or self.s, self.status_key, b'ThaiSubburn Revocation v1\x00'), **(args or self.args))
        if decision.allowed:
            (self.delivery if (args or self.args)['action'].startswith('deliver') else self.renderer)()
        return decision
    def denied(self, **kw):
        self.assertFalse(self.run_gate(**kw).allowed)
        self.renderer.assert_not_called(); self.delivery.assert_not_called()
    def test_valid_and_same_job_resume(self):
        self.assertTrue(self.run_gate().allowed)
        self.args['existing_binding']='job-1'
        self.assertTrue(self.run_gate().allowed)
    def test_signature_tamper_and_untrusted(self):
        raw=sign(self.g,self.grant_key,b'ThaiSubburn JobGrant v1\x00').replace(b'viralpop',b'paper-chips')
        self.denied(raw=raw)
        self.args['trusted_grant_keys']={}; self.denied()
    def test_strict_schema_and_json(self):
        for raw in [b'{}', b'{"schema_version":"1","schema_version":"1"}', b'NaN',b'\xff',b'1.2']:
            with self.subTest(raw=raw): self.denied(raw=raw)
        g=dict(self.g, unknown_permission=True); self.denied(g=g)
        g=dict(self.g, allowed_actions=['publish']); self.denied(g=g)
        g=dict(self.g, retry_allowance=True); self.denied(g=g)
    def test_time_boundaries(self):
        for g in [dict(self.g, expires_at=ts(NOW)),dict(self.g,issued_at=ts(NOW+timedelta(seconds=1))),dict(self.g,issued_at=ts(NOW-timedelta(hours=24)),expires_at=ts(NOW+timedelta(seconds=1)))]:
            with self.subTest(g=g): self.denied(g=g)
    def test_status(self):
        for s in [dict(self.s,status='revoked'),dict(self.s,checked_at=ts(NOW-timedelta(minutes=5,seconds=1))),dict(self.s,checked_at=ts(NOW+timedelta(seconds=1))),dict(self.s,job_id='other')]:
            with self.subTest(s=s): self.denied(s=s)
        self.denied(status_raw=b'{}')
        self.denied(status_raw=sign(self.s,self.grant_key,b'ThaiSubburn Revocation v1\x00'))
        s=dict(self.s,checked_at=ts(NOW-timedelta(minutes=5)))
        self.assertTrue(self.run_gate(s=s).allowed)
    def test_context_mismatch_and_replay(self):
        for field, value in [('job_id','other'),('source_ref',dict(self.g['source_ref'],revision='r2')),('style_id','paper-chips'),('style_overrides',{'font':'x'}),('review_destination',dict(self.g['review_destination'],id='elsewhere')),('action','publish'),('processor','unapproved'),('existing_binding','other')]:
            with self.subTest(field=field):
                args=dict(self.args,**{field:value}); self.denied(args=args)
    def test_delivery_denied_without_fresh_status(self):
        args=dict(self.args,action='deliver_to_owner_review_destination')
        self.denied(args=args,s=dict(self.s,checked_at=ts(NOW-timedelta(minutes=6))))
    def test_ed25519_rfc8032_vector(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex('d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'))
        sig = bytes.fromhex('e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
                            '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b')
        key.verify(sig, b'')

    def test_canonical_vector(self):
        self.assertEqual(canonical({'z':'ไทย','a':1}),b'{"a":1,"z":"\\u0e44\\u0e17\\u0e22"}')

if __name__ == '__main__': unittest.main()
