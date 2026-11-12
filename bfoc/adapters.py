"""BF and OC CLI/config contracts; shared by offline probes and real runs."""
import json


def configs(agent, model, url, token):
    name = model['name']
    if agent == 'BF':
        return {'schema_version': 2, 'app': {'logging': {'model_exchange_tracing': {'mode': 'full'}}}, 'ai': {
            'models': [{'id': 'experiment-primary', 'name': 'Experiment shared model', 'provider': 'openai',
                        'model_name': name, 'base_url': url, 'request_url': url + '/chat/completions', 'api_key': token,
                        'context_window': model['context_window'], 'max_tokens': model['max_output_tokens'],
                        'temperature': None, 'top_p': None, 'enabled': True, 'category': 'general_chat',
                        'capabilities': ['text_chat', 'function_calling'], 'metadata': {}, 'inline_think_in_text': False,
                        'custom_request_body': json.dumps({'reasoning_effort': model['reasoning_effort']}), 'auth': {'type': 'api_key'}}],
            'default_models': {'primary': 'experiment-primary', 'fast': 'experiment-primary', 'image_understanding': 'experiment-primary',
                               'search': None, 'image_generation': None, 'speech_recognition': None}}}
    if agent == 'OC':
        return {'update': 'disable', 'mcp': {}, 'plugins': [], 'share': 'disabled', 'agents': {'title': {'disabled': True}},
                'model': {'providerID': 'openai-compatible', 'model': name},
                'permissions': [{'action': '*', 'resource': '*', 'effect': 'allow'}],
                'policies': [{'action': 'provider.use', 'resource': '*', 'effect': 'deny'},
                             {'action': 'provider.use', 'resource': 'openai-compatible', 'effect': 'allow'}],
                'providers': {'openai-compatible': {'package': '@opencode/ai/providers/openai-compatible',
                    'models': {name: {'name': name, 'limit': {'context': model['context_window'], 'output': model['max_output_tokens']},
                                      'settings': {'reasoningEffort': model['reasoning_effort']}}},
                    'settings': {'apiKey': token, 'baseURL': url}}}}
    raise ValueError('Unknown agent: ' + agent)


def command(agent, binary, model, session):
    if agent == 'BF':
        return [str(binary), 'exec', '--agent', 'agentic', '--no-skills', '--session-id', session,
                '--output-format', 'stream-json', '--permission-mode', 'full-access']
    if agent == 'OC':
        return [str(binary), 'run', '--standalone', '--format', 'json', '--thinking',
                '--model', 'openai-compatible/' + model['name'], '--agent', 'build', '--auto']
    raise ValueError('Unknown agent')


def environment(agent, run_root, config, inherited):
    from pathlib import Path
    root = Path(run_root)
    env = dict(inherited)
    env.update({'HOME': '/root', 'LANG': 'C.UTF-8', 'TZ': 'UTC'})
    if agent == 'BF':
        home = root / 'bf-home'
        (home / 'config').mkdir(parents=True, exist_ok=True)
        (home / 'config/app.json').write_text(json.dumps(config))
        env['BITFUN_USER_ROOT'] = str(home)
    elif agent == 'OC':
        for suffix in ['CONFIG', 'DATA', 'CACHE', 'STATE']:
            env['XDG_' + suffix + '_HOME'] = str(root / 'oc-state' / suffix.lower())
        env.update({'OPENCODE_DISABLE_PROJECT_CONFIG': 'true', 'OPENCODE_DISABLE_AUTOUPDATE': 'true',
                    'OPENCODE_DISABLE_MODELS_FETCH': 'true', 'OPENCODE_CONFIG_CONTENT': json.dumps(config)})
    else:
        raise ValueError('Unknown agent')
    return env
