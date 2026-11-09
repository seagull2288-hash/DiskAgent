"""Optional v202 Mechanical metric collection injected into each fresh journal."""
from __future__ import annotations

import json
import inspect
import math
from pathlib import Path

from .objectives import validate_policy
from .runtime import ControllerError


def engineering_density(path, material):
    """Read one constant SI density from the material assigned to the body."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    materials = [item for item in root.findall('.//MatML_Doc/Material')
                 if item.findtext('BulkDetails/Name') == material]
    if len(materials) != 1:
        raise ValueError('Expected one assigned material in Engineering Data.')
    definitions = dict((item.get('id'), item) for item in root.findall('.//ParameterDetails'))
    values = []
    for parameter in materials[0].findall('BulkDetails/PropertyData/ParameterValue'):
        definition = definitions.get(parameter.get('parameter'))
        if definition is None or definition.findtext('Name') != 'Density':
            continue
        units = dict((unit.findtext('Name'), float(unit.get('power', '1')))
                     for unit in definition.findall('Units/Unit'))
        if units != {'kg': 1.0, 'm': -3.0}:
            raise ValueError('Density must be explicitly stored in kg/m^3.')
        values.append(float(parameter.findtext('Data')))
    if len(values) != 1 or values[0] <= 0 or math.isnan(values[0]) or math.isinf(values[0]):
        raise ValueError('One positive constant material density is required.')
    return values[0]


def revolved_mass(area_m2, centroid_radius_m, density_kg_m3):
    """Pappus volume for a meridional section revolved about global Y."""
    values = [float(area_m2), float(centroid_radius_m), float(density_kg_m3)]
    if any(value <= 0 or math.isnan(value) or math.isinf(value) for value in values):
        raise ValueError('Section area, radius and density must be finite and positive.')
    volume_m3 = 2.0 * math.pi * values[0] * values[1]
    return volume_m3, volume_m3 * values[2]


def solver_mass_evidence(text, expected_mass_kg):
    """Check the solver's full-360-degree mass summary in explicit units."""
    import re
    value = re.search(r'TOTAL\s+MASS\s*=\s*([0-9.EeDd+\-]+)', text, re.I)
    unit = re.search(r'^\s*MASS\s*=\s*([^\r\n]+)', text, re.M | re.I)
    full_circle = re.search(r'AXISYMMETRIC\s+360\.?\s+DEGREE\s+MODEL', text, re.I)
    if value is None or unit is None or full_circle is None:
        raise ValueError('A full-360-degree axisymmetric solver mass summary is required.')
    unit_text = unit.group(1).upper()
    if 'TONNE' in unit_text:
        factor = 1000.0
    elif 'KILOGRAM' in unit_text:
        factor = 1.0
    else:
        raise ValueError('Unsupported solver mass unit in the native summary.')
    mass = float(value.group(1).replace('D', 'E').replace('d', 'e')) * factor
    relative_error = abs(mass - expected_mass_kg) / expected_mass_kg
    if mass <= 0 or math.isnan(mass) or math.isinf(mass) or relative_error > 0.001:
        raise ValueError('Solver mass differs from the exact revolved geometry by more than 0.1 percent.')
    return {'solver_mass_kg': mass, 'solver_mass_unit': unit.group(1).strip(),
            'relative_difference': relative_error, 'relative_tolerance': 0.001,
            'scope': 'axisymmetric_360_degrees', 'status': 'passed'}


COLLECT = '''
    metric_policy = __METRIC_POLICY__
    report["metric_evidence"] = {}
    if metric_policy["collect_mass"]:
        progress("collect_native_axisymmetric_mass")
        metric_bodies = ExtAPI.DataModel.GetObjectsByType(DataModelObjectCategory.Body)
        metric_body = [item for item in metric_bodies if not bool(item.Suppressed)][0]
        body_mass = finite(metric_body.Mass.ConvertUnit("kg").Value, "body mass kg")
        model_mass = finite(Model.Geometry.Mass.ConvertUnit("kg").Value, "model mass kg")
        area = finite(metric_body.SurfaceArea.ConvertUnit("m^2").Value, "section area m^2")
        radius = finite(metric_body.CentroidX.ConvertUnit("m").Value, "section centroid radius m")
        material_file = Path.Combine(Path.GetDirectoryName(PROJECT_PATH),
            Path.GetFileNameWithoutExtension(PROJECT_PATH) + "_files", "dp0", "SYS", "ENGD", "material.engd")
        density = engineering_density(material_file, unicode(metric_body.Material))
        volume, full_mass = revolved_mass(area, radius, density)
        if any(value < 0 for value in (body_mass, model_mass)):
            raise RuntimeError("Native mass properties must be nonnegative")
        for value in (body_mass, model_mass):
            if value > 0 and abs(value - full_mass) > max(1e-9, full_mass * 1e-5):
                raise RuntimeError("Positive native mass differs from the full revolved section mass")
        report["mass_kg"] = full_mass
        report["metric_evidence"]["mass_kg"] = {
            "source": "Mechanical.revolved_section_and_material_density", "unit": "kg", "scope": "full_axisymmetric_disk",
            "raw_body_mass_kg": body_mass, "raw_geometry_mass_kg": model_mass,
            "section_area_m2": area, "section_centroid_radius_m": radius,
            "revolved_volume_m3": volume, "density_kg_m3": density,
            "material_file": unicode(material_file), "material": unicode(metric_body.Material),
            "model_2d_behavior": unicode(Model.Geometry.Model2DBehavior)}
        mass_check = solver_mass_evidence(unicode(File.ReadAllText(Path.Combine(working_dir, "solve.out"))), full_mass)
        report["metric_evidence"]["mass_kg"]["solver_crosscheck"] = mass_check
    if metric_policy["modal"] is not None:
        progress("solve_configured_modal_analysis")
        modal_config = metric_policy["modal"]
        modal = [item for item in Model.Analyses if unicode(item.Name) == modal_config["analysis_name"]][0]
        initial_conditions = list(modal.InitialConditions)
        source_ids = [int(item.PreStressEnvironmentModalIC.ObjectId) for item in initial_conditions
                      if item.PreStressEnvironmentModalIC is not None]
        prestress_verified = source_ids == [int(analysis.ObjectId)]
        if modal_config.get("prestress_required", False) and not prestress_verified:
            raise RuntimeError("Modal prestress must point to the verified static analysis")
        if int(modal.AnalysisSettings.MaximumModesToFind) < 6 or modal_config["mode_index"] > 6:
            raise RuntimeError("The registered modal analysis must provide orders 1 through 6")
        modal_solution = modal.Solution
        mode_results = []
        try:
            for order in range(1, 7):
                mode_result = modal_solution.AddTotalDeformation()
                mode_result.Name = "DiskAgent collected mode %02d" % order
                mode_result.Mode = order
                mode_results.append(mode_result)
            modal_solution.ClearGeneratedData()
            modal_started = DateTime.UtcNow
            modal_solution.Solve(True)
            modal_solution.EvaluateAllResults()
            modal_path = Path.Combine(unicode(modal_solution.WorkingDir), "file.rst")
            if modal_solution.Status != SolutionStatusType.Done or any(item.ObjectState != ObjectState.Solved for item in mode_results):
                raise RuntimeError("Configured modal analysis did not produce solved results")
            if not File.Exists(modal_path) or FileInfo(modal_path).LastWriteTimeUtc <= modal_started:
                raise RuntimeError("A fresh native modal result file is required")
            frequencies = [finite(item.ReportedFrequency.ConvertUnit("Hz").Value, "modal frequency Hz") for item in mode_results]
            if any(value <= 0 for value in frequencies):
                raise RuntimeError("Select a positive elastic modal frequency")
            frequency = frequencies[modal_config["mode_index"] - 1]
            report["modal_frequencies_hz"] = frequencies
            report["frequency_hz"] = frequency
            report["metric_evidence"]["frequency_hz"] = {
                "source": "Mechanical.DeformationResult.ReportedFrequency", "unit": "Hz",
                "analysis_name": unicode(modal.Name), "mode_index": int(modal_config["mode_index"]),
                "family": modal_config["family"], "tracking": "fixed_order_only",
                "prestress_verified": prestress_verified, "prestress_source_ids": source_ids,
                "result_file": file_snapshot(modal_path), "fresh_for_this_solve": True}
            report["metric_evidence"]["modal_frequencies_hz"] = {
                "source": "Mechanical.DeformationResult.ReportedFrequency", "unit": "Hz",
                "analysis_name": unicode(modal.Name), "mode_indices": [int(item.Mode) for item in mode_results],
                "family": modal_config["family"], "tracking": "fixed_order_only",
                "prestress_verified": prestress_verified, "prestress_source_ids": source_ids,
                "result_file": file_snapshot(modal_path), "fresh_for_this_solve": True}
        finally:
            for mode_result in mode_results:
                mode_result.Delete()
'''


def extend(journal, policy):
    policy = validate_policy(policy)
    native = Path(journal).parent / 'SolveAndVerify.py'
    text = native.read_text(encoding='ascii')
    helper_anchor = '\ntry:\n    # Enum members'
    if text.count(helper_anchor) != 1:
        raise ControllerError('Native preflight anchor changed; review metric helpers.')
    helpers = '\n'.join(inspect.getsource(helper) for helper in (
        engineering_density, revolved_mass, solver_mass_evidence))
    text = text.replace(helper_anchor, '\n' + helpers + helper_anchor, 1)
    if policy['modal'] is not None:
        old = ('    if len(Model.Analyses) != 1:\n'
               '        raise RuntimeError("Expected exactly one existing static structural analysis")\n'
               '    analysis = Model.Analyses[0]\n')
        modal_name = json.dumps(policy['modal']['analysis_name'], ensure_ascii=True)
        new = ('    static_analyses = [item for item in Model.Analyses if item.AnalysisType == AnalysisType.Static]\n'
               '    configured_modals = [item for item in Model.Analyses if item.AnalysisType == AnalysisType.Modal\n'
               '                         and unicode(item.Name) == u' + modal_name + ']\n'
               '    if len(static_analyses) != 1 or len(configured_modals) != 1 or len(Model.Analyses) != 2:\n'
               '        raise RuntimeError("Configure exactly one static and the explicitly named modal analysis first")\n'
               '    analysis = static_analyses[0]\n')
        if text.count(old) != 1:
            raise ControllerError('Native preflight changed; review modal integration.')
        text = text.replace(old, new, 1)
    anchor = '    report["success"] = True\n'
    if text.count(anchor) != 1:
        raise ControllerError('Native success anchor changed; review metric collection.')
    # Keep non-ASCII analysis names portable to the installed IronPython runtime.
    encoded = 'json.loads(' + repr(json.dumps({'collect_mass': policy['collect_mass'], 'modal': policy['modal']},
                                            ensure_ascii=True)) + ')'
    text = text.replace(anchor, COLLECT.replace('__METRIC_POLICY__', encoded) + anchor, 1)
    compile(text, str(native), 'exec')
    native.write_text(text, encoding='ascii')


def prepare_mesh_study(journal, size_mm):
    """Remesh an independent study copy before taking the verifier's snapshots."""
    size = float(size_mm)
    if not math.isfinite(size) or size <= 0:
        raise ControllerError('Mesh study size must be positive and finite.')
    native = Path(journal).parent / 'SolveAndVerify.py'
    text = native.read_text(encoding='ascii')
    anchor = '\ntry:\n    # Enum members'
    if text.count(anchor) != 1:
        raise ControllerError('Native preflight changed; review mesh study integration.')
    setup = ('\ntry:\n    Model.Mesh.ElementSize = Quantity("%.12g [mm]")\n'
             '    Model.Mesh.GenerateMesh()\n    # Enum members') % size
    text = text.replace(anchor, setup, 1)
    reuse = 'if fresh_update_available(solution, stress, result_path, working_dir, update_started):'
    if text.count(reuse) != 1:
        raise ControllerError('Native solve selection changed; review the independent study.')
    text = text.replace(reuse, 'if False:  # Study remesh requires its own native solve.', 1)
    compile(text, str(native), 'exec')
    native.write_text(text, encoding='ascii')
