"""Calendar annotations for source expressions; never infer event ownership or timezone."""
from datetime import datetime
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .temporal import date_mentions


def annotate(source):
    stamp = source.get('sourceAt')
    if not stamp:
        return {**source, 'timeExpressions': [], 'timeAnnotationVersion': 1}
    at = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if at.tzinfo is not None and source.get('timezone'):
        at = at.astimezone(ZoneInfo(source['timezone']))
    # date_mentions needs a local calendar, not an invented UTC instant.
    expressions = [{'expression': text, 'localDate': day.isoformat() if day else None,
                    'precision': 'day'} for text, day in date_mentions(source['text'], SimpleNamespace(local_now=at))]
    for match in re.finditer(r'\b(last|this|next) (year|month)\b|去年|今年|明年|上个月|这个月|下个月', source['text'], re.I):
        text = match[0].lower()
        offset = -1 if text.startswith('last') or text in {'去年', '上个月'} else 1 if text.startswith('next') or text in {'明年', '下个月'} else 0
        if 'year' in text or '年' in text:
            year = at.year + offset
            if 1 <= year <= 9999:
                expressions.append({'expression': match[0], 'calendarPeriod': str(year), 'precision': 'year'})
        else:
            year, month = divmod(at.year * 12 + at.month - 1 + offset, 12)
            if 1 <= year <= 9999:
                expressions.append({'expression': match[0], 'calendarPeriod': f'{year:04}-{month+1:02}', 'precision': 'month'})
    return {**source, 'timeExpressions': expressions, 'timeAnnotationVersion': 1,
        'timeExpressionBoundary': 'Calendar expansions anchored to sourceAt, not event claims. Associate each expression with its own clause; quoted speech can have a different anchor. Timezone remains as supplied.'}
