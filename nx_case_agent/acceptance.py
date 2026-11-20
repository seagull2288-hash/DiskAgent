"""Opt-in real MCP -> NX acceptance; creates two new parts."""
import asyncio,json,os,sys,shutil
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    root=Path(__file__).resolve().parent;records=[]
    cases=json.loads((root/'regression_prompts.json').read_text(encoding='utf-8'))
    cases.extend([
        {'id':'short_2d','description':'请建立一个内外端厚中间薄的二维轮盘截面'},
        {'id':'short_3d','description':'请建立带一片叶片的三维轮盘局部模型'},
        {'id':'optimization_3d','description':'单叶片三维轮盘，薄盘厚度8毫米，叶片高度38毫米，叶根弦长18毫米，叶尖弦长12毫米，扭转角12度，叶根圆角半径0.4毫米'}])
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(root/'mcp_server.py')],env=dict(os.environ))) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            for case in cases:
                description=case['description']
                preview=await session.call_tool('preview_model',{'description':description})
                if preview.isError:raise RuntimeError(str(preview.content))
                result=await session.call_tool('build_model',{'description':description})
                if result.isError:raise RuntimeError(str(result.content))
                data=json.loads(result.content[0].text)
                if not data['ok'] or not data['reopened'] or not all(t['update_errors']==0 for t in data['parameter_tests']):raise RuntimeError(str(data))
                assert data['geometry_checks']['transitions']['joins_checked']==8
                assert data['geometry_checks']['transitions']['passed']
                assert all(v=='passed' for k,v in data['reopened_geometry_checks'].items() if k!='faces_checked')
                if data['model_id']=='new160':
                    assert any(f['type']=='BLEND' for f in data['modeling_steps'])
                    assert abs(data['derived_parameters']['blade_height']['value']-(data['parameters']['tip_radius']-data['parameters']['rim_radius']))<1e-8
                    assert len(data['parameter_tests'])==14
                data['acceptance_case']=case['id'];records.append(data)
                if case['id'].startswith('original_'):
                    name='二维轮盘_参数化.prt' if data['model_id']=='geom2d' else '单叶片轮盘_参数化.prt'
                    shutil.copy2(data['output'],root/'examples'/name)
                    (root/('acceptance_2d.json' if data['model_id']=='geom2d' else 'acceptance_3d.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
                print(case['id'],data['output'],len(data['parameter_tests']),'parameters verified',flush=True)
    (root/'acceptance_mcp.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
if __name__=='__main__':asyncio.run(main())
