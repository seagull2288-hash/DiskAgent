"""Real MCP -> NX precision acceptance. Optional --rounds 2 repeats both original prompts."""
import argparse,asyncio,json,os,sys,shutil,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

async def main(rounds):
    root=Path(__file__).resolve().parent
    fixtures=json.loads((root/'regression_prompts.json').read_text(encoding='utf-8'))
    cases=[fixtures[0],next(c for c in fixtures if c['id']=='bold_3d')]
    records=[];record_file=root/'acceptance_mcp.json'
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(root/'mcp_server.py')],env=dict(os.environ))) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            for iteration in range(1,rounds+1):
                for case in cases:
                    request={'description':case['description']}
                    print('START round',iteration,case['id'],flush=True)
                    started=time.perf_counter()
                    response=await session.call_tool('build_model',request)
                    if response.isError:raise RuntimeError(str(response.content))
                    data=json.loads(response.content[0].text)
                    if not data.get('ok'):raise RuntimeError(json.dumps(data,ensure_ascii=False))
                    assert data['geometry_mode']=='precise' and data['simplified'] is False
                    assert data['reference_comparison']['passed'] and data['exact_original_baseline']
                    assert data['reopened']
                    expected=10 if data['model_id']=='geom2d' else 2
                    assert len(data['parameter_tests'])==expected
                    assert all(t['update_errors']==0 and t['restored'] for t in data['parameter_tests'])
                    if data['model_id']=='new160':
                        assert len(data['supplied_parameters'])==6
                        assert (data['geometry']['faces'],data['geometry']['edges'])==(44,122)
                        assert data['parameterization_scope']=='axial_and_radial_scale_only'
                        assert not data['full_feature_history_recovered']
                    data['acceptance_case']=case['id'];data['round']=iteration;data['seconds']=round(time.perf_counter()-started,2)
                    records.append(data)
                    name='二维轮盘_参数化.prt' if data['model_id']=='geom2d' else '单叶片轮盘_参数化.prt'
                    shutil.copy2(data['output'],root/'examples'/name)
                    filename='acceptance_2d.json' if data['model_id']=='geom2d' else 'acceptance_3d.json'
                    (root/filename).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
                    record_file.write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
                    comparison=data['reference_comparison']
                    maximum=max(comparison['candidate_to_reference']['max_distance_mm'],comparison['reference_to_candidate']['max_distance_mm'])
                    print('PASS',iteration,case['id'],data['seconds'],'seconds','max sampled deviation',maximum,'mm',flush=True)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--rounds',type=int,choices=[1,2],default=1);args=parser.parse_args()
    asyncio.run(main(args.rounds))
