"""Python-controller-only reuse of fresh Workbench update results.

The shared MATLAB generator and Mechanical template retain their original behavior.
Mechanical still checks actual dimensions/physics and reads native results; only
the unconditional second clear/solve becomes conditional.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from textwrap import indent

from .runtime import ControllerError

POLICY = 'reuse_fresh_workbench_update_v1'

# This code runs in ANSYS 2020 R2 / IronPython 2, not the controller's Python.
REUSE_HELPER = '''
def fresh_update_available(solution, stress, result_path, working_dir, update_started):
    if report["mesh_generation_requested"]:
        return False
    if solution.Status != SolutionStatusType.Done:
        return False
    if any(obj.ObjectState != ObjectState.Solved for obj in [solution, stress]):
        return False
    if any(item["severity"] == u"Error" for item in report["actual"]["messages_before"]):
        return False
    solver_path = Path.Combine(working_dir, "solve.out")
    for path in [result_path, solver_path]:
        if not File.Exists(path):
            return False
        info = FileInfo(path)
        if info.Length <= 0 or info.LastWriteTimeUtc <= update_started:
            return False
        if info.LastWriteTimeUtc > DateTime.UtcNow:
            return False
    solver_text = unicode(File.ReadAllText(solver_path))
    if re.search(r"\\*{3}\\s*ERROR\\s*\\*{3}", solver_text, re.IGNORECASE):
        return False
    return True


'''

REUSE_BRANCH = '''    from System.Globalization import CultureInfo, DateTimeStyles
    update_started = DateTime.Parse(
        File.ReadAllText(Path.Combine(BASE, "solve_started_utc.txt")),
        CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind).ToUniversalTime()
    report["evaluator_policy"] = "reuse_fresh_workbench_update_v1"
    report["actual"]["update_started_utc"] = unicode(update_started.ToString("o"))
    if fresh_update_available(solution, stress, result_path, working_dir, update_started):
        solve_started = update_started
        report["actual"]["result_source"] = "workbench_update"
        report["actual"]["extra_solve_skipped"] = True
        report["actual"]["cached_generated_data_cleared"] = False
        progress("reuse_fresh_workbench_update")
    else:
        report["actual"]["result_source"] = "mechanical_fallback"
        report["actual"]["extra_solve_skipped"] = False
'''


def create(root, x, journal, result, complete):
    root = Path(root)
    spec = importlib.util.spec_from_file_location('verified_journal_generator', root / 'make_verified_journal.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.create(x, journal, result, complete)
    native = Path(journal).parent / 'SolveAndVerify.py'
    text = native.read_text(encoding='ascii')
    start_anchor = '    progress("clear_generated_solution_data")\n'
    end_anchor = '    progress("evaluate_existing_results")\n'
    try_anchor = '\ntry:\n    # Enum members'
    fresh_anchor = '    report["actual"]["result_file_after"]["fresh_for_this_solve"] = True\n'
    for anchor in (start_anchor, end_anchor, try_anchor, fresh_anchor):
        if text.count(anchor) != 1:
            raise ControllerError('Shared Mechanical template changed; review the Python reuse adapter before running.')
    start, end = text.index(start_anchor), text.index(end_anchor)
    original_solve = text[start:end]
    if (start >= end or original_solve.count('solution.Solve(True)') != 1
            or original_solve.count('solution.ClearGeneratedData()') != 1):
        raise ControllerError('Unexpected native solve block; no solver will be launched.')
    text = text[:start] + REUSE_BRANCH + indent(original_solve, '    ') + text[end:]
    text = text.replace(try_anchor, '\n' + REUSE_HELPER + try_anchor, 1)
    text = text.replace(fresh_anchor, fresh_anchor + '''    if report["actual"]["result_source"] == "workbench_update":
        report["actual"]["result_file_after"]["fresh_for_this_update"] = True
''', 1)
    compile(text, str(native), 'exec')
    native.write_text(text, encoding='ascii')
