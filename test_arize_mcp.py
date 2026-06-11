import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession
import json

async def main():
    server_params = StdioServerParameters(
        command="npx",
        args=[
            "-y",
            "@arizeai/phoenix-mcp@latest",
            "--baseUrl",
            "https://my-phoenix.com",
            "--apiKey",
            "fake-key"
        ],
    )
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_res = await session.list_tools()
                
                output = []
                for t in tools_res.tools:
                    output.append({
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.inputSchema
                    })
                    
                with open("arize_tools.json", "w") as f:
                    json.dump(output, f, indent=2)
                print("Successfully dumped arize tools to arize_tools.json")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
