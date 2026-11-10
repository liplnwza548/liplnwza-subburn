"""Offline P2-002 verifier. Inputs named trusted must come from a protected host.

No issuer, renderer, delivery, persistent replay ledger or execution isolation lives here.
"""
import base64
import binascii
import json
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ACTIONS = frozenset({'intake_copy', 'transcribe_using_allowed_processor', 'prepare_script',
                     'mfa_align', 'render_candidate', 'qc', 'deliver_to_owner_review_destination'})
FIELDS = frozenset({'schema_version','grant_id','job_id','issuer_id','key_id','issued_at','expires_at',
                    'source_ref','style_id','style_overrides','allowed_actions','allowed_processors',
                    'review_destination','retry_allowance','signature'})
STATUS_FIELDS = frozenset({'schema_version','grant_id','job_id','issuer_id','key_id','checked_at','status','signature'})
SOURCE_FIELDS = frozenset({'provider','id','revision','sha256'})
DEST_FIELDS = frozenset({'provider','id','owner_id'})
TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
IDENT = re.compile(r'^[A-Za-z0-9._:@/-]{1,256}$')
DIGEST = re.compile(r'^[0-9a-f]{64}$')

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

class Invalid(Exception):
    pass

def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise Invalid('duplicate key')
        result[key] = value
    return result

def _parse(raw):
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise Invalid('invalid input')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_float=lambda _: (_ for _ in ()).throw(Invalid('float')),
                          parse_constant=lambda _: (_ for _ in ()).throw(Invalid('constant')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Invalid('invalid json') from exc

def canonical(payload):
    """Version 1: UTF-8 JSON, sorted ASCII keys, compact, ASCII escapes; no floats."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')

def _id(value):
    return type(value) is str and bool(IDENT.fullmatch(value))

def _date(value):
    if type(value) is not str or not TIMESTAMP.fullmatch(value):
        raise Invalid('date')
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise Invalid('date') from exc

def _object(value, fields):
    return type(value) is dict and value.keys() == fields and all(_id(v) for v in value.values())

def _ids(value, allowed=None):
    return (type(value) is list and len(value) <= 32 and all(_id(v) for v in value)
            and len(set(value)) == len(value) and (allowed is None or set(value) <= allowed))

def _signed(obj, keys, domain):
    signature = obj.get('signature')
    if type(signature) is not str:
        raise Invalid('signature')
    try:
        raw = base64.b64decode(signature, validate=True)
        if len(raw) != 64 or base64.b64encode(raw).decode('ascii') != signature:
            raise Invalid('signature encoding')
        key = keys.get((obj.get('issuer_id'), obj.get('key_id')))
        if not isinstance(key, Ed25519PublicKey):
            raise Invalid('untrusted key')
        payload = {k: v for k, v in obj.items() if k != 'signature'}
        key.verify(raw, domain + canonical(payload))
    except (ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise Invalid('signature') from exc

def verify(grant_bytes, status_bytes, *, trusted_grant_keys, trusted_status_keys,
           now, job_id, source_ref, style_id, style_overrides, review_destination,
           action, processor, existing_binding):
    """Return a decision only. Caller must use authenticated host inputs and recheck each action.

    existing_binding is trusted ledger lookup: None if unbound, otherwise the job ID.
    Matching it here does not atomically reserve a grant; P2-004 owns that operation.
    """
    deny = lambda reason: Decision(False, reason)
    try:
        g = _parse(grant_bytes)
        if type(g) is not dict or g.keys() != FIELDS or g['schema_version'] != '1':
            return deny('AUTH_SCHEMA_INVALID')
        if not all(_id(g[x]) for x in ('grant_id','job_id','issuer_id','key_id')):
            return deny('AUTH_SCHEMA_INVALID')
        if not _object(g['source_ref'], SOURCE_FIELDS) or not DIGEST.fullmatch(g['source_ref']['sha256']):
            return deny('AUTH_SCHEMA_INVALID')
        if not _object(g['review_destination'], DEST_FIELDS):
            return deny('AUTH_SCHEMA_INVALID')
        # Override options have not yet been qualified. Empty is the only safe v1 set.
        if g['style_id'] not in ('paper-chips','viralpop') or g['style_overrides'] != {} or type(g['style_overrides']) is not dict:
            return deny('STYLE_NOT_AUTHORIZED')
        if not _ids(g['allowed_actions'], ACTIONS) or not _ids(g['allowed_processors']):
            return deny('ACTION_NOT_AUTHORIZED')
        if type(g['retry_allowance']) is not int or not 0 <= g['retry_allowance'] <= 2:
            return deny('AUTH_SCHEMA_INVALID')
        _signed(g, trusted_grant_keys, b'ThaiSubburn JobGrant v1\x00')
        issued, expires = _date(g['issued_at']), _date(g['expires_at'])
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return deny('AUTH_TIME_INVALID')
        if not issued <= now < expires or expires - issued > timedelta(hours=24):
            return deny('AUTH_EXPIRED_OR_NOT_YET_VALID')
        s = _parse(status_bytes)
        if type(s) is not dict or s.keys() != STATUS_FIELDS or s['schema_version'] != '1':
            return deny('REVOCATION_INVALID')
        if not all(_id(s[x]) for x in ('grant_id','job_id','issuer_id','key_id')) or s['status'] not in ('active','revoked'):
            return deny('REVOCATION_INVALID')
        _signed(s, trusted_status_keys, b'ThaiSubburn Revocation v1\x00')
        checked = _date(s['checked_at'])
        if s['grant_id'] != g['grant_id'] or s['job_id'] != g['job_id'] or not timedelta(0) <= now - checked <= timedelta(minutes=5):
            return deny('REVOCATION_STALE_OR_MISMATCH')
        if s['status'] != 'active':
            return deny('AUTH_REVOKED')
        if g['job_id'] != job_id or (existing_binding is not None and existing_binding != job_id):
            return deny('GRANT_REPLAY')
        if g['source_ref'] != source_ref:
            return deny('SOURCE_MISMATCH')
        if g['style_id'] != style_id or g['style_overrides'] != style_overrides:
            return deny('STYLE_NOT_AUTHORIZED')
        if g['review_destination'] != review_destination:
            return deny('DESTINATION_NOT_AUTHORIZED')
        if action not in ACTIONS or action not in g['allowed_actions'] or (processor is not None and processor not in g['allowed_processors']):
            return deny('ACTION_NOT_AUTHORIZED')
        if action == 'transcribe_using_allowed_processor' and processor is None:
            return deny('ACTION_NOT_AUTHORIZED')
        return Decision(True, 'AUTHORIZED')
    except (Invalid, KeyError, TypeError, ValueError, OverflowError):
        return deny('AUTH_INVALID_OR_UNTRUSTED')
