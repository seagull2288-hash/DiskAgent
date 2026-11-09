"""Deterministic result cards from committed engineering measurements."""
from __future__ import annotations

import hashlib
import json

from python_control.objectives import default_policy, evaluate, policy_id


def result_cards(result, job_id=None):
    if not isinstance(result, dict):
        return []
    job = result.get('job')
    if isinstance(job, dict):
        return result_cards(job.get('result'), job.get('job_id', job_id))
    if result.get('ok') is not True:
        return []
    data = result.get('data', {})
    analysis = data.get('analysis') or data
    selections = analysis.get('design_selection') or {}
    cards = []
    evidence = analysis.get('evidence', {})
    records = {record['iteration']: record for record in evidence.get('records', [])}
    for label, key in (('最小应力方案', 'minimum_stress'), ('当前配置推荐方案', 'recommended')):
        design = selections.get(key)
        if design is None:
            continue
        origin = records.get(design['iteration'], {})
        cards.append({'label': label, 'iteration': design['iteration'], 'job_id': job_id,
                      'run_id': analysis.get('run_id'), 'observation': design['observation'],
                      'policy_id': selections['policy_id'], 'evaluation': design['evaluation'],
                      'constraint_count': selections['constraint_count'],
                      'native_verified': evidence.get('native_records_reverified') is True,
                      'native_report_file': origin.get('native_report_file'),
                      'checkpoint_file': evidence.get('checkpoint_file'),
                      'engineering_qualification': 'not_assessed'})
    point = data.get('observation')
    if isinstance(point, dict) and 'stress_mpa' in point:
        policy = data.get('optimization_policy') or default_policy()
        evaluation = evaluate(point, {}, policy)
        checks = evaluation['constraint_checks']
        constraint_status = ('not_configured' if not checks else
                             'missing' if any(check['status'] == 'missing' for check in checks) else
                             'infeasible' if any(check['status'] == 'failed' for check in checks) else 'feasible')
        cards.append({'label': '指定尺寸评价', 'job_id': job_id, 'run_id': data.get('run_id'),
                      'observation': point, 'policy_id': policy_id(policy), 'evaluation': evaluation,
                      'constraint_status': constraint_status, 'constraint_count': len(checks),
                      'native_verified': data.get('native_verified') is True,
                      'native_report_file': data.get('native_report_file'),
                      'project_file': data.get('project_file'),
                      'engineering_qualification': 'not_assessed'})
    for card in cards:
        source = {key: card.get(key) for key in ('label', 'job_id', 'run_id', 'iteration', 'native_report_file', 'policy_id')}
        card['card_id'] = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:24]
    return cards
