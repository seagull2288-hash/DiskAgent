"""Generate stress and dimension histories using saved measured records only."""
from __future__ import annotations

import csv
from pathlib import Path

from .runtime import ControllerError, bridge


def plot_history(run_dir):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    root = Path(run_dir).resolve()
    with (root / 'history.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ControllerError('No measured history is available to plot.')
    iterations = [int(row['Iteration']) for row in rows]
    stress = [float(row['STRESS_MPa']) for row in rows]
    best = []
    for value in stress:
        best.append(min(value, best[-1]) if best else value)
    best_index = min(range(len(stress)), key=stress.__getitem__)
    folder = root / 'plots'
    folder.mkdir(exist_ok=True)
    paths = []
    problem_path = root / 'problem.json'
    variables = bridge.load_problem(problem_path)['variables'] if problem_path.is_file() else None

    def save(figure, name):
        for extension in ('png', 'svg'):
            path = folder / (name + '.' + extension)
            figure.savefig(path, dpi=180, bbox_inches='tight')
            paths.append(path)
        plt.close(figure)

    figure, axis = plt.subplots(figsize=(11, 5.5), layout='constrained')
    axis.plot(iterations, stress, '-o', markersize=3, linewidth=1, alpha=.7, label='Measured stress')
    axis.plot(iterations, best, linewidth=2, label='Best so far')
    axis.axhline(stress[0], color='gray', linestyle='--', label='Baseline')
    axis.scatter([iterations[best_index]], [stress[best_index]], s=65, color='crimson', zorder=4)
    axis.set(xlabel='Candidate round (0 = baseline)', ylabel='Maximum von Mises stress (MPa)',
             title='DeepSeek + Workbench | Python controller')
    axis.text(.98, .91, 'Best %.6f MPa | reduction %.3f%% | round %d' %
              (stress[best_index], (stress[0]-stress[best_index])/stress[0]*100, iterations[best_index]),
              transform=axis.transAxes, ha='right', va='top')
    axis.grid(alpha=.25)
    axis.legend(loc='best')
    save(figure, 'optimization_history')

    figure, axes = plt.subplots(3, 3, figsize=(13, 9), sharex=True, layout='constrained')
    for index, (axis, name) in enumerate(zip(axes.flat, bridge.NAMES)):
        values = [float(row[name]) for row in rows]
        axis.plot(iterations, values, '-o', markersize=2.5, linewidth=1)
        axis.scatter([iterations[best_index]], [values[best_index]], color='crimson', zorder=4)
        axis.set(title=name + ' | best = %.4f mm' % values[best_index], ylabel='Dimension (mm)')
        if variables:
            for bound in ('lower', 'upper'):
                axis.axhline(variables[index][bound], color='gray', linestyle='--', linewidth=.7, alpha=.6)
        axis.grid(alpha=.25)
    for axis in axes[-1]:
        axis.set_xlabel('Candidate round (0 = baseline)')
    figure.suptitle('Nine dimensions | red marks = best measured design')
    save(figure, 'dimension_history')
    return paths
