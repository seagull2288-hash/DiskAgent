"""Create a v202 Workbench journal and its native Mechanical verifier."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent

def pystr(value):
    return 'u' + json.dumps(str(value).replace('\\', '/'), ensure_ascii=True)

def create(x, journal, result, complete):
    folder = Path(journal).parent
    if any(Path(p).exists() for p in (journal, result, complete)):
        raise RuntimeError('Fresh evaluation files are required')
    x = [round(float(v), 4) for v in x]
    project = ROOT / 'project_v2' / '2dmodel.wbpj'
    cad_files = list((ROOT/'cad').glob('*.prt'))
    if len(cad_files) != 1:
        raise RuntimeError('Expected one independent CAD copy')
    text = (ROOT / 'mechanical_template.py.txt').read_text(encoding='utf-8')
    for token, val in {'__RUN_DIR__':pystr(folder), '__PROJECT_ROOT__':pystr(project.parent),
                       '__PROJECT_FILE__':pystr(project), '__EXPECTED_X__':repr(x)}.items():
        text = text.replace(token, val)
    # Apply the design point before native verification. The native script then
    # clears cached solution data and solves explicitly, because a copied project
    # can claim Solved while having no corresponding file.rst.
    anchor = '    report["actual"]["stress_maximum"] = {'
    text = text.replace(anchor, '''    deformation_results = [obj for obj in ExtAPI.DataModel.GetObjectsByType(DataModelObjectCategory.TotalDeformation)
                           if int(obj.Parent.ObjectId) == int(solution.ObjectId) and not bool(obj.Suppressed)]
    if len(deformation_results) != 1:
        raise RuntimeError("Expected one existing total-deformation result in the solved analysis")
    deformation = deformation_results[0]
    if deformation.ObjectState != ObjectState.Solved:
        raise RuntimeError("Total deformation result is not Solved")
    report["displacement_mm"] = finite(deformation.Maximum.ConvertUnit("mm").Value, "maximum displacement mm")
    report["actual"]["deformation_maximum"] = {"mm": report["displacement_mm"], "name": unicode(deformation.Name)}
''' + anchor)
    native = folder / 'SolveAndVerify.py'
    native.write_text(text, encoding='ascii')
    names = ['P8','P9','P1','P2','P3','P4','P5','P6','P7']
    labels = ['X1','X2','X3','X4','X5','Y1','Y2','Y3','Y4']
    lines = ['# encoding: ascii', 'SetScriptVersion(Version="20.2.221")',
             'import json', 'from System.IO import File', 'from System import DateTime',
             'system1 = GetSystem(Name="SYS")',
             'geometry1 = system1.GetContainer(ComponentName="Geometry")',
             'props = geometry1.GetGeometryProperties()',
             'props.GeometryImportSavePartFile = False',
             'geometry_path = unicode(props.GeometryFilePath).replace("\\\\", "/")',
             'with open('+pystr(folder/'geometry_link.json')+', "w") as link_file:',
             '    link_file.write(json.dumps({"geometry_file": geometry_path, "save_source_part": bool(props.GeometryImportSavePartFile)}, ensure_ascii=True))',
             'if not geometry_path.lower().startswith('+pystr(project.parent).lower()+'.lower()+"/"):',
             '    raise RuntimeError("The DesignModeler database is outside the isolated project")',
             'designPoint1 = Parameters.GetDesignPoint(Name="0")']
    for name, label, value in zip(names, labels, x):
        lines += ['parameter1 = Parameters.GetParameter(Name="'+name+'")',
                  'if "DS_'+label+'" not in unicode(parameter1.DisplayText): raise RuntimeError("Input mapping changed: '+name+'")',
                  'designPoint1.SetParameterExpression(Parameter=parameter1, Expression="%.4f")' % value]
    lines += ['File.WriteAllText('+pystr(folder/'solve_started_utc.txt')+', DateTime.UtcNow.ToString("o"))',
              'UpdateAllDesignPoints(DesignPoints=[designPoint1])',
              'model1 = system1.GetContainer(ComponentName="Model")',
              'model1.Edit(Interactive=False)',
              'model1.SendCommand(Language="Python", Command=File.ReadAllText('+pystr(native)+'))',
              'native_report = json.loads(unicode(File.ReadAllText('+pystr(folder/'mechanical_verified.json')+')))',
              'if native_report.get("success") is not True: raise RuntimeError("Native verification failed: " + unicode(native_report.get("error")))',
              'model1.Exit()',
              'Parameters.ExportAllDesignPointsData(FilePath='+pystr(result)+')',
              'with open('+pystr(complete)+', "w") as marker:',
              '    marker.write("WB2D_COMPLETE_V1\\n")', '']
    Path(journal).write_text('\n'.join(lines), encoding='ascii')

if __name__ == '__main__':
    create(json.loads(sys.argv[1]), *sys.argv[2:5])
