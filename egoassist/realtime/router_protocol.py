"""Compact provider wire format; the runtime still receives full validated labels."""

from egoassist.prompts import prompt_text

LEGACY_DEFAULTS = dict.fromkeys((
    'speechControl', 'taskControl', 'preferenceIntent', 'memoryQuery', 'memoryKind',
    'memoryText', 'memoryKey', 'memoryScope', 'memoryConsent', 'skillControl', 'skill',
), 'none')
WIRE_FIELDS = {
    'speechControl', 'taskControl', 'preferenceIntent', 'memoryQuery', 'skillControl',
    'skill', 'memoryDraft', 'memoryAtoms', 'memoryEvents', 'memoryQuerySpec', 'intentKind', 'intents', 'revision', 'interaction',
}
EVENT_CONTENT_FIELDS = {'text', 'subject', 'predicate', 'value', 'kind'}


def expand_decision(parsed):
    if not isinstance(parsed, dict):
        raise ValueError('router output must be a JSON object')
    if 'v' not in parsed:
        return parsed  # historical full protocol, validated by the caller
    extra = set(parsed) - WIRE_FIELDS - {'v'}
    ignored = extra if 'memoryEvents' in parsed and extra <= EVENT_CONTENT_FIELDS else set()
    if type(parsed['v']) is not int or parsed['v'] != 1 or extra - ignored:
        raise ValueError('invalid compact router schema')
    result = {**LEGACY_DEFAULTS, **{k: v for k, v in parsed.items() if k != 'v' and k not in ignored}}
    if 'memoryEvents' in result:
        events = result.pop('memoryEvents')
        if not isinstance(events, list) or not isinstance(result.get('memoryAtoms', []), list):
            raise ValueError('memory events and atoms must be lists')
        # Some providers repeat the implied event kind. Accept that exact
        # redundancy without allowing a different kind or extra memory fields.
        if any(not isinstance(item, dict)
               or set(item) - {'scope', 'consent', 'context', 'text', 'kind'}
               or item.get('kind', 'event') != 'event' for item in events):
            raise ValueError('invalid memory event envelope')
        result['memoryAtoms'] = [*result.get('memoryAtoms', []), *[{'kind': 'event', **item} for item in events]]
    query = result.get('memoryQuery')
    # A bounded read-only query envelope has the same meaning as the canonical
    # query spec. Conflicting envelopes and unknown fields remain invalid.
    if isinstance(query, dict):
        if set(query) - {'referential', 'subject', 'predicate', 'context'} or query.get('referential') is not True:
            raise ValueError('invalid memory query envelope')
        if parsed.get('memoryQuerySpec') is not None:
            raise ValueError('conflicting memory query envelopes')
        result['memoryQuerySpec'] = {'subject': query.get('subject'), 'predicate': query.get('predicate')}
        if 'context' in query:
            result['memoryQuerySpec']['context'] = query['context']
        result['memoryQuery'] = 'referential'
    if result.get('memoryQuerySpec') and 'memoryQuery' not in parsed:
        result['memoryQuery'] = 'referential'
    return result


ROUTER_PROMPT = prompt_text('router_protocol/ROUTER_PROMPT.1')
