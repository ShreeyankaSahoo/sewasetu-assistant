import asyncio
import sys

sys.path.insert(0, "/app")
import app as portal
import oauth

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    token, _ = oauth.mint_access_token(
        subject="citizen:0000000000",
        role="citizen",
        scope="citizen",
        audience="http://localhost:8001",
        client_id="local-smoke-test",
    )

    headers = {"Authorization": f"Bearer {token}"}

    async with streamablehttp_client(
        "http://citizen-mcp:8001/mcp",
        headers=headers,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("MCP handshake: PASS")

            result = await session.list_tools()
            print("Registered tools:", [tool.name for tool in result.tools])

            status = await session.call_tool(
                "get_my_application",
                arguments={},
            )
            print("Application lookup result:", status.content)

            eligibility = await session.call_tool(
                "check_eligibility",
                arguments={
                    "date_of_birth": "01/01/1950",
                    "village": "Test Village",
                    "block": "Sonari",
                },
            )
            print("Eligibility result:", eligibility.content)


asyncio.run(main())
