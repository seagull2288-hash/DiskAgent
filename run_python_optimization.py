"""Python-only optimization entry. Defaults to read-only checks; --live starts real work."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

from python_control.controller import run
from python_control.plots import plot_history
from python_control.runtime import (ControllerError, api_key, bridge, load_settings,
                                    safe_text, saved_key)
from python_control.workbench import project_unlocked


def check_environment(settings):
    if sys.version_info < (3, 10):
        raise ControllerError('Python 3.10 or newer is required; use the llm_opt environment.')
    for path in (settings.project_file, settings.workbench_executable, settings.api_config,
                 settings.problem_file, settings.root / 'make_verified_journal.py',
                 settings.root / 'mechanical_template.py.txt'):
        if not path.is_file():
            raise ControllerError('Required file is missing: ' + str(path))
    problem = bridge.load_problem(settings.problem_file)
    if problem['workbench_parameter_ids'] != ['P8', 'P9', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7'] or problem['decimals'] != 4:
        raise ControllerError('Existing verified macro requires the original parameter mapping and four decimal places.')
    api = bridge.load_config(settings.api_config)
    from python_control.search import load_search_config
    search = load_search_config(settings.root)
    if search['strategy'] != 'direct' and importlib.util.find_spec('numpy') is None:
        raise ControllerError('NumPy is required for center sampling and surrogate screening.')
    if not api['api_key_env']:
        raise ControllerError('The API configuration must name its key environment variable.')
    if settings.plot_on_complete and importlib.util.find_spec('matplotlib') is None:
        raise ControllerError('Matplotlib is missing from this Python environment.')
    project_unlocked(settings.project_file)
    configured = bool(saved_key(settings.key_environment_variable))
    report = {'controller': 'python', 'python': sys.executable, 'python_version': sys.version.split()[0],
              'candidate_count': settings.candidate_count, 'baseline_additional': 1,
              'project': str(settings.project_file), 'output_root': str(settings.output_root),
              'progress_log': str(settings.progress_log), 'api_model': api['model'],
              'api_key_source_variable': settings.key_environment_variable, 'api_key_configured': configured,
              'matlab_required': False, 'api_requests_made': 0, 'finite_element_solves_made': 0,
              'search_strategy': search['strategy'], 'initial_lhs': search['initial_lhs'],
              'batch_size': search['batch_size'], 'pool_size': search['pool_size'],
              'surrogate_min_samples': search['surrogate_min_samples'],
              'search_config': str(settings.root / 'python_control/search_config.json')}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only environment check; no API/solver calls (default).')
    mode.add_argument('--live', action='store_true', help='Start real API requests and Workbench solves.')
    mode.add_argument('--plot-run', metavar='RUN_DIR', help='Plot an existing Python history without calling API/solver.')
    parser.add_argument('--config', help='Python controller JSON settings file.')
    parser.add_argument('--rounds', type=int, help='Override the target total candidate count for this invocation.')
    parser.add_argument('--resume', help='Original Python run directory containing checkpoint.json.')
    args = parser.parse_args(argv)
    try:
        if args.resume and not args.live:
            raise ControllerError('--resume requires --live; it resumes real work.')
        if args.plot_run:
            for path in plot_history(args.plot_run):
                print(str(path), flush=True)
            return 0
        settings = load_settings(args.config, args.rounds)
        report = check_environment(settings)
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
        if not args.live:
            print('CHECK ONLY: no API request, Workbench solve, or MATLAB process was started.', flush=True)
            return 0
        api = bridge.load_config(settings.api_config)
        with api_key(settings, api['api_key_env']):
            run_dir = run(settings, args.resume)
        if settings.plot_on_complete:
            try:
                for path in plot_history(run_dir):
                    print('PLOT: ' + str(path), flush=True)
            except Exception as exc:
                print('PLOT WARNING: optimization results are saved; chart generation failed: ' + safe_text(exc), flush=True)
        return 0
    except KeyboardInterrupt:
        print('Stopped. Resume using the printed Python run directory and --live --resume.', file=sys.stderr)
        return 130
    except Exception as exc:
        print('ERROR [%s]: %s' % (type(exc).__name__, safe_text(exc)), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
