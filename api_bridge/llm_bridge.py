"""LLM proposal bridge. Python >=3.10, standard library only.

No Workbench execution lives here. Network access requires an explicit live=True.
API secrets are read from the environment and never written to request logs.
"""
from __future__ import annotations

import csv
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import os
from pathlib import Path
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


NAMES = ['X1', 'X2', 'X3', 'X4', 'X5', 'Y1', 'Y2', 'Y3', 'Y4']
DEFAULT_CONFIG = {
    'provider': 'openai_chat', 'base_url': '', 'model': '',
    'api_key_env': 'LLM_API_KEY', 'timeout_seconds': 90,
    'max_http_attempts': 2, 'max_proposal_attempts': 2,
    'retry_delay_seconds': 1, 'max_retry_delay_seconds': 10,
    'max_output_tokens': 2048, 'temperature': None, 'json_mode': False,
    'chat_token_field': 'max_tokens', 'anthropic_version': '2023-06-01', 'anthropic_workspace_id': '',
    'history_compatibility_confirmed': False, 'history_prompt_limit': 20,
    'offline_seed': 42,
}


class BridgeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError('INVALID_JSON', 'Duplicate JSON object key.')
        result[key] = value
    return result


def parse_json(text):
    def invalid_constant(_):
        raise BridgeError('INVALID_JSON', 'NaN and Infinity are not valid inputs.')
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=invalid_constant)
    except (ValueError, TypeError) as exc:
        raise BridgeError('INVALID_JSON', 'Cannot parse JSON.') from exc


def read_json(path):
    try:
        return parse_json(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError) as exc:
        raise BridgeError('FILE_READ', 'Cannot read UTF-8 JSON file.') from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def finite_number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise BridgeError('INVALID_NUMBER', label + ' must be a finite real number.')
    return float(value)


def load_config(path):
    incoming = read_json(path)
    if not isinstance(incoming, dict) or set(incoming) - set(DEFAULT_CONFIG):
        raise BridgeError('CONFIG', 'Config must contain only documented fields; put keys in environment variables.')
    cfg = dict(DEFAULT_CONFIG, **incoming)
    if cfg['provider'] not in ('openai_chat', 'openai_responses', 'anthropic_messages'):
        raise BridgeError('CONFIG', 'Unsupported provider protocol.')
    for name, low, high in [('max_http_attempts', 1, 5), ('max_proposal_attempts', 1, 5),
                            ('max_output_tokens', 16, 32768), ('history_prompt_limit', 1, 100)]:
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise BridgeError('CONFIG', 'Invalid integer setting: ' + name)
    for name in ('timeout_seconds', 'retry_delay_seconds', 'max_retry_delay_seconds'):
        value = finite_number(cfg[name], name)
        if value < 0 or (name == 'timeout_seconds' and not 1 <= value <= 600):
            raise BridgeError('CONFIG', 'Invalid duration: ' + name)
    if type(cfg['offline_seed']) is not int:
        raise BridgeError('CONFIG', 'offline_seed must be an integer.')
    for name in ('json_mode', 'history_compatibility_confirmed'):
        if type(cfg[name]) is not bool:
            raise BridgeError('CONFIG', name + ' must be true or false.')
    for name in ('base_url', 'model', 'api_key_env', 'anthropic_version', 'anthropic_workspace_id'):
        if not isinstance(cfg[name], str) or any(c in cfg[name] for c in '\r\n\x00'):
            raise BridgeError('CONFIG', 'Invalid text setting: ' + name)
    if cfg['api_key_env'] and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', cfg['api_key_env']):
        raise BridgeError('CONFIG', 'api_key_env must be an environment variable name.')
    if cfg['chat_token_field'] not in ('max_tokens', 'max_completion_tokens'):
        raise BridgeError('CONFIG', 'Invalid chat_token_field.')
    if cfg['temperature'] is not None and not 0 <= finite_number(cfg['temperature'], 'temperature') <= 2:
        raise BridgeError('CONFIG', 'temperature must be null or between 0 and 2.')
    if cfg['base_url']:
        endpoint(cfg)
    return cfg


def load_problem(path):
    p = read_json(path)
    if not isinstance(p, dict) or p.get('input_order') != NAMES:
        raise BridgeError('PROBLEM', 'The problem must use the canonical nine-variable order.')
    vs = p.get('variables', [])
    if len(vs) != 9 or [v.get('name') for v in vs] != NAMES:
        raise BridgeError('PROBLEM', 'Exactly nine variable definitions are required.')
    for v in vs:
        lo, hi = finite_number(v['lower'], v['name']), finite_number(v['upper'], v['name'])
        if lo >= hi or not lo <= finite_number(v['reference'], v['name']) <= hi:
            raise BridgeError('PROBLEM', 'Invalid bounds or reference.')
    if type(p.get('decimals')) is not int or not 0 <= p['decimals'] <= 8:
        raise BridgeError('PROBLEM', 'Invalid decimal precision.')
    if finite_number(p.get('min_normalized_distance'), 'min_normalized_distance') < 0:
        raise BridgeError('PROBLEM', 'Invalid duplicate tolerance.')
    if p.get('objective') != {'name': 'maximum_von_mises_stress', 'unit': 'MPa', 'direction': 'minimize'}:
        raise BridgeError('PROBLEM', 'Review the objective before changing this bridge to another problem.')
    return p


def ordered_values(parameters, problem, quantize=True):
    if not isinstance(parameters, dict) or set(parameters) != set(NAMES):
        raise BridgeError('PARAMETERS', 'parameters must contain exactly X1..X5,Y1..Y4.')
    result = []
    quantum = Decimal(1).scaleb(-problem['decimals'])
    for v in problem['variables']:
        value = finite_number(parameters[v['name']], v['name'])
        if not v['lower'] <= value <= v['upper']:
            raise BridgeError('BOUNDS', v['name'] + ' is outside its bounds; no automatic clipping is performed.')
        if quantize:
            value = float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))
        if not v['lower'] <= value <= v['upper']:
            raise BridgeError('BOUNDS', v['name'] + ' is outside bounds after quantization.')
        result.append(value)
    return result


def normalized_distance2(x, y, problem):
    return sum(((a-b)/(v['upper']-v['lower']))**2
               for a,b,v in zip(x,y,problem['variables']))


def load_history(path, problem):
    if path is None:
        return []
    path = Path(path)
    if path.suffix.lower() == '.csv':
        try:
            with path.open(encoding='utf-8-sig', newline='') as f:
                table = csv.DictReader(f)
                if not table.fieldnames or not set(NAMES + ['STRESS_MPa']).issubset(table.fieldnames):
                    raise BridgeError('HISTORY', 'Use MATLAB history.csv with named variables and STRESS_MPa, not a raw Workbench P-column CSV.')
                if len(set(table.fieldnames)) != len(table.fieldnames):
                    raise BridgeError('HISTORY', 'History CSV contains duplicate column names.')
                rows = list(table)
            data = []
            for row in rows:
                item = {'parameters': {n: float(row[n]) for n in NAMES},
                        'stress_mpa': float(row['STRESS_MPa'])}
                for source, target in [('Displacement_mm', 'displacement_mm'), ('AREA', 'area_m2')]:
                    item[target] = float(row[source]) if row.get(source, '').strip() else None
                data.append(item)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise BridgeError('HISTORY', 'History CSV contains unreadable or invalid numeric values.') from exc
    else:
        data = read_json(path)
    if not isinstance(data, list):
        raise BridgeError('HISTORY', 'History JSON must be an array of successful observations.')
    result = []
    for row in data:
        if not isinstance(row, dict) or not {'parameters', 'stress_mpa'}.issubset(row):
            raise BridgeError('HISTORY', 'Each history row requires parameters and stress_mpa.')
        x = ordered_values(row['parameters'], problem)
        stress = finite_number(row['stress_mpa'], 'stress_mpa')
        if stress < 0:
            raise BridgeError('HISTORY', 'Use measured positive MPa, not a negative BO score.')
        item = {'parameters': dict(zip(NAMES, x)), 'stress_mpa': stress}
        for name in ('displacement_mm', 'area_m2'):
            value = row.get(name)
            if value is not None:
                value = finite_number(value, name)
                if value < 0 or (name == 'area_m2' and value == 0):
                    raise BridgeError('HISTORY', 'Invalid physical output: ' + name)
            item[name] = value
        result.append(item)
    return result


def select_history(history, problem, limit):
    if len(history) <= limit:
        return list(range(len(history)))
    selected = []
    def add(i):
        if i not in selected and len(selected) < limit:
            selected.append(i)
    for i in sorted(range(len(history)), key=lambda i: (history[i]['stress_mpa'], i))[:min(5, limit)]:
        add(i)
    for i in range(max(0, len(history)-min(8, limit)), len(history)):
        add(i)
    xs = [list(row['parameters'].values()) for row in history]
    while len(selected) < limit:
        others = [i for i in range(len(history)) if i not in selected]
        i = max(others, key=lambda i: (min(normalized_distance2(xs[i], xs[j], problem) for j in selected), -i))
        add(i)
    return selected


def proposal_schema():
    return {'type': 'object', 'properties': {
        'parameters': {'type': 'object', 'properties': {n: {'type': 'number'} for n in NAMES},
                       'required': NAMES, 'additionalProperties': False},
        'rationale': {'type': 'string'},
        'candidate_type': {'type': 'string', 'enum': ['explore', 'exploit']}},
        'required': ['parameters', 'rationale', 'candidate_type'], 'additionalProperties': False}


def make_prompt(problem, history, cfg, correction=None):
    indices = select_history(history, problem, cfg['history_prompt_limit'])
    best = min(history, key=lambda r: r['stress_mpa']) if history else None
    context = {
        'task': 'Propose one new nine-parameter design for the existing evaluator.',
        'objective': problem['objective'], 'input_order': NAMES,
        'input_numeric_convention': problem['input_numeric_convention'],
        'variables': problem['variables'], 'engineering_context': problem['engineering_context'],
        'constraints_currently_implemented': problem['constraints_currently_implemented'],
        'numerical_decimals': problem['decimals'], 'observations_available': len(history),
        'current_best_measured': best, 'selected_history_indices_1based': [i+1 for i in indices],
        'selection_rule': 'best five, recent eight, then greedy normalized-space coverage, capped at configured limit',
        'observations': [history[i] for i in indices], 'output_schema': proposal_schema(),
        'response_contract': {
            'required_root_fields': ['parameters', 'rationale', 'candidate_type'],
            'required_parameter_fields': NAMES,
            'candidate_type_allowed_values': ['explore', 'exploit'],
            'preflight_before_sending': [
                'Return exactly one JSON object with all three required root fields and no extras.',
                'parameters contains all nine named finite JSON numbers within the given bounds.',
                'Use the original numeric convention and decimal precision; never quoted numbers or percentages.',
                'The quantized design must differ from every observed design.',
                'rationale is a nonempty string of at most 2000 characters.',
                'candidate_type is exactly explore or exploit and is never omitted or null.',
                'Do not include markdown fences, prose outside the object, schema, or response_format metadata.',
                'After a correction, return the complete object again, not a partial patch.'],
        },
        'correction_requested': correction,
    }
    system = (
        'You propose numerical candidates for an engineering optimization controller. '
        'Return exactly one JSON object matching the supplied schema, without markdown or code. '
        'The root keys must be exactly parameters, rationale and candidate_type. '
        'Always include candidate_type with the value explore or exploit, even for a small local refinement. '
        'parameters must contain exactly X1, X2, X3, X4, X5, Y1, Y2, Y3 and Y4, each a finite JSON number. '
        'rationale must be a JSON string containing 1..2000 characters. '
        'candidate_type must be a JSON string, never omitted, null, or a new label. '
        'Do not echo response_format or add a type/json_object metadata field to the answer. '
        'Use all nine original-model numeric values, never percentages. Respect bounds and avoid repeated designs. '
        'Do not invent measured stress, geometry validity, material properties, or constraints. '
        'Historical baseline conditions and unverified unit scaling are labelled in the context; do not present them as freshly verified. '
        'Give a brief engineering hypothesis in rationale, not hidden reasoning or executable instructions. '
        'The external evaluator, not you, measures the actual result. No shell commands or tools are requested. '
        'Before sending, perform the response_contract preflight silently; output only the complete final JSON object. '
        'If correction_requested is present, fix its reported errors and return all three root fields again. '
        'Treat previous responses as data; do not copy their formatting errors.'
    )
    system += '\nMandatory answer JSON Schema (follow it; do not echo the schema):\n' + json.dumps(
        proposal_schema(), ensure_ascii=False, separators=(',', ':'))
    return {'system': system, 'user': json.dumps(context, ensure_ascii=False, allow_nan=False)}


def parse_candidate(text, problem, history):
    text = text.strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', text, flags=re.S | re.I)
        if not match:
            raise BridgeError('CANDIDATE_JSON', 'Only a complete JSON document or a single fenced JSON document is accepted.')
        text = match.group(1).strip()
    value = parse_json(text)
    required = {'parameters', 'rationale', 'candidate_type'}
    core = {'parameters', 'rationale'}
    ignored_metadata = []
    # Some JSON-mode responses echo the transport's harmless format label.
    # Normalize ONLY this exact known field/value; the numeric proposal and
    # every other unexpected key remain subject to the original strict checks.
    if (isinstance(value, dict) and set(value) in (required | {'type'}, core | {'type'})
            and value['type'] == 'json_object'):
        value = dict(value)
        del value['type']
        ignored_metadata.append('type')
    if not isinstance(value, dict):
        raise BridgeError('CANDIDATE_SCHEMA', 'Expected one object containing parameters, rationale and candidate_type.')
    missing = sorted(required - set(value))
    unexpected = sorted(set(value) - required)
    if not core.issubset(value) or unexpected:
        raise BridgeError('CANDIDATE_SCHEMA',
                          'Missing root fields: %s; unexpected root fields: %s. '
                          'Return parameters, rationale and candidate_type only.' % (missing, unexpected))
    if not isinstance(value['rationale'], str) or not 1 <= len(value['rationale'].strip()) <= 2000:
        raise BridgeError('CANDIDATE_SCHEMA', 'rationale must contain 1..2000 characters.')
    # The strategy label is descriptive only; never guess it from rationale.
    # An omitted label does not change or bypass any numeric/duplicate checks.
    candidate_type = value.get('candidate_type', 'unspecified')
    if 'candidate_type' in value and candidate_type not in ('explore', 'exploit'):
        raise BridgeError('CANDIDATE_SCHEMA', 'candidate_type must be explore or exploit.')
    x = ordered_values(value['parameters'], problem)
    if any(normalized_distance2(x, [r['parameters'][n] for n in NAMES], problem)
           <= problem['min_normalized_distance']**2 for r in history):
        raise BridgeError('DUPLICATE', 'Quantized candidate repeats or is too close to a recorded design.')
    return {'parameters_requested': value['parameters'], 'parameters': dict(zip(NAMES, x)),
            'x': x, 'rationale': value['rationale'].strip(), 'candidate_type': candidate_type,
            'rounding_applied': any(value['parameters'][n] != x[i] for i,n in enumerate(NAMES)),
            'input_numeric_convention': problem['input_numeric_convention'],
            'response_normalization': {'ignored_metadata_fields': ignored_metadata,
                                       'missing_metadata_fields': missing},
            'validation': {'bounds': True, 'finite_numbers': True, 'duplicate_check': True,
                           'cad_and_fem_validity': 'not_evaluated', 'manufacturing_validity': 'not_evaluated'}}


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, '[REDACTED]') if secret else value
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if k.lower() in ('authorization', 'x-api-key', 'api_key') else redact(v, secret)) for k,v in value.items()}
    return value


def endpoint(cfg):
    base = cfg['base_url'].rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeError('CONFIG', 'base_url must not contain credentials, query parameters, or a fragment.')
    local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if not parsed.hostname or (parsed.scheme != 'https' and not (local and parsed.scheme == 'http')):
        raise BridgeError('CONFIG', 'Use HTTPS, or HTTP on localhost for a local model.')
    suffix = {'openai_chat': '/chat/completions', 'openai_responses': '/responses',
              'anthropic_messages': '/messages'}[cfg['provider']]
    if base.endswith(suffix):
        raise BridgeError('CONFIG', 'base_url should be the API root, without the method suffix.')
    return base + suffix, local


def request_body(cfg, prompt, schema=None):
    common = {'model': cfg['model'], 'stream': False}
    if cfg['temperature'] is not None:
        common['temperature'] = cfg['temperature']
    if cfg['provider'] == 'openai_chat':
        common.update(messages=[{'role': 'system', 'content': prompt['system']},
                                {'role': 'user', 'content': prompt['user']}])
        common[cfg['chat_token_field']] = cfg['max_output_tokens']
        if urllib.parse.urlsplit(cfg['base_url']).hostname == 'api.deepseek.com':
            common['thinking'] = {'type': 'disabled'}
        if cfg['json_mode']:
            common['response_format'] = {'type': 'json_object'}
    elif cfg['provider'] == 'openai_responses':
        common.update(instructions=prompt['system'], input=prompt['user'],
                      max_output_tokens=cfg['max_output_tokens'], store=False)
        if cfg['json_mode'] and schema is not None:
            common['text'] = {'format': {'type': 'json_schema', 'name': 'design_candidate', 'strict': True, 'schema': schema}}
    else:
        common.update(system=prompt['system'], messages=[{'role': 'user', 'content': prompt['user']}],
                      max_tokens=cfg['max_output_tokens'])
    return common


def _text_blocks(blocks):
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ''
    return ''.join(b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') in ('text', 'output_text'))


def response_text(provider, body):
    if not isinstance(body, dict) or body.get('error'):
        raise BridgeError('PROVIDER_RESPONSE', 'Provider returned an error object.')
    if provider == 'openai_chat':
        choices = body.get('choices', [])
        if not choices or not isinstance(choices[0], dict):
            raise BridgeError('PROVIDER_RESPONSE', 'Missing chat completion choice.')
        choice = choices[0]
        if choice.get('finish_reason') not in ('stop', None):
            raise BridgeError('INCOMPLETE_RESPONSE', 'Chat completion did not finish normally.')
        message = choice.get('message', {})
        if message.get('refusal') or message.get('tool_calls'):
            raise BridgeError('PROVIDER_RESPONSE', 'Refusal or a tool call cannot be used as a design.')
        text = _text_blocks(message.get('content'))
    elif provider == 'openai_responses':
        if body.get('status') != 'completed':
            raise BridgeError('INCOMPLETE_RESPONSE', 'Responses API status is not completed.')
        messages = [item for item in body.get('output', []) if item.get('type') == 'message']
        if any(block.get('type') == 'refusal' for m in messages for block in m.get('content', [])):
            raise BridgeError('PROVIDER_RESPONSE', 'Provider refused the request.')
        text = ''.join(_text_blocks(item.get('content')) for item in messages)
    else:
        if body.get('stop_reason') not in ('end_turn', 'stop_sequence'):
            raise BridgeError('INCOMPLETE_RESPONSE', 'Messages API did not finish with a complete text answer.')
        text = _text_blocks(body.get('content'))
    if not text.strip():
        raise BridgeError('EMPTY_RESPONSE', 'Provider returned no final text.')
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, cfg, *, live=False, transport=None, sleep=time.sleep):
        self.cfg, self.live, self.sleep = cfg, live, sleep
        self.transport = transport or urllib.request.build_opener(_NoRedirect()).open
        self.http_attempts = 0

    def generate(self, prompt, log_dir, schema=None):
        if not self.live:
            raise BridgeError('OFFLINE', 'Network generation requires explicit live mode.')
        cfg = self.cfg
        if not cfg['model'].strip():
            raise BridgeError('CONFIG', 'Set the exact model name before a live request.')
        url, local = endpoint(cfg)
        secret = os.environ.get(cfg['api_key_env'], '').strip() if cfg['api_key_env'] else ''
        if any(c in secret for c in '\r\n\x00'):
            raise BridgeError('CONFIG', 'API key contains invalid header characters.')
        if not secret and not (local and not cfg['api_key_env']):
            raise BridgeError('MISSING_KEY', 'Set the API key in the configured environment variable.')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if secret:
            headers['Authorization'] = 'Bearer ' + secret
        if cfg['provider'] == 'anthropic_messages':
            headers['anthropic-version'] = cfg['anthropic_version']
            if cfg['anthropic_workspace_id']:
                headers['anthropic-workspace-id'] = cfg['anthropic_workspace_id']
        body = request_body(cfg, prompt, schema)
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        write_json(log_dir / 'request.json', redact({'url': url, 'body': body}, secret))
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        events = []
        for attempt in range(1, cfg['max_http_attempts']+1):
            self.http_attempts += 1
            request = urllib.request.Request(url, data=encoded, headers=headers, method='POST')
            start = time.monotonic()
            events.append({'attempt': attempt, 'status': 'started', 'started_utc': dt.datetime.now(dt.timezone.utc).isoformat()})
            write_json(log_dir / 'transport.json', events)
            try:
                with self.transport(request, timeout=cfg['timeout_seconds']) as response:
                    raw = response.read(4*1024*1024+1)
                    response_headers = response.headers
                if len(raw) > 4*1024*1024:
                    raise BridgeError('RESPONSE_SIZE', 'Provider response exceeds 4 MiB.')
                data = parse_json(raw.decode('utf-8'))
                write_json(log_dir / 'response.json', redact(data, secret))
                text = response_text(cfg['provider'], data)
                request_id = response_headers.get('x-request-id') or response_headers.get('request-id') or data.get('id')
                events.append({'attempt': attempt, 'status': 'success', 'elapsed_seconds': time.monotonic()-start})
                write_json(log_dir / 'transport.json', events)
                return text, redact({'response_id': request_id, 'returned_model': data.get('model'), 'usage': data.get('usage'),
                                     'http_attempts_in_call': attempt}, secret)
            except urllib.error.HTTPError as exc:
                # Headers and response bodies may echo a key: never log them verbatim.
                events.append({'attempt': attempt, 'status': 'http_error', 'http_status': exc.code})
                write_json(log_dir / 'transport.json', events)
                if exc.code not in (429, 500, 502, 503, 504, 529) or attempt == cfg['max_http_attempts']:
                    raise BridgeError('HTTP_ERROR', 'HTTP ' + str(exc.code) + '; inspect endpoint, model, key permissions and quota.') from None
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                delay = float(retry_after) if retry_after.isdigit() else cfg['retry_delay_seconds'] * 2**(attempt-1)
                if delay > cfg['max_retry_delay_seconds']:
                    raise BridgeError('RETRY_LATER', 'Provider requests a longer wait; no early retry was sent.') from None
                self.sleep(delay)
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                events.append({'attempt': attempt, 'status': 'network_error_unknown_delivery'})
                write_json(log_dir / 'transport.json', events)
                raise BridgeError('NETWORK_ERROR', 'Connection failed or timed out; the server may have processed the request, so it was not automatically retried.') from None
            except (UnicodeError, ValueError, TypeError, KeyError, AttributeError) as exc:
                raise BridgeError('PROVIDER_RESPONSE', 'Provider response has an invalid or unsupported structure.') from None
        raise BridgeError('HTTP_ERROR', 'Request attempts exhausted.')


def new_run(output, mode, command, cfg):
    output = Path(output)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BridgeError('OUTPUT_EXISTS', 'Choose a new output directory; old outputs will not be reused.') from exc
    manifest = {'run_id': uuid.uuid4().hex, 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                'command': command, 'mode': mode, 'provider': cfg['provider'], 'requested_model': cfg['model'],
                'status': 'running', 'http_attempts': 0, 'finite_element_calls': 0}
    secret = os.environ.get(cfg['api_key_env'], '').strip() if cfg['api_key_env'] else ''
    manifest = redact(manifest, secret)
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'config_snapshot.json', redact(cfg, secret))
    return manifest


def mock_candidate(problem, history, seed):
    rng = random.Random(seed)
    for _ in range(1000):
        values = {v['name']: rng.uniform(v['lower'], v['upper']) for v in problem['variables']}
        text = json.dumps({'parameters': values, 'rationale': 'Offline fixture only; no LLM or physical solver was called.', 'candidate_type': 'explore'})
        try:
            parse_candidate(text, problem, history)
            return text
        except BridgeError as exc:
            if exc.code != 'DUPLICATE':
                raise
    raise BridgeError('MOCK_EXHAUSTED', 'Could not produce a distinct offline fixture.')


def propose(cfg, problem, history, output, *, live=False, client=None):
    if live and history and not cfg['history_compatibility_confirmed']:
        raise BridgeError('HISTORY_COMPATIBILITY', 'Confirm that imported history uses the same geometry, units, material, loads and result definition before a live history-based request.')
    manifest = new_run(output, 'live' if live else 'offline', 'propose', cfg)
    output = Path(output)
    client = client or Client(cfg, live=live)
    secret = os.environ.get(cfg['api_key_env'], '').strip() if cfg['api_key_env'] else ''
    correction = None
    calls = []
    try:
        for attempt in range(1, cfg['max_proposal_attempts']+1):
            attempt_dir = output / ('proposal_%02d' % attempt)
            prompt = make_prompt(problem, history, cfg, correction)
            write_json(attempt_dir / 'prompt.json', redact(prompt, secret))
            if live:
                text, meta = client.generate(prompt, attempt_dir, proposal_schema())
            else:
                text = mock_candidate(problem, history, cfg['offline_seed']+attempt-1)
                meta = {'response_id': None, 'returned_model': 'offline_fixture', 'usage': None, 'http_attempts_in_call': 0}
            # Final answer text only. This does not execute any generated source code.
            write_json(attempt_dir / 'answer.json', {'text': redact(text, secret), 'metadata': redact(meta, secret)})
            calls.append(meta)
            try:
                candidate = parse_candidate(text, problem, history)
            except BridgeError as exc:
                correction = redact({'code': exc.code, 'message': str(exc), 'previous_response': text}, secret)
                write_json(attempt_dir / 'rejection.json', redact(correction, secret))
                if attempt == cfg['max_proposal_attempts']:
                    raise
                continue
            candidate.update(candidate_id=manifest['run_id'], source='llm' if live else 'offline_fixture',
                             mode=manifest['mode'], proposal_attempt=attempt, calls=calls)
            candidate = redact(candidate, secret)
            write_json(output / 'candidate.json', candidate)
            manifest.update(status='candidate_ready', http_attempts=client.http_attempts,
                            proposal_attempts=attempt, history_rows=len(history), calls=calls)
            write_json(output / 'manifest.json', manifest)
            return candidate
    except BridgeError as exc:
        manifest.update(status='failed', http_attempts=client.http_attempts,
                        error={'code': exc.code, 'message': str(exc)}, calls=calls)
        write_json(output / 'manifest.json', manifest)
        raise


def smoke(cfg, output, *, live=False, client=None):
    manifest = new_run(output, 'live' if live else 'offline', 'smoke', cfg)
    output = Path(output)
    client = client or Client(cfg, live=live)
    prompt = {'system': 'Return one JSON object only.', 'user': 'Reply exactly with this JSON: {"ok":true}'}
    write_json(output / 'prompt.json', prompt)
    try:
        text, meta = client.generate(prompt, output / 'request_01') if live else ('{"ok":true}', {'returned_model': 'offline_fixture'})
        reply = parse_json(text.strip())
        if not isinstance(reply, dict) or reply.get('ok') is not True:
            raise BridgeError('SMOKE_FORMAT', 'Provider did not return the requested JSON acknowledgement.')
        result = {'ok': True, 'mode': manifest['mode'], 'metadata': meta, 'http_attempts': client.http_attempts,
                  'engineering_data_sent': False, 'finite_element_calls': 0}
        write_json(output / 'smoke_result.json', result)
        manifest.update(status='complete', http_attempts=client.http_attempts)
        write_json(output / 'manifest.json', manifest)
        return result
    except BridgeError as exc:
        manifest.update(status='failed', http_attempts=client.http_attempts,
                        error={'code': exc.code, 'message': str(exc)})
        write_json(output / 'manifest.json', manifest)
        raise
