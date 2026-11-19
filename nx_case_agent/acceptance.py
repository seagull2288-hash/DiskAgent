"""Opt-in real MCP -> NX acceptance; creates two new parts."""
import asyncio,json,os,sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    root=Path(__file__).resolve().parent;records=[]
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[str(root/'mcp_server.py')],env=dict(os.environ))) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            for description in ['请建立一个内外端厚中间薄的二维轮盘截面','请建立带一片叶片的三维轮盘局部模型']:
                preview=await session.call_tool('preview_model',{'description':description})
                if preview.isError:raise RuntimeError(str(preview.content))
                result=await session.call_tool('build_model',{'description':description})
                if result.isError:raise RuntimeError(str(result.content))
                data=json.loads(result.content[0].text)
                if not data['ok'] or not data['reopened'] or not all(t['update_errors']==0 for t in data['parameter_tests']):raise RuntimeError(str(data))
                records.append(data);print(data['model_id'],data['output'],len(data['parameter_tests']),'parameters verified',flush=True)
    (root/'acceptance_mcp.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':asyncio.run(main())
