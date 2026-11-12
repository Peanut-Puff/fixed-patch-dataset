"""Usage accounting from shared HTTP ledger; missing stays unknown, never zero."""
import csv
from pathlib import Path
from .common import read_json, write_json


def summarize(output):
    output = Path(output).resolve()
    plan = read_json(output / 'plan.json')
    ledger = output / 'model-ledger/requests.jsonl'
    import json
    requests = []
    if ledger.exists():
        with ledger.open() as stream:
            requests = [json.loads(line) for line in stream]
    starts = output / 'model-ledger/attempts-started.jsonl'
    if starts.exists():
        completed_ids = {r['id'] for r in requests}
        with starts.open() as stream:
            requests.extend(r for r in (json.loads(line) for line in stream) if r['id'] not in completed_ids)
    run_summary = output / 'summary.json'
    completed = read_json(run_summary).get('runs', []) if run_summary.exists() else []
    rows = []
    for item in plan['tasks']:
        for agent in plan['agents']:
            task = item['task']['id']
            directory = output / 'agents' / (task + '-' + agent)
            run = read_json(directory / 'summary.json') if (directory / 'summary.json').exists() else next(
                (r['execution'] for r in completed if r['task'] == task and r['agent'] == agent), {})
            final = output / 'verification' / task / ('final-' + agent) / 'summary.json'
            reward = (read_json(final).get('reward') or {}).get('reward') if final.exists() else None
            accepted = [r for r in requests if r['run'] == task + '-' + agent and r.get('forwarded')]
            rejected = [r for r in requests if r['run'] == task + '-' + agent and not r.get('forwarded')]
            usage = [r['usage_observations'][-1] for r in accepted if r.get('usage_observations')]
            def observed(field):
                values = [u.get(field) for u in usage]
                valid = [v for v in values if type(v) is int and v >= 0]
                return sum(valid) if valid else None
            cached = [(u.get('prompt_tokens_details') or {}).get('cached_tokens', u.get('prompt_cache_hit_tokens')) for u in usage]
            reasoning = [(u.get('completion_tokens_details') or {}).get('reasoning_tokens') for u in usage]
            def subtotal(values):
                valid = [v for v in values if type(v) is int and v >= 0]
                return sum(valid) if valid else None
            tokens_input = observed('prompt_tokens')
            tokens_output = observed('completion_tokens')
            cached_total = subtotal(cached)
            complete = bool(accepted) and len(usage) == len(accepted) and all(
                type(u.get(k)) is int and u[k] >= 0 for u in usage for k in ['prompt_tokens', 'completion_tokens'])
            fresh = None
            if len(cached) == len(usage) and cached and all(type(v) is int and 0 <= v <= u.get('prompt_tokens', -1) for v, u in zip(cached, usage)):
                fresh = tokens_input - cached_total
            process = run.get('process') or {}
            rows.append({'task': task, 'agent': agent, 'binary_sha256': run.get('binary_sha256'),
                'reward': reward, 'cli_exit_code': run.get('cli_exit_code'),
                'duration_seconds': process.get('duration_seconds'),
                'forwarded_attempts': len(accepted), 'locally_rejected_attempts': len(rejected),
                'usage_covered': len(usage), 'usage_missing': len(accepted) - len(usage),
                'token_status': 'complete' if complete else ('partial' if usage else 'missing'),
                'input_observed': tokens_input, 'output_observed': tokens_output,
                'cached_observed': cached_total, 'uncached_observed': fresh, 'reasoning_observed': subtotal(reasoning)})
    write_json(output / 'metrics.json', {'rows': rows, 'token_semantics': 'HTTP prompt includes cache; completion includes reasoning. Observed subtotals only; incomplete runs must be excluded from full-token comparisons.'})
    with (output / 'metrics.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(rows)
    return rows
