"""Read per-candidate proposals and committed measurements without model fitting."""
from __future__ import annotations

from python_control import controller, runtime
from python_control.objectives import archive, default_policy, rank_key
from .schemas import TaskValidationError, ToolResult
from .validation import read_checkpoint, resolve_run


def inspect_optimization_trace(run_id, *, settings=None):
    current = settings if settings is not None else runtime.load_settings()
    run = resolve_run(current, run_id)
    snapshot = read_checkpoint(run)
    history = snapshot['history']
    problem = runtime.bridge.load_problem(run / 'problem.json')
    policy = snapshot.get('optimization_policy')
    evaluated = archive(history, policy or default_policy())
    evaluations = evaluated['evaluations']
    config_file = run / 'search_config.json'
    strategy = runtime.read_json(config_file).get('strategy', 'unknown') if config_file.is_file() else 'unknown'
    rows = []
    anchor = 0 if history else None

    def proposal_row(proposal, iteration, directory):
        parameters = proposal.get('parameters')
        if not isinstance(parameters, dict) or set(parameters) != set(runtime.bridge.NAMES):
            raise TaskValidationError('OPTIMIZATION_TRACE', 'Saved proposal requires nine named dimensions.')
        checked = runtime.bridge.ordered_values(parameters, problem)
        if proposal.get('x') != checked:
            raise TaskValidationError('OPTIMIZATION_TRACE', 'Saved proposal dimensions disagree with its vector.')
        source = proposal.get('source', 'unknown')
        origin = proposal.get('selection_origin')
        labels = {'llm': 'LLM 逐轮尺寸提案', 'python_sample': '历史程序选点',
                  'initial_design': '历史初始采样'}
        repair = proposal.get('automatic_boundary_repair')
        if repair is not None:
            labels['llm'] = 'LLM 尺寸提案（边界自动修正）'
        calls = proposal.get('calls')
        return {
            'iteration': iteration, 'source': source, 'selection_origin': origin,
            'source_label': labels.get(source, '来源未记录'),
            'parameters': parameters, 'rationale': proposal.get('rationale'),
            'candidate_type': proposal.get('candidate_type'),
            'automatic_boundary_repair': repair,
            'automatic_schema_normalization': proposal.get('automatic_schema_normalization'),
            'proposal_attempt': proposal.get('proposal_attempt'),
            'api_response_count': len(calls) if isinstance(calls, list) else None,
            'anchor_iteration': anchor,
            'parameter_changes': {name: round(parameters[name] - history[anchor]['parameters'][name], 4)
                                  for name in runtime.bridge.NAMES} if anchor is not None else {},
            'candidate_file': str(directory / 'candidate.json'),
            'prompt_file': str(directory / 'prompt.json'),
        }

    for iteration, record in enumerate(snapshot['records'], 1):
        point = history[iteration]
        proposal = record['proposal']
        if (controller.observation(record['detail']) != point
                or proposal.get('parameters') != point['parameters']):
            raise TaskValidationError('OPTIMIZATION_TRACE', 'Proposal and committed measured geometry disagree.')
        directory = run / 'iterations' / ('iter_%03d' % iteration)
        row = proposal_row(proposal, iteration, directory)
        evaluation = evaluations[iteration]
        improved = rank_key(evaluation) < rank_key(evaluations[anchor])
        row.update(status='committed', stress_mpa=point['stress_mpa'],
                   mass_kg=point.get('mass_kg'), frequency_hz=point.get('frequency_hz'),
                   configured_status=evaluation['status'], score=evaluation['score'],
                   constraint_checks=evaluation['constraint_checks'], improved_incumbent=improved)
        rows.append(row)
        if improved:
            anchor = iteration

    pending = snapshot.get('pending')
    if pending and pending.get('proposal') is not None:
        iteration = snapshot['nCompleted'] + 1
        row = proposal_row(pending['proposal'], iteration, run / 'iterations' / ('iter_%03d' % iteration))
        row.update(status='awaiting_commit' if pending.get('detail') is not None else 'pending',
                   phase=pending.get('phase'), stress_mpa=None, mass_kg=None, frequency_hz=None,
                   configured_status='pending', improved_incumbent=None)
        rows.append(row)

    if read_checkpoint(run) != snapshot:
        raise TaskValidationError('RUN_CHANGED', 'Run changed while reading proposals. Refresh the trace.')
    direct_count = sum(row['source'] == 'llm' for row in rows if row['status'] == 'committed')
    data = {
        'run_id': run.name, 'strategy': strategy,
        'mode_label': 'LLM 逐轮提出尺寸' if strategy == 'direct' else '历史搜索运行' if strategy != 'unknown' else '按候选来源查看',
        'completed_candidates': snapshot['nCompleted'], 'target_total_candidates': snapshot['nLLM'],
        'llm_direct_candidates': direct_count,
        'other_candidates': snapshot['nCompleted'] - direct_count,
        'feasible_candidates': sum(row['status'] == 'feasible' for row in evaluations[1:]),
        'configured_constraints': policy is not None,
        'best_iteration': evaluated['best_iteration'], 'policy_id': evaluated['policy_id'],
        'pending_iteration': pending.get('iteration') if pending else None,
        'records': rows,
        'evidence_basis': 'saved_proposals_and_committed_controller_measurements',
    }
    return ToolResult(True, 'optimization_trace', 'read', data)
