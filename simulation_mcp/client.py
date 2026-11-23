"""Call the local server over the real MCP stdio protocol without host registration."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER_PATH = Path(__file__).resolve().with_name("server.py")


@asynccontextmanager
async def connect(server_path: Path = SERVER_PATH):
    """Start a stdio MCP session; environment includes EIE575_MCP_SETTINGS."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-X", "utf8", str(server_path)],
        cwd=str(server_path.parent),
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=120)
        ) as session:
            await session.initialize()
            yield session


def result_data(result) -> dict:
    """Obtain a dict result; reject errors instead of treating error text as data."""
    if result.isError:
        message = "\n".join(
            block.text for block in result.content if getattr(block, "type", None) == "text"
        )
        raise RuntimeError(message or "MCP tool returned an error")
    if result.structuredContent is not None:
        return result.structuredContent
    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                value = json.loads(block.text)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    raise RuntimeError("MCP tool returned no structured JSON object")


async def invoke(args) -> int:
    async with connect(args.server.resolve()) as session:
        if args.list:
            result = await session.list_tools()
        else:
            result = await session.call_tool(args.tool, args.arguments)
        print(json.dumps(result.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2))
        return 1 if getattr(result, "isError", False) else 0


def describe_error(error: BaseException) -> dict:
    description = {"type": type(error).__name__, "error": str(error)}
    if isinstance(error, BaseExceptionGroup):
        description["causes"] = [describe_error(item) for item in error.exceptions]
    return description


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", nargs="?", help="Tool name, e.g. get_status")
    parser.add_argument("--args", default="{}", help="JSON object of tool arguments")
    parser.add_argument("--list", action="store_true", help="List tools via MCP")
    parser.add_argument("--server", type=Path, default=SERVER_PATH, help="Server Python entry point")
    args = parser.parse_args()
    if args.list == bool(args.tool):
        parser.error("Specify exactly one of a tool name or --list")
    try:
        args.arguments = json.loads(args.args)
    except json.JSONDecodeError as error:
        parser.error(f"--args is not valid JSON: {error}")
    if not isinstance(args.arguments, dict):
        parser.error("--args must be a JSON object")
    try:
        return asyncio.run(invoke(args))
    except Exception as error:
        print(json.dumps(describe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
