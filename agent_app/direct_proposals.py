"""Recorded boundary repair and duplicate feedback for direct LLM proposals."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re

from python_control import runtime


POLICY = 'direct_boundary_guard_v1'
DUPLICATE_POLICY = 'direct_duplicate_feedback_v1'
SCHEMA_POLICY = 'direct_schema_alias_v1'
MAX_BOUND_OVERRUN = 0.05


def repair_bounds(text, problem, history):
    """Project only small finite overruns, then reuse every original candidate check."""
    raw = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', raw, flags=re.S | re.I)
    value = runtime.bridge.parse_json(fenced.group(1).strip() if fenced else raw)
    if not isinstance(value, dict) or not isinstance(value.get('parameters'), dict):
        raise runtime.bridge.BridgeError('CANDIDATE_SCHEMA', 'Boundary repair requires a complete numerical proposal.')
    original = value['parameters']
    if set(original) != set(runtime.bridge.NAMES):
        raise runtime.bridge.BridgeError('PARAMETERS', 'Boundary repair requires exactly nine dimensions.')
    parameters = dict(original)
    changes = []
    for variable in problem['variables']:
        name, lower, upper = variable['name'], variable['lower'], variable['upper']
        requested = runtime.bridge.finite_number(original[name], name)
        applied = min(upper, max(lower, requested))
        if applied == requested:
            continue
        overrun = abs(applied - requested) / (upper - lower)
        if overrun > MAX_BOUND_OVERRUN + 16 * math.ulp(MAX_BOUND_OVERRUN):
            raise runtime.bridge.BridgeError('BOUNDS', name + ' exceeds the automatic repair limit of 5% of its variable range.')
        parameters[name] = applied
        changes.append({'name': name, 'requested': requested, 'applied': applied,
                        'lower': lower, 'upper': upper, 'normalized_overrun': overrun})
    if not changes:
        raise runtime.bridge.BridgeError('BOUNDS', 'No out-of-bounds dimension was found for repair.')
    corrected = dict(value, parameters=parameters)
    corrected_text = json.dumps(corrected, ensure_ascii=False, allow_nan=False)
    # Bounds, four decimals, finite values, schema and history duplicates remain
    # authoritative after projection. A duplicate is never sent to Workbench.
    runtime.bridge.parse_candidate(corrected_text, problem, history)
    return corrected_text, {'policy': POLICY, 'max_normalized_overrun': MAX_BOUND_OVERRUN,
                            'original_parameters': original, 'changes': changes,
                            'validation': 'original_parse_candidate_passed'}


def _previous_duplicate_answers(log_dir):
    """Bounded numerical feedback from rejected requests for this same iteration."""
    request = Path(log_dir).parent
    scope = request.parent
    if scope.name != 'api' or not request.name.startswith('request_'):
        return []
    paths = []
    for path in scope.glob('request_*/proposal_*/rejection.json'):
        if path.parent.parent == request or not path.resolve().is_relative_to(scope.resolve()):
            continue
        paths.append(path)
    paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    answers = []
    for path in paths[:4]:
        if path.stat().st_size > 64 * 1024:
            continue
        try:
            rejection = runtime.read_json(path)
        except runtime.bridge.BridgeError:
            continue
        if (isinstance(rejection, dict) and rejection.get('code') == 'DUPLICATE'
                and isinstance(rejection.get('previous_response'), str)):
            answers.append(rejection['previous_response'])
    return answers


def _duplicate_feedback(answers, problem, history):
    rejected, seen = [], set()
    tolerance = problem['min_normalized_distance'] ** 2
    for text in answers:
        try:
            candidate = runtime.bridge.parse_candidate(text, problem, [])
        except runtime.bridge.BridgeError:
            continue
        key = tuple(candidate['x'])
        if key in seen:
            continue
        matches = []
        for iteration, observation in enumerate(history):
            observed = [observation['parameters'][name] for name in runtime.bridge.NAMES]
            if runtime.bridge.normalized_distance2(candidate['x'], observed, problem) <= tolerance:
                matches.append({'iteration': iteration, 'parameters': observation['parameters']})
        if matches:
            rejected.append({'parameters': candidate['parameters'], 'matching_records': matches[:3]})
            seen.add(key)
    return rejected


def normalize_schema(text):
    """Translate exact known response labels while preserving all numerical data."""
    raw = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\n?```', raw, flags=re.S | re.I)
    try:
        value = runtime.bridge.parse_json(fenced.group(1).strip() if fenced else raw)
    except runtime.bridge.BridgeError:
        return text, None
    if not isinstance(value, dict):
        return text, None
    aliases = {'参数': 'parameters', '理由': 'rationale', '候选类型': 'candidate_type'}
    names = [aliases.get(name, name) for name in value]
    # Mixed canonical and translated names are ambiguous, even with equal values.
    if len(set(names)) != len(names):
        return text, None
    normalized = {aliases.get(name, name): item for name, item in value.items()}
    mapped = {name: aliases[name] for name in value if name in aliases}
    labels = {'利用': 'exploit', '探索': 'explore'}
    original_type = normalized.get('candidate_type')
    translated_type = labels.get(original_type) if isinstance(original_type, str) else None
    if not mapped and translated_type is None:
        return text, None
    if translated_type is not None:
        normalized['candidate_type'] = translated_type
    audit = {'policy': SCHEMA_POLICY, 'root_field_mapping': mapped,
             'candidate_type_mapping': {'from': original_type, 'to': translated_type} if translated_type else None,
             'parameters_changed': False}
    try:
        normalized_text = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (ValueError, OverflowError):
        return text, None
    return normalized_text, audit


class _GuardedClient:
    def __init__(self, client, cfg, problem, history):
        self.client, self.cfg, self.problem, self.history = client, cfg, problem, history
        self.repair = None
        self.normalization = None

    @property
    def http_attempts(self):
        return self.client.http_attempts

    def generate(self, prompt, log_dir, schema=None):
        context = json.loads(prompt['user'])
        bounds = {v['name']: {'minimum': v['lower'], 'maximum': v['upper']} for v in self.problem['variables']}
        anchor = context.get('current_best_measured') or {}
        anchor_parameters = anchor.get('parameters', {})
        context['bound_enforcement'] = {
            'policy': POLICY, 'parameter_bounds': bounds,
            'at_lower_bound': [name for name, rule in bounds.items() if anchor_parameters.get(name) == rule['minimum']],
            'at_upper_bound': [name for name, rule in bounds.items() if anchor_parameters.get(name) == rule['maximum']],
            'instruction': 'Keep every value inside its own closed interval. At a lower bound, hold or increase it. At an upper bound, hold or decrease it.'}
        previous = _previous_duplicate_answers(log_dir)
        correction = context.get('correction_requested')
        if (isinstance(correction, dict) and correction.get('code') == 'DUPLICATE'
                and isinstance(correction.get('previous_response'), str)):
            previous.insert(0, correction['previous_response'])
        context['duplicate_enforcement'] = {
            'policy': DUPLICATE_POLICY, 'numerical_decimals': self.problem['decimals'],
            'minimum_normalized_distance': self.problem['min_normalized_distance'],
            'history_rows_checked': len(self.history),
            'rejected_designs': _duplicate_feedback(previous, self.problem, self.history),
            'instruction': 'Return a genuinely new quantized design. Never reuse a measured design as a confirmation or recheck. '
                           'Change the proposed dimensions within their bounds so the design is distinct after rounding.'}
        guarded_prompt = {
            'system': prompt['system'] + '\nCheck each numerical dimension against bound_enforcement.parameter_bounds before replying. '
                      'A stress or mass improvement never permits a dimension outside its interval. '
                      'Always use the English JSON keys parameters, rationale, candidate_type and enum explore/exploit, '
                      'even when the rationale is in Chinese.',
            'user': json.dumps(context, ensure_ascii=False, allow_nan=False)}
        guarded_schema = deepcopy(schema or runtime.bridge.proposal_schema())
        for name, rule in bounds.items():
            guarded_schema['properties']['parameters']['properties'][name].update(rule)
        secret = os.environ.get(self.cfg.get('api_key_env', ''), '')
        log_dir = Path(log_dir)
        runtime.write_json(log_dir / 'prompt.json', runtime.bridge.redact(guarded_prompt, secret))
        text, metadata = self.client.generate(guarded_prompt, log_dir, guarded_schema)
        text, self.normalization = normalize_schema(text)
        if self.normalization is not None:
            runtime.atomic_text(log_dir / 'schema.json', json.dumps(self.normalization, ensure_ascii=False, allow_nan=False))
            metadata = dict(metadata, automatic_schema_normalization=self.normalization)
        # Preserve the configured LLM correction attempts. Projection is the
        # final local step and adds no provider request or candidate evaluation.
        final_attempt = 'proposal_%02d' % self.cfg.get('max_proposal_attempts', 2)
        if log_dir.name != final_attempt:
            return text, metadata
        try:
            runtime.bridge.parse_candidate(text, self.problem, self.history)
        except runtime.bridge.BridgeError as exc:
            if exc.code != 'BOUNDS':
                return text, metadata
            try:
                corrected, audit = repair_bounds(text, self.problem, self.history)
            except runtime.bridge.BridgeError as rejected:
                runtime.atomic_text(log_dir / 'repair_skip.json', json.dumps(
                    {'policy': POLICY, 'code': rejected.code, 'message': str(rejected)}, ensure_ascii=False))
                return text, metadata
            runtime.atomic_text(log_dir / 'repair.json', json.dumps(audit, ensure_ascii=False, allow_nan=False))
            self.repair = audit
            return corrected, dict(metadata, automatic_boundary_repair=audit)
        return text, metadata


def propose(cfg, problem, history, output, *, live=False, client=None):
    guarded = _GuardedClient(client or runtime.bridge.Client(cfg, live=live), cfg, problem, history)
    candidate = runtime.bridge.propose(cfg, problem, history, output, live=live, client=guarded)
    candidate['boundary_guard_policy'] = POLICY
    candidate['duplicate_guard_policy'] = DUPLICATE_POLICY
    if guarded.normalization is not None:
        candidate['automatic_schema_normalization'] = guarded.normalization
    if guarded.repair is not None:
        candidate['parameters_requested'] = guarded.repair['original_parameters']
        candidate['automatic_boundary_repair'] = guarded.repair
    runtime.write_json(Path(output) / 'candidate.json', candidate)
    return candidate


class DirectProposer:
    """Preserve the controller's saved search configuration contract."""
    def __init__(self, config):
        self.config = dict(config)

    def __call__(self, *args, **kwargs):
        return propose(*args, **kwargs)
