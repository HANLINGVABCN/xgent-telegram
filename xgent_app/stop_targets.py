"""A visible stop control belongs to one active phase of a running conversation."""
from __future__ import annotations

import secrets

_phases = {}


def begin_phase(run_id, kind):
    if not run_id:
        return None
    ref = secrets.token_hex(8)
    _phases[str(run_id)] = {'ref': ref, 'kind': kind}
    return ref


def phase_ref(run_id):
    return (_phases.get(str(run_id)) or {}).get('ref')


def end_phase(run_id, ref=None):
    if ref is None or phase_ref(run_id) == ref:
        _phases.pop(str(run_id), None)


def active_target(run_id, ref):
    return bool(run_id and ref and phase_ref(run_id) == ref)


def admit_remote_phase(context, spec):
    """CLI relay phases are admitted only while their server-owned run is live."""
    if not context or context.get('origin') != 'cli' or not spec.get('ref'):
        return
    from .conversations import get_conversations
    try:
        run = get_conversations().runtime(context.get('run_id'))
    except RuntimeError:
        return
    if run is None or (run.scope.conversation_id, run.scope.generation) != (
            context.get('conversation_id'), context.get('generation')):
        return
    run_id, ref = context['run_id'], spec['ref']
    if spec.get('phase') == 'control' or spec.get('running'):
        _phases[run_id] = {'ref': ref, 'kind': 'relay'}
    elif spec.get('phase') == 'reply':
        end_phase(run_id, ref)
