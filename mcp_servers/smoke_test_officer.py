
import asyncio
import json
import sys

sys.path.insert(0, "/workspace/app")

import app as portal
import oauth

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    token, _ = oauth.mint_access_token(
        subject="officer:local-smoke-test",
        role="officer",
        scope="officer",
        audience="https://sewasetu-officer-mcp-f22a.onrender.com",
        client_id="local-officer-smoke-test",
    )

    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(
        "https://sewasetu-officer-mcp-f22a.onrender.com/mcp",
        headers=headers,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("Officer MCP handshake: PASS")

            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            print("Registered officer tools:", names)

            result = await session.call_tool(
                "list_pending_applications",
                arguments={"limit": 3},
            )

            text_result = next(
                (
                    item.text
                    for item in result.content
                    if hasattr(item, "text")
                ),
                "{}",
            )
            data = json.loads(text_result)
            print("Pending-list tool succeeded:", data.get("success"))
            print("Number of records returned:", data.get("count"))


asyncio.run(main())
