"""Serial DeepSeek -> Workbench -> measured feedback loop; no MATLAB dependency."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import time
import uuid

from .runtime import (ControllerError, bridge, fingerprint, local_time, project_lock,
                      read_json, safe_text, write_csv, write_json)
from .workbench import canonical_x, evaluate, verify_native

SCHEMA = 'wb2d.python.loop.v1'


def log(settings, state, message):
    line = '[%s] %s' % (local_time(), safe_text(message))
    print(line, flush=True)
    for path in (Path(state['runDir']) / 'progress.log', settings.progress_log):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')


def summary(state):
    result = {key: state[key] for key in ('status', 'nCompleted', 'nLLM', 'runDir')}
    result.update(schema=SCHEMA, controller='python', updatedLocal=local_time(), pending=state['pending'])
    if state['history']:
        index = min(range(len(state['history'])), key=lambda i: state['history'][i]['stress_mpa'])
        best = state['history'][index]
        initial = state['history'][0]['stress_mpa']
        result.update(bestIteration=index, bestStressMPa=best['stress_mpa'],
                      bestX=[best['parameters'][name] for name in bridge.NAMES],
                      baselineStressMPa=initial, bestReductionPercent=(initial-best['stress_mpa'])/initial*100)
    if state.get('lastError'):
        result['lastError'] = state['lastError']
    return result


def save_state(state):
    root = Path(state['runDir'])
    write_json(root / 'checkpoint.json', state)
    write_json(root / 'status.json', summary(state))


def observation(detail):
    return {'parameters': dict(zip(bridge.NAMES, detail['xActual'])),
            'stress_mpa': detail['stressMPa'], 'displacement_mm': detail['displacementMM'],
            'area_m2': detail['area']}


def export_history(state):
    if not state['history']:
        return
    root = Path(state['runDir'])
    initial = state['history'][0]['stress_mpa']
    best = float('inf')
    rows = []
    for index, point in enumerate(state['history']):
        best = min(best, point['stress_mpa'])
        record = state['records'][index-1] if index else None
        rows.append(dict(Iteration=index, **point['parameters'], STRESS_MPa=point['stress_mpa'],
                         Displacement_mm=point['displacement_mm'], AREA=point['area_m2'],
                         BestStress_MPa=best, BestReduction_pct=(initial-best)/initial*100,
                         Source=('deepseek_candidate' if record['proposal'].get('source') == 'llm'
                                 else record['proposal'].get('source', 'unknown')) if index else 'baseline',
                         API_Run=record['apiRun'] if record else '',
                         Solver_Run=record['detail']['runDir'] if record else state['baseline']['runDir']))
    write_csv(root / 'history.csv', rows, list(rows[0]))
    write_json(root / 'history.json', state['history'])
    index = min(range(len(rows)), key=lambda i: rows[i]['STRESS_MPa'])
    point = state['history'][index]
    write_json(root / 'best_design.json', {
        'iteration': index, 'x': [point['parameters'][name] for name in bridge.NAMES],
        'parameters': point['parameters'], 'stressMPa': point['stress_mpa'],
        'displacementMM': point['displacement_mm'], 'areaM2': point['area_m2'],
        'stressReductionPercent': rows[index]['BestReduction_pct']})
    # Rebuild derived per-round exports after an interruption during file writing.
    for record in state['records']:
        write_json(Path(record['roundDirectory']) / 'result.json', record)
    write_json(root / 'status.json', summary(state))


def log_designs(settings, state):
    if not state['history']:
        return
    history = state['history']
    best_index = min(range(len(history)), key=lambda i: history[i]['stress_mpa'])
    for label, index in (('CURRENT', len(history)-1), ('BEST', best_index)):
        point = history[index]
        log(settings, state, '%s round=%03d stress=%.9f MPa displacement=%.9f mm' %
            (label, index, point['stress_mpa'], point['displacement_mm']))
        for group in (bridge.NAMES[:5], bridge.NAMES[5:]):
            log(settings, state, label + ' ' + '  '.join('%s=%.4f' % (n, point['parameters'][n]) for n in group))


def validate_proposal(candidate, problem, history):
    source = candidate.get('source')
    if source not in ('llm', 'python_sample', 'initial_design') or candidate.get('mode') != 'live':
        raise ControllerError('Only a validated live LLM or committed Python selection may enter a real solve.')
    if source != 'llm':
        from .search import validate_generated
        validate_generated(candidate, problem, history)
    raw = {name: candidate[name] for name in ('parameters', 'rationale')}
    if candidate.get('candidate_type') != 'unspecified':
        raw['candidate_type'] = candidate.get('candidate_type')
    checked = bridge.parse_candidate(json.dumps(raw, allow_nan=False), problem, history)
    x = canonical_x(candidate.get('x'), problem)
    if candidate['x'] != x or checked['x'] != x:
        raise ControllerError('Named candidate parameters disagree with the ordered/quantized vector.')


def copy_api_files(pending):
    output = Path(pending['directory'])
    proposal = pending['proposal']
    source = (Path(pending['api_output']) / ('proposal_%02d' % proposal['proposal_attempt'])
              if proposal.get('source') == 'llm' else
              Path(proposal['apiAttemptDirectory']) if proposal.get('apiAttemptDirectory') else None)
    if source is not None:
        for name in ('prompt.json', 'request.json', 'response.json', 'answer.json'):
            if (source / name).is_file():
                shutil.copyfile(source / name, output / name)
    if (output / 'prompt.json').is_file():
        prompt = read_json(output / 'prompt.json')
        write_json(output / 'prompt_user.json', bridge.parse_json(prompt['user']))
    write_json(output / 'candidate.json', pending['proposal'])
    if proposal.get('source') != 'llm':
        write_json(output / 'selection.json', proposal)


def restore_state(settings, resume):
    root = Path(resume).resolve()
    if not root.is_relative_to(settings.output_root.resolve()):
        raise ControllerError('Resume must select a run inside the Python output root.')
    if not (root / 'checkpoint.json').is_file():
        raise ControllerError('Select a Python run containing checkpoint.json; MATLAB .mat checkpoints are retained separately.')
    state = read_json(root / 'checkpoint.json')
    if state.get('schema') != SCHEMA or Path(state.get('runDir', '')).resolve() != root:
        raise ControllerError('Invalid Python checkpoint or run directory.')
    if state['definition'] != settings.definition() or state['fingerprint'] != fingerprint(settings):
        raise ControllerError('Engineering settings or controller files changed. Review them and start a new run.')
    if settings.candidate_count < state['nCompleted']:
        raise ControllerError('Candidate count cannot be smaller than completed rounds.')
    if (len(state['records']) != state['nCompleted'] or
            len(state['history']) != state['nCompleted'] + int(state['baseline'] is not None)):
        raise ControllerError('Checkpoint history and completed-round counts disagree.')
    problem = bridge.load_problem(root / 'problem.json')
    for index, point in enumerate(state['history']):
        canonical_x([point['parameters'][name] for name in bridge.NAMES], problem)
        if bridge.finite_number(point['stress_mpa'], 'checkpoint stress') < 0:
            raise ControllerError('Checkpoint stress must be nonnegative.')
        detail = state['baseline'] if index == 0 else state['records'][index-1]['detail']
        if observation(detail) != point:
            raise ControllerError('Checkpoint dimensions and measured results disagree.')
        verify_native(detail, state['baseline'] if index else None)
    state['nLLM'] = settings.candidate_count
    return state


def run(settings, resume=None, *, proposal_fn=None, evaluator=None):
    """Real work only: the CLI requires --live. Tests inject isolated fake boundaries."""
    if proposal_fn is None:
        from .search import SearchProposer, load_search_config
        search_config = load_search_config(settings.root)
        proposal_fn = bridge.propose if search_config['strategy'] == 'direct' else SearchProposer(search_config)
    else:
        search_config = getattr(proposal_fn, 'config', None)
    evaluator = evaluator or evaluate
    with project_lock(settings.root):
        if resume:
            state = restore_state(settings, resume)
            root = Path(state['runDir'])
        else:
            settings.output_root.mkdir(parents=True, exist_ok=True)
            root = settings.output_root / ('run_' + datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:12])
            root.mkdir()
            problem = bridge.load_problem(settings.problem_file)
            api = bridge.load_config(settings.api_config)
            api['history_compatibility_confirmed'] = True  # Only this run's fresh baseline/history is used.
            write_json(root / 'problem.json', problem)
            write_json(root / 'api_config.json', api)
            if search_config is not None:
                write_json(root / 'search_config.json', search_config)
            state = {'schema': SCHEMA, 'controller': 'python', 'runDir': str(root),
                     'definition': settings.definition(), 'fingerprint': fingerprint(settings),
                     'startedLocal': local_time(), 'nLLM': settings.candidate_count, 'nCompleted': 0,
                     'status': 'initializing', 'baseline': None, 'pending': None, 'history': [], 'records': []}
            save_state(state)
        problem = bridge.load_problem(root / 'problem.json')
        api = bridge.load_config(root / 'api_config.json')
        write_json(settings.output_root / 'latest_run.json', {'runDir': str(root), 'controller': 'python',
                   'progressLog': str(root / 'progress.log'), 'liveLog': str(settings.progress_log)})
        log(settings, state, 'SESSION controller=python completed=%d target=%d run=%s' % (state['nCompleted'], state['nLLM'], root))
        try:
            state['status'] = 'running'
            state.pop('lastError', None)
            save_state(state)
            if state['baseline'] is None:
                log(settings, state, 'BASELINE: updating and solving the reference design.')
                reference = [v['reference'] for v in problem['variables']]
                detail = evaluator(reference, settings, problem, root / 'evaluations')
                native = verify_native(detail)
                if detail['stressMPa'] <= 0:
                    raise ControllerError('Baseline stress must be positive.')
                state['baseline'] = detail
                state['history'].append(observation(detail))
                verified = dict(native['actual']['baseline_after'])
                verified['mesh'] = native['actual']['mesh_after']
                verified['result_source'] = 'Fresh native Mechanical stress/deformation, independent of cached P10/P11.'
                problem['input_numeric_convention'] = 'Verified in this run: nine original bare numeric inputs match actual geometry dimensions in mm. Never use percentages or append units.'
                problem['engineering_context']['freshly_verified_current_run'] = verified
                write_json(root / 'problem.json', problem)
                save_state(state)
                export_history(state)
                log_designs(settings, state)
                if detail.get('stopRequested'):
                    raise KeyboardInterrupt
            else:
                export_history(state)
                log_designs(settings, state)
            while state['nCompleted'] < state['nLLM']:
                k = state['nCompleted'] + 1
                started = time.monotonic()
                if state['pending'] is None:
                    directory = root / 'iterations' / ('iter_%03d' % k)
                    directory.mkdir(parents=True, exist_ok=False)
                    state['pending'] = {'iteration': k, 'directory': str(directory), 'phase': 'requesting',
                                        'proposal': None, 'api_output': None, 'detail': None}
                    save_state(state)
                pending = state['pending']
                if pending['iteration'] != k or Path(pending['directory']).resolve() != root / 'iterations' / ('iter_%03d' % k):
                    raise ControllerError('Pending round is inconsistent with the checkpoint.')
                if pending['proposal'] is None:
                    cached = Path(pending['api_output']) / 'candidate.json' if pending['api_output'] else None
                    if cached and cached.is_file():
                        candidate = read_json(cached)
                    else:
                        api_output = Path(pending['directory']) / 'api' / ('request_' + uuid.uuid4().hex)
                        pending['api_output'] = str(api_output)
                        save_state(state)  # Durable path before calling the provider.
                        log(settings, state, 'ROUND %03d/%03d preparing next design.' % (k, state['nLLM']))
                        candidate = proposal_fn(api, problem, state['history'], api_output, live=True)
                    validate_proposal(candidate, problem, state['history'])
                    pending.update(proposal=candidate, phase='candidate_ready')
                    save_state(state)
                validate_proposal(pending['proposal'], problem, state['history'])
                copy_api_files(pending)
                candidate = pending['proposal']
                if candidate.get('source') != 'llm':
                    status = candidate['surrogate']
                    log(settings, state, 'SEARCH generation=%d source=%s surrogate=%s reason=%s' % (
                        candidate['generation'], candidate['source'], status['enabled'], status['reason']))
                log(settings, state, 'ROUND %03d/%03d parameters=%s' % (k, state['nLLM'], candidate['x']))
                log(settings, state, 'ROUND %03d rationale=%s' % (k, candidate['rationale']))
                if pending['detail'] is None:
                    pending['phase'] = 'solving'
                    save_state(state)
                    log(settings, state, 'ROUND %03d/%03d Workbench solving.' % (k, state['nLLM']))
                    detail = evaluator(candidate['x'], settings, problem, root / 'evaluations', state['baseline'])
                    verify_native(detail, state['baseline'])
                    pending.update(detail=detail, phase='evaluated')
                    save_state(state)
                detail = pending['detail']
                verify_native(detail, state['baseline'])
                if any(abs(a-b) > 0.51*10**(-problem['decimals']) for a, b in zip(detail['xActual'], candidate['x'])):
                    raise ControllerError('Solver result belongs to a different candidate.')
                previous = min(row['stress_mpa'] for row in state['history'])
                record = {'iteration': k, 'proposal': candidate, 'apiRun': pending['api_output'],
                          'detail': detail, 'roundDirectory': pending['directory'],
                          'elapsedSeconds': time.monotonic()-started, 'improved': detail['stressMPa'] < previous}
                state['records'].append(record)
                state['history'].append(observation(detail))
                state.update(nCompleted=k, pending=None)
                save_state(state)  # Commit expensive result before regenerating derived files.
                export_history(state)
                best = min(row['stress_mpa'] for row in state['history'])
                reduction = (state['history'][0]['stress_mpa']-best)/state['history'][0]['stress_mpa']*100
                log(settings, state, 'ROUND %03d/%03d current=%.9f MPa best=%.9f MPa best_reduction=%.6f%% elapsed=%.1fs' %
                    (k, state['nLLM'], detail['stressMPa'], best, reduction, record['elapsedSeconds']))
                log_designs(settings, state)
                if detail.get('stopRequested'):
                    raise KeyboardInterrupt
            state['status'] = 'complete'
            save_state(state)
            export_history(state)
            log(settings, state, 'COMPLETE candidates=%d history=%s' % (state['nCompleted'], root / 'history.csv'))
        except (Exception, KeyboardInterrupt) as exc:
            state['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
            state['lastError'] = {'type': type(exc).__name__, 'message': safe_text(str(exc)), 'time': local_time()}
            save_state(state)
            log(settings, state, 'STOP after %d candidates: %s' % (state['nCompleted'], state['lastError']))
            raise
    return root
