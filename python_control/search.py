"""LLM search centers -> reproducible pools -> guarded surrogate selection.

Each batch plan is immutable and durable before Workbench is invoked. Existing
controller commits only real measurements, one at a time, and recovers pending work.
"""
from __future__ import annotations

import json
from pathlib import Path

from .runtime import ControllerError, bridge, read_json, write_json
from .sampling import candidate_pool, history_x, latin_hypercube, signature
from .surrogate import select_pool

SELECTION_SCHEMA = 'wb2d.search.selection.v1'
DEFAULT_CONFIG = {
    'strategy': 'direct', 'seed': 20261029, 'initial_lhs': 0, 'batch_size': 4,
    'pool_size': 32, 'sigma_start': .08, 'sigma_min': .02, 'sigma_decay': .9,
    'global_fraction': .2, 'exploration_slots': 1, 'surrogate_min_samples': 24,
    'surrogate_max_samples': 160, 'surrogate_folds': 4,
    'surrogate_length_scales': [.15, .35, .7], 'surrogate_max_cv_nrmse': .5,
    'surrogate_max_rmse_mpa': 15., 'surrogate_uncertainty_weight': 1.5,
    'surrogate_max_distance': .6}


def load_search_config(root):
    path = Path(root) / 'python_control/search_config.json'
    cfg = read_json(path) if path.exists() else DEFAULT_CONFIG.copy()
    return validate_search_config(cfg)


def validate_search_config(cfg):
    # Preserve legacy dictionaries byte-for-byte in signatures: never inject a
    # v2 default when reading an archived configuration or cached batch plan.
    required = set(DEFAULT_CONFIG)
    if not isinstance(cfg, dict) or set(cfg) not in (required, required | {'selection_policy'}):
        raise ControllerError('Search config requires the documented settings and optional selection_policy only.')
    if cfg.get('selection_policy', 'legacy_v1') not in ('legacy_v1', 'hybrid_local_v2', 'hybrid_local_v3'):
        raise ControllerError('Unsupported selection_policy.')
    if cfg['strategy'] not in ('direct', 'center_only', 'center_surrogate'):
        raise ControllerError('Unknown search strategy.')
    for name in ('seed', 'initial_lhs', 'exploration_slots'):
        if type(cfg[name]) is not int or cfg[name] < 0:
            raise ControllerError(name + ' must be a nonnegative integer.')
    for name in ('batch_size', 'pool_size', 'surrogate_min_samples', 'surrogate_max_samples', 'surrogate_folds'):
        if type(cfg[name]) is not int or cfg[name] < 1:
            raise ControllerError(name + ' must be a positive integer.')
    if not (cfg['pool_size'] >= cfg['batch_size'] > cfg['exploration_slots']
            and cfg['surrogate_max_samples'] >= cfg['surrogate_min_samples'] >= max(12, cfg['surrogate_folds']+4)
            and cfg['surrogate_folds'] >= 2):
        raise ControllerError('Invalid batch size, exploration slots or surrogate sample/fold counts.')
    if cfg.get('selection_policy') == 'hybrid_local_v2' and (cfg['batch_size'] < 2 or cfg['exploration_slots'] < 1):
        raise ControllerError('Hybrid local policy requires batch_size >= 2 and exploration_slots >= 1.')
    if cfg.get('selection_policy') == 'hybrid_local_v3' and cfg['batch_size'] < 2:
        raise ControllerError('Directional local policy requires batch_size >= 2.')
    for name in ('sigma_start', 'sigma_min', 'sigma_decay', 'global_fraction', 'surrogate_max_cv_nrmse',
                 'surrogate_max_rmse_mpa', 'surrogate_uncertainty_weight', 'surrogate_max_distance'):
        bridge.finite_number(cfg[name], name)
    if not (0 < cfg['sigma_min'] <= cfg['sigma_start'] <= 1 and 0 < cfg['sigma_decay'] <= 1
            and 0 <= cfg['global_fraction'] <= 1):
        raise ControllerError('Invalid sampling standard deviations, decay or global fraction.')
    if any(cfg[n] <= 0 for n in ('surrogate_max_cv_nrmse', 'surrogate_max_rmse_mpa',
                                 'surrogate_uncertainty_weight', 'surrogate_max_distance')):
        raise ControllerError('Surrogate limits must be positive.')
    if not isinstance(cfg['surrogate_length_scales'], list) or not cfg['surrogate_length_scales']:
        raise ControllerError('Provide one or more GP length scales.')
    if any(bridge.finite_number(v, 'length_scale') <= 0 for v in cfg['surrogate_length_scales']):
        raise ControllerError('GP length scales must be positive.')
    return cfg


def center_schema():
    value = bridge.proposal_schema()
    value['properties']['center'] = value['properties'].pop('parameters')
    value['required'] = ['center', 'rationale', 'candidate_type']
    return value


def parse_center(text, problem):
    value = bridge.parse_json(text)
    if not isinstance(value, dict) or set(value) != {'center', 'rationale', 'candidate_type'}:
        raise bridge.BridgeError('CENTER_SCHEMA', 'Return center, rationale and candidate_type only.')
    if not isinstance(value['center'], dict) or set(value['center']) != set(bridge.NAMES):
        raise bridge.BridgeError('CENTER_SCHEMA', 'center must contain the nine named numbers.')
    # A center is allowed to equal an existing design; samples must be new.
    x = bridge.ordered_values(value['center'], problem)
    if not isinstance(value['rationale'], str) or not 1 <= len(value['rationale'].strip()) <= 2000:
        raise bridge.BridgeError('CENTER_SCHEMA', 'rationale must contain 1..2000 characters.')
    if value['candidate_type'] not in ('explore', 'exploit'):
        raise bridge.BridgeError('CENTER_SCHEMA', 'candidate_type must be explore or exploit.')
    return dict(center=dict(zip(bridge.NAMES, x)), x=x, rationale=value['rationale'].strip(),
                candidate_type=value['candidate_type'])


def center_prompt(api, problem, history, generation, sigma, correction=None, selection_context=None):
    indices = bridge.select_history(history, problem, api['history_prompt_limit'])
    best = min(history, key=lambda p: p['stress_mpa'])
    optimization = None
    if problem.get('optimization_policy') is not None:
        from .objectives import archive
        evaluated = archive(history, problem['optimization_policy'])
        best = history[evaluated['anchor_iteration']]
        optimization = {'policy': evaluated['policy'], 'policy_id': evaluated['policy_id'],
                        'feasible_count': evaluated['feasible_count'],
                        'best_iteration': evaluated['best_iteration'],
                        'pareto_iterations': evaluated['pareto_iterations'],
                        'recent_evaluations': evaluated['evaluations'][-3:]}
    recent = history[-8:]
    before = min((p['stress_mpa'] for p in history[:-8]), default=history[0]['stress_mpa'])
    context = {'task': 'Propose a nine-parameter SEARCH CENTER, not a final evaluated design.',
               'objective': problem['objective'], 'variables': problem['variables'],
               'input_order': bridge.NAMES, 'engineering_context': problem['engineering_context'],
               'implemented_constraints': problem['constraints_currently_implemented'],
               'current_best_measured': best, 'selected_measured_observations': [history[i] for i in indices],
               'search_state': {'generation': generation, 'measured_designs': len(history),
                                'recent_best_improvement_mpa': before-min(p['stress_mpa'] for p in recent),
                                'python_sampling_sigma_fraction_of_variable_range': sigma},
               'strategy': ['Infer promising directions from measured observations; correlations are not verified causes.',
                            'Balance exploration and local refinement. If progress stalls, consider another region.',
                            'Python generates bounded four-decimal candidates and rejects duplicates.',
                            'A center can equal the best measured design; do not nudge it merely to evade duplication.'],
               'output_schema': center_schema(), 'correction_requested': correction}
    if selection_context is not None:
        context['search_selection'] = dict(selection_context)
        context['task'] = 'Propose a nine-parameter center for the explicit hybrid batch policy.'
        context['search_state'].pop('python_sampling_sigma_fraction_of_variable_range')
        context['search_state']['local_radius_fraction_of_variable_range'] = sigma
        context['strategy'].extend([
            'A legal unmeasured center receives a protected evaluation slot in this batch.',
            'If the center was already measured, its slot becomes a new incumbent-neighborhood design.',
            'Incumbent-neighborhood candidates use a normalized L-infinity box, not a Gaussian standard deviation.',
            'The program preserves local and global quotas even when GP validation fails.'])
    if optimization is not None:
        context['optimization'] = optimization
        context['objective'] = {'objectives': optimization['policy']['objectives'],
                                'constraints': optimization['policy']['constraints']}
        context['task'] = 'Propose a search center for the explicit objectives and hard constraints in optimization.policy.'
        context['strategy'].append('Use measured objective tradeoffs and constraint violations. Missing metrics remain unknown.')
    system = ('You propose search centers for a structural optimization controller. Return exactly one JSON object '
              'with root keys center, rationale, candidate_type and no extras. center contains exactly X1,X2,X3,X4,X5,'
              'Y1,Y2,Y3,Y4 as finite numeric values in the original mm convention within their bounds, never percentages. '
              'candidate_type is explore or exploit. rationale is a brief hypothesis, not verified physics. '
              'Do not invent stress, allowable values, constraints or solver success. Only Workbench measures outcomes. '
              'Do not emit code, markdown, commands or text outside JSON. Follow this schema: '+json.dumps(center_schema()))
    return {'system': system, 'user': json.dumps(context, ensure_ascii=False, allow_nan=False)}


def request_center(api, problem, history, folder, generation, sigma, client_factory=None, selection_context=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    client = (client_factory or bridge.Client)(api, live=True)
    correction = None
    for attempt in range(1, api['max_proposal_attempts']+1):
        attempt_dir = folder / ('proposal_%02d' % attempt)
        prompt = center_prompt(api, problem, history, generation, sigma, correction, selection_context)
        write_json(attempt_dir / 'prompt.json', prompt)
        text, metadata = client.generate(prompt, attempt_dir, center_schema())
        write_json(attempt_dir / 'answer.json', {'text': text, 'metadata': metadata})
        try:
            center = parse_center(text, problem)
        except bridge.BridgeError as exc:
            correction = {'code': exc.code, 'message': str(exc), 'previous_response': text}
            write_json(attempt_dir / 'rejection.json', correction)
            if attempt == api['max_proposal_attempts']:
                raise
            continue
        center['apiAttemptDirectory'] = str(attempt_dir)
        write_json(folder / 'center.json', center)
        return center


class SearchProposer:
    def __init__(self, config, *, client_factory=None):
        self.config, self.client_factory = config, client_factory

    def __call__(self, api, problem, history, output, *, live):
        if live is not True or not api['history_compatibility_confirmed'] or not history:
            raise ControllerError('Search proposals require live mode and a compatible measured baseline.')
        output = Path(output)
        if output.parent.name != 'api' or output.parent.parent.parent.name != 'iterations':
            raise ControllerError('Unexpected controller selection directory.')
        root = output.parents[3]
        cfg = self.config
        completed = len(history)-1
        initial = cfg['initial_lhs']
        warmup = completed < initial
        generation = (completed-initial)//cfg['batch_size'] if not warmup else -1
        offset = (completed-initial)%cfg['batch_size'] if not warmup else completed
        start = initial + generation*cfg['batch_size'] if not warmup else 0
        basis_history = history[:start+1]
        folder = root / 'search' / ('initial_lhs' if warmup else 'generation_%03d' % (generation+1))
        folder.mkdir(parents=True, exist_ok=True)
        basis = {'config_signature': signature(cfg), 'history_signature': signature(basis_history)}
        if problem.get('optimization_policy') is not None:
            from .objectives import policy_id
            basis['optimization_policy_id'] = policy_id(problem['optimization_policy'])
        basis_path, plan_path = folder / 'basis.json', folder / 'plan.json'
        if basis_path.exists() and read_json(basis_path) != basis:
            raise ControllerError('Search batch basis changed; refusing to mix histories or settings.')
        write_json(basis_path, basis)
        if plan_path.exists():
            plan = read_json(plan_path)
            if any(plan.get(key) != value for key, value in basis.items()):
                raise ControllerError('Sampling plan differs from its measured history/configuration basis.')
        elif warmup:
            occupied = history_x(basis_history)
            designs = None
            for retry in range(100):
                lhs_seed = cfg['seed']+retry*909
                trial = latin_hypercube(initial, problem, lhs_seed)
                if all(bridge.normalized_distance2(x, y, problem) > problem['min_normalized_distance']**2
                       for x in trial for y in occupied):
                    designs = trial
                    break
            if designs is None:
                raise ControllerError('Not enough distinct LHS designs.')
            plan = dict(basis, source='initial_design', selected=designs, lhs_seed=lhs_seed,
                        center=None, apiAttemptDirectory=None, surrogate={'enabled': False, 'reason': 'initial_lhs'})
            write_json(plan_path, plan)
        else:
            selection_policy = cfg.get('selection_policy', 'legacy_v1')
            directional = selection_policy == 'hybrid_local_v3'
            hybrid = selection_policy in ('hybrid_local_v2', 'hybrid_local_v3')
            if hybrid:
                if directional:
                    from .directional_search import derive_directional_state as derive_state
                else:
                    from .local_search import derive_local_state as derive_state
                local_state = derive_state(basis_history, cfg, policy=problem.get('optimization_policy'))
                sigma = local_state['radius_fraction']
            else:
                sigma = max(cfg['sigma_min'], cfg['sigma_start']*cfg['sigma_decay']**generation)
            center_path = folder / 'center.json'
            if center_path.exists():
                center = read_json(center_path)
            else:
                # Recover a completed response after an interruption before plan commit.
                center = None
                for answer in sorted((folder / 'center_api').glob('*/proposal_*/answer.json')):
                    try:
                        center = parse_center(read_json(answer)['text'], problem)
                        center['apiAttemptDirectory'] = str(answer.parent)
                        break
                    except (bridge.BridgeError, KeyError):
                        continue
                if center is None:
                    context = ({'policy': selection_policy, 'batch_size': cfg['batch_size'],
                                'global_exploration_slots': cfg['exploration_slots'],
                                'protected_new_center_slots': 1,
                                'local_radius_metric': 'normalized_Linf'} if hybrid else None)
                    if directional:
                        context.update(global_exploration_slots=local_state['global_exploration_slots'],
                                       local_selection='multiscale_coordinate_poll',
                                       global_schedule=local_state.get('global_schedule'),
                                       model_scope='validated_local_neighborhood')
                    options = {'selection_context': context} if hybrid else {}
                    center = request_center(api, problem, basis_history, folder / 'center_api' / output.name,
                                            generation+1, sigma, self.client_factory, **options)
                write_json(center_path, center)
            if hybrid:
                if directional:
                    from .directional_search import build_directional_plan as build_plan
                else:
                    from .local_search import build_local_plan as build_plan
                from .surrogate import select_hybrid_pool
                grouped = build_plan(center['x'], problem, basis_history, cfg, generation)
                pool = grouped['pool']
                indices, validation, predictions = select_hybrid_pool(
                    grouped, basis_history, problem, cfg,
                    enabled=cfg['strategy'] == 'center_surrogate')
                origins = grouped['pool_origins']
                counts = {name: sum(origins[i] == name for i in indices)
                          for name in ('llm_center', 'best_local', 'global_uniform')}
                plan = dict(basis, source='python_sample', center=center,
                            apiAttemptDirectory=center['apiAttemptDirectory'], sigma_fraction=sigma,
                            pool=pool, selected_pool_indices=indices, selected=[pool[i] for i in indices],
                            surrogate=validation, predictions=predictions,
                            selection_policy=selection_policy, pool_origins=origins,
                            local_state=grouped['local_state'],
                            selection_summary={'policy_version': selection_policy,
                                'requested_quotas': grouped['selection_quota'], 'planned_origin_counts': counts,
                                'local_radius_fraction': sigma,
                                'local_radius_metric': 'normalized_Linf',
                                'adaptation_action': grouped['local_state'].get('adaptation_action', 'initial'),
                                'surrogate_used': validation.get('surrogate_used', validation['enabled']),
                                'surrogate_reason': validation.get('selection_reason', validation['reason'])})
                if directional:
                    plan['local_candidate_details'] = grouped['local_candidate_details']
                    plan['fallback_local_indices'] = grouped['fallback_local_indices']
                    plan['selection_summary'].update(
                        local_method='multiscale_coordinate_poll',
                        directional_selected=validation.get('directional_selected', 0),
                        global_schedule=grouped['local_state'].get('global_schedule'),
                        model_scope=validation.get('model_scope', 'local'),
                        model_validation=validation)
            else:
                pool, sigma = candidate_pool(center['x'], problem, basis_history, cfg, generation)
                indices, validation, predictions = select_pool(pool, basis_history, problem, cfg,
                                                              enabled=cfg['strategy'] == 'center_surrogate')
                plan = dict(basis, source='python_sample', center=center, apiAttemptDirectory=center['apiAttemptDirectory'],
                            sigma_fraction=sigma, pool=pool, selected_pool_indices=indices,
                            selected=[pool[i] for i in indices], surrogate=validation, predictions=predictions)
            write_json(plan_path, plan)
        if not warmup and (plan.get('selection_policy', 'legacy_v1') != cfg.get('selection_policy', 'legacy_v1')):
            raise ControllerError('Cached plan belongs to another selection policy; create a new run.')
        x = plan['selected'][offset]
        result = {'schema': SELECTION_SCHEMA, 'source': plan['source'], 'mode': 'live',
                  'parameters': dict(zip(bridge.NAMES, x)), 'x': x,
                  'rationale': 'Python selected a reproducible LHS design.' if warmup else
                      'Python sampled/selected this design around the LLM center. Center hypothesis: '+plan['center']['rationale'],
                  'candidate_type': 'explore' if warmup else plan['center']['candidate_type'],
                  'planFile': str(plan_path), 'planSignature': signature(plan), 'planOffset': offset,
                  'basis_history_length': len(basis_history), 'history_signature': signature(history),
                  'apiAttemptDirectory': plan['apiAttemptDirectory'], 'surrogate': plan['surrogate'],
                  'generation': generation+1, 'proposal_attempt': 1}
        if plan.get('selection_policy') in ('hybrid_local_v2', 'hybrid_local_v3'):
            result.update(_selection_metadata(plan, offset))
            result['candidate_type'] = ('explore' if result['selection_origin'] == 'global_uniform' else 'exploit')
            result['llm_candidate_type'] = plan['center']['candidate_type']
            result['rationale'] = ('Program selected '+result['selection_role']+
                ' using explicit quotas; LLM center hypothesis: '+plan['center']['rationale'])
        result['rationale'] = result['rationale'][:2000]
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / 'selection.json', result)
        write_json(output / 'candidate.json', result)
        status = plan['surrogate']
        print('SEARCH source=%s generation=%d batch_item=%d/%d surrogate=%s reason=%s' % (
            result['source'], generation+1, offset+1, len(plan['selected']), status['enabled'], status['reason']), flush=True)
        return result


def validate_generated(candidate, problem, history):
    if candidate.get('schema') != SELECTION_SCHEMA or candidate.get('history_signature') != signature(history):
        raise ControllerError('Generated selection does not belong to the current measured history.')
    plan = read_json(candidate['planFile'])
    n, offset = candidate['basis_history_length'], candidate['planOffset']
    if (type(n) is not int or not 1 <= n <= len(history) or type(offset) is not int
            or offset < 0 or offset >= len(plan['selected'])
            or plan['source'] != candidate['source'] or signature(plan) != candidate['planSignature']
            or plan['history_signature'] != signature(history[:n])
            or candidate['x'] != plan['selected'][offset]):
        raise ControllerError('Generated candidate differs from its committed sampling plan.')
    if candidate['source'] == 'python_sample':
        center = plan['center']
        checked = parse_center(json.dumps({k: center[k] for k in ('center', 'rationale', 'candidate_type')}), problem)
        if checked['x'] != center['x']:
            raise ControllerError('Stored center mapping changed.')
    if plan.get('selection_policy') in ('hybrid_local_v2', 'hybrid_local_v3'):
        expected = _selection_metadata(plan, offset)
        if any(candidate.get(key) != value for key, value in expected.items()):
            raise ControllerError('Candidate search provenance differs from its signed plan.')
        origin = expected['selection_origin']
        if (candidate.get('candidate_type') != ('explore' if origin == 'global_uniform' else 'exploit')
                or candidate.get('llm_candidate_type') != plan['center']['candidate_type']):
            raise ControllerError('Candidate search roles differ from the committed plan.')
    elif any(key in candidate for key in ('selection_policy', 'selection_origin', 'selection_role', 'local_radius_fraction')):
        raise ControllerError('Legacy plan cannot claim hybrid-local selection provenance.')


def _selection_metadata(plan, offset):
    try:
        i = plan['selected_pool_indices'][offset]
        origin = plan['pool_origins'][i]
        roles = {'llm_center': 'llm_proposal', 'best_local': 'local_refinement',
                 'global_uniform': 'global_exploration'}
        radius = plan['local_state']['radius_fraction']
        if (type(i) is not int or i < 0 or plan['pool'][i] != plan['selected'][offset]
                or type(radius) not in (int, float) or not 0 < radius <= 1):
            raise ValueError('Invalid v2 provenance')
        result = {'selection_policy': plan['selection_policy'], 'selection_origin': origin,
                  'selection_role': roles[origin], 'local_radius_fraction': radius}
        if plan['selection_policy'] == 'hybrid_local_v3':
            result['local_move'] = plan['local_candidate_details'].get(str(i))
        return result
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ControllerError('Malformed hybrid-local selection provenance.') from exc
