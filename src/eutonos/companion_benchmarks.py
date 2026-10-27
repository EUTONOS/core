"""Project explicit benchmark stages from native run IDs, without model calls."""
import json
import hashlib
from datetime import datetime
from pathlib import Path, PurePosixPath

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
REPORT_ROOT = ('.eutonos-local', 'reports')


def report_metadata(value):
    """Return structurally safe opaque-report metadata, without reading the report."""
    path, digest = value.get('report_path'), value.get('report_sha256')
    if not isinstance(path, str) or not isinstance(digest, str) or '\\' in path:
        return None
    candidate = PurePosixPath(path)
    if (candidate.is_absolute() or candidate.parts[:2] != REPORT_ROOT
            or any(part in {'.', '..'} for part in candidate.parts)
            or len(candidate.parts) < 3):
        return None
    digest = digest.lower()
    if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
        return None
    return {'report_path': candidate.as_posix(), 'report_sha256': digest}


def read_report(root, report_path, report_sha256):
    """Read one hash-pinned report from the selected repository's report area."""
    metadata = report_metadata({'report_path': report_path, 'report_sha256': report_sha256})
    if metadata is None:
        raise ValueError('Invalid benchmark report reference')
    root = Path(root).resolve(strict=True)
    target = (root / Path(*PurePosixPath(metadata['report_path']).parts)).resolve(strict=True)
    report_root = (root / Path(*REPORT_ROOT)).resolve(strict=False)
    if not target.is_relative_to(report_root) or not target.is_file():
        raise ValueError('Benchmark report is unavailable')
    body = target.read_bytes()
    if hashlib.sha256(body).hexdigest() != metadata['report_sha256']:
        raise ValueError('Benchmark report changed')
    try:
        return body.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('Benchmark report is not UTF-8 text') from exc


def _number(value):
    return value if type(value) in (int, float) and value >= 0 else None


def _ram_side(value):
    text = str(value or '').strip().lower()
    if any(word in text for word in ('disabled', 'unavailable', 'off')):
        return 'off'
    if any(word in text for word in ('enabled', 'optional', 'on')):
        return 'on'
    return None


def _analysis(row, baseline, axis):
    """Describe one observed comparison without turning it into a routing claim."""
    if baseline is None:
        return {
            'status': 'insufficient-evidence', 'axis': axis, 'baseline_id': None,
            'label': 'No matched baseline',
            'summary': 'A matching recorded baseline is unavailable, so cost and quality tradeoffs are unknown.',
            'routing_signal': 'Collect a matched run before using this result for model or RAM selection.',
            'cost_delta_usd': None, 'cost_delta_percent': None, 'quality_delta': None,
            'input_delta': None, 'reasoning_delta': None, 'output_delta': None, 'token_delta': None,
        }
    if row is baseline:
        return {
            'status': 'baseline', 'axis': axis, 'baseline_id': baseline.get('id'),
            'label': 'Matched baseline',
            'summary': 'This result is the recorded baseline for comparable configurations in this run.',
            'routing_signal': 'Use as the within-run reference; it is not a universal model ranking.',
            'cost_delta_usd': 0, 'cost_delta_percent': 0, 'quality_delta': 0,
            'input_delta': 0, 'reasoning_delta': 0, 'output_delta': 0, 'token_delta': 0,
        }
    cost, base_cost = _number(row.get('answer', {}).get('cost_usd')), _number(baseline.get('answer', {}).get('cost_usd'))
    quality, base_quality = _number((row.get('quality') or {}).get('score')), _number((baseline.get('quality') or {}).get('score'))
    if cost is None or base_cost is None or quality is None or base_quality is None:
        return {
            'status': 'insufficient-evidence', 'axis': axis, 'baseline_id': baseline.get('id'),
            'label': 'Incomplete matched evidence',
            'summary': 'The matched result is missing a measured answer cost or reviewed quality score.',
            'routing_signal': 'Do not use this result for routing until the missing measurement is available.',
            'cost_delta_usd': None, 'cost_delta_percent': None, 'quality_delta': None,
            'input_delta': None, 'reasoning_delta': None, 'output_delta': None, 'token_delta': None,
        }
    cost_delta = cost - base_cost
    quality_delta = quality - base_quality
    if cost_delta <= 0 and quality_delta >= 0:
        status, label = 'candidate', 'Lower cost with equal or higher quality'
        signal = 'Promising candidate for a repeated matched test before it influences routing.'
    elif cost_delta < 0 and quality_delta < 0:
        status, label = 'tradeoff', 'Lower-cost quality tradeoff'
        signal = 'Use only if the observed quality loss is acceptable for this task class.'
    elif cost_delta > 0 and quality_delta > 0:
        status, label = 'tradeoff', 'Higher-cost quality gain'
        signal = 'Potential escalation path when the observed quality gain is worth the added cost.'
    else:
        status, label = 'baseline-favored', 'Matched baseline favored'
        signal = 'This sample does not support preferring the tested configuration over its baseline.'
    def delta(field):
        value, base = _number(row.get('answer', {}).get(field)), _number(baseline.get('answer', {}).get(field))
        return None if value is None or base is None else value - base
    input_delta, reasoning_delta, output_delta = (delta(field) for field in ('input_tokens', 'reasoning_output_tokens', 'output_tokens'))
    token_delta = None if input_delta is None or output_delta is None else input_delta + output_delta
    pct = None if base_cost == 0 else cost_delta / base_cost * 100
    return {
        'status': status, 'axis': axis, 'baseline_id': baseline.get('id'), 'label': label,
        'summary': 'Observed answer cost and rubric quality are compared only with the matched baseline in this run.',
        'routing_signal': signal, 'cost_delta_usd': cost_delta, 'cost_delta_percent': pct,
        'quality_delta': quality_delta, 'input_delta': input_delta, 'reasoning_delta': reasoning_delta,
        'output_delta': output_delta, 'token_delta': token_delta,
    }


def analyze_suite(suite):
    """Attach evidence-qualified interpretations to every projected result."""
    results = suite.get('results', [])
    test_type = suite.get('test_type')
    for row in results:
        baseline = None
        if test_type in ('companion', 'delegation-cost'):
            if not row.get('support_model'):
                baseline = row
            else:
                baseline = next((item for item in results
                                 if item.get('primary_model') == row.get('primary_model')
                                 and not item.get('support_model')), None)
        elif test_type == 'ram':
            side = _ram_side(row.get('ram_condition'))
            if side == 'off':
                baseline = row
            elif side == 'on':
                baseline = next((item for item in results
                                 if item.get('primary_model') == row.get('primary_model')
                                 and item.get('support_model') == row.get('support_model')
                                 and item.get('context_condition') == row.get('context_condition')
                                 and _ram_side(item.get('ram_condition')) == 'off'), None)
        row['analysis'] = _analysis(row, baseline, test_type or 'unknown')
    interpreted = [row['analysis'] for row in results if row['analysis']['status'] not in ('baseline', 'insufficient-evidence')]
    counts = {
        'candidate': sum(item['status'] == 'candidate' for item in interpreted),
        'tradeoff': sum(item['status'] == 'tradeoff' for item in interpreted),
        'baseline_favored': sum(item['status'] == 'baseline-favored' for item in interpreted),
    }
    suite['analysis'] = {
        'interpreted_count': len(interpreted),
        'baseline_count': sum(row['analysis']['status'] == 'baseline' for row in results),
        'insufficient_count': sum(row['analysis']['status'] == 'insufficient-evidence' for row in results),
        **counts,
        'scope': 'Descriptive within-run comparisons only; cohort, cache, scheduling and review limits still apply. No automatic routing decision is made.',
    }
    return suite


def stage(ids, by_id):
    empty = {**dict.fromkeys(FIELDS), 'cost_usd': None, 'tool_calls': None,
             'cost_by_model': None, 'effort': None, 'efforts': [],
             'credit_estimate': None, 'credit_priced_run_count': 0, 'credit_unpriced_run_count': len(ids) if isinstance(ids, list) else 0,
             'worker_seconds': None, 'elapsed_seconds': None, 'tool_counts': None,
             'handoff_seconds': None, 'status': 'pending', 'run_ids': ids}
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        return {**empty, 'status': 'invalid run selection'}
    rows = [by_id[x] for x in ids if x in by_id]
    if not ids or len(rows) != len(ids) or any(r.get('state') != 'completed' for r in rows):
        return empty
    result = {**empty, 'status': 'completed'}
    for field in FIELDS:
        values = [r.get('usage', {}).get(field) for r in rows]
        if all(type(v) is int and v >= 0 for v in values):
            result[field] = sum(values)
    for target, source in [('cost_usd', 'api_estimate_usd'), ('tool_calls', 'tool_calls'), ('worker_seconds', 'elapsed_seconds')]:
        values = [r.get(source) for r in rows]
        if all(type(v) in (int, float) and v >= 0 for v in values):
            result[target] = sum(values)
            if target == 'cost_usd':
                result['cost_by_model'] = {}
                for row, value in zip(rows, values):
                    models = [model for model in row.get('models', []) if isinstance(model, str) and model]
                    model = models[0] if len(models) == 1 else 'unknown'
                    result['cost_by_model'][model] = result['cost_by_model'].get(model, 0) + value
    credit_values = [r.get('standard_credit_estimate') for r in rows]
    known_credits = [value for value in credit_values if type(value) in (int, float) and value >= 0]
    result['credit_priced_run_count'] = len(known_credits)
    result['credit_unpriced_run_count'] = len(rows) - len(known_credits)
    if known_credits:
        result['credit_estimate'] = sum(known_credits)
    efforts = sorted({
        effort
        for row in rows
        for effort in (row.get('efforts') if isinstance(row.get('efforts'), list) else [])
        if isinstance(effort, str) and effort
    })
    result['efforts'] = efforts
    result['effort'] = efforts[0] if len(efforts) == 1 else None
    if all(r.get('tool_counts') is not None for r in rows):
        result['tool_counts'] = {}
        for row in rows:
            for name, count in row['tool_counts'].items():
                result['tool_counts'][name] = result['tool_counts'].get(name, 0) + count
    else:
        result['tool_counts'] = None
    try:
        parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
        start = min(parse(r['start']) for r in rows)
        end = max(parse(r['end']) for r in rows)
        result['elapsed_seconds'] = (end - start).total_seconds()
        # Sum of worker durations is not wall time when workers overlap.
        intervals = sorted((parse(r['start']), parse(r['end'])) for r in rows)
        covered = 0.0
        left, right = intervals[0]
        for a, b in intervals[1:]:
            if a > right:
                covered += (right - left).total_seconds()
                left, right = a, b
            else:
                right = max(right, b)
        covered += (right - left).total_seconds()
        result['handoff_seconds'] = max(0.0, result['elapsed_seconds'] - covered)
    except (ValueError, TypeError, KeyError):
        pass
    return result


def project_suites(root, runs):
    path = Path(root) / '.eutonos-local' / 'benchmark-suites.json'
    if not path.exists():
        return []
    config = json.loads(path.read_text(encoding='utf-8'))
    if config.get('profile') != 'eutonos.benchmark-suites.v1':
        raise ValueError('Invalid benchmark suite metadata')
    by_id = {r['id']: r for r in runs}
    suites = []
    for source in config.get('suites', []):
        suite = {k: source.get(k) for k in ('id', 'label', 'task_id', 'status', 'limits', 'test_type', 'description', 'started_at', 'completed_at')}
        suite['results'] = []
        for item in source.get('results', []):
            row = {k: item.get(k) for k in ('id', 'label', 'primary_model', 'support_model', 'status', 'output_summary', 'answer_text', 'quality', 'packet', 'primary_reads')}
            report = report_metadata(item)
            if report:
                row.update(report)
            row.update({k: item.get(k) for k in ('context_condition', 'ram_condition', 'memory_metrics', 'execution_issue')})
            row['closeout_applicable'] = item.get('closeout_applicable') is not False
            answer_ids, closeout_ids = item.get('answer_run_ids', []), item.get('closeout_run_ids', [])
            row['answer'] = stage(answer_ids, by_id)
            row['closeout'] = stage(closeout_ids, by_id)
            row['total'] = stage(list(dict.fromkeys(answer_ids + closeout_ids)), by_id)
            for role in ('primary', 'support'):
                row[role] = stage([rid for rid in answer_ids if by_id.get(rid, {}).get('role') == role], by_id)
            required_roles = {'primary'} | ({'support'} if item.get('support_model') else set())
            observed_roles = {by_id.get(rid, {}).get('role') for rid in answer_ids}
            retained_answer = bool(row.get('answer_text') or row.get('report_path'))
            if not required_roles <= observed_roles or not retained_answer:
                row['answer'] = {**stage([], by_id), 'run_ids': answer_ids}
            quality = row.get('quality')
            retained_hash = (row.get('report_sha256') if row.get('report_path') and not row.get('answer_text')
                             else hashlib.sha256((row.get('answer_text') or '').encode('utf-8')).hexdigest())
            if quality and quality.get('answer_sha256') != retained_hash:
                row['quality'] = None
                row['status'] = 'assessment does not match retained answer'
            proof = item.get('closeout_verification') or {}
            row['closeout'].update(verified=proof.get('verified'), summary=proof.get('summary'), checks=proof.get('checks', []))
            assessment = item.get('closeout_assessment') or {}
            row['closeout']['quality'] = assessment.get('score')
            if assessment.get('score') is not None:
                row['closeout']['summary'] = (proof.get('summary') or '') + ' Semantic review: ' + assessment.get('summary', '')
            if set(answer_ids) & set(closeout_ids):
                row['answer'] = {**stage([], by_id), 'status': 'overlapping stage identities'}
                row['closeout'] = {**stage([], by_id), 'verified': None, 'status': 'overlapping stage identities'}
                row['total'] = {**stage([], by_id), 'status': 'overlapping stage identities'}
            suite['results'].append(row)
        selected_ids = {rid for item in source.get('results', []) for key in ('answer_run_ids', 'closeout_run_ids') for rid in item.get(key, [])}
        selected_runs = [by_id[rid] for rid in selected_ids if rid in by_id]
        if not suite['started_at']:
            suite['started_at'] = min((r['start'] for r in selected_runs if r.get('start')), default=None)
        # Native turn completion is distinct from completion of the reviewed test.
        if not suite['completed_at'] and suite['results'] and all(i.get('quality') for i in suite['results']) and len(selected_runs) == len(selected_ids) and all(r.get('state') == 'completed' for r in selected_runs):
            suite['completed_at'] = max((r['end'] for r in selected_runs if r.get('end')), default=None)
        suite['test_type'] = suite['test_type'] or ('ram' if any(r.get('context_condition') for r in suite['results']) else 'companion')
        suites.append(analyze_suite(suite))
    return suites
