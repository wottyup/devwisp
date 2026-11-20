"""Bridge SSH MCP form approvals to the browser without weakening server policy.

Harness rc2 does not handle MCP elicitation. Keep tools on its native MCP client
and handle ssh-mcp's boolean confirmation locally over the private broker socket.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from contextlib import suppress


async def approve(params: dict) -> dict:
    schema = params.get('requestedSchema', {})
    # Only ssh-mcp's confirmation form is supported; unknown forms fail closed.
    if not isinstance(schema, dict):
        return {'action': 'decline'}
    properties = schema.get('properties', {})
    if (params.get('mode', 'form') != 'form' or schema.get('type') != 'object'
            or not isinstance(properties, dict) or set(properties) != {'confirm'}
            or not isinstance(properties['confirm'], dict) or properties['confirm'].get('type') != 'boolean'):
        return {'action': 'decline'}
    path, thread = os.environ.get('DEVWISP_PERMISSION_SOCKET'), os.environ.get('DEVWISP_PERMISSION_THREAD')
    if not path or not thread:
        return {'action': 'decline'}
    writer = None
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write((json.dumps({'permissionId': uuid.uuid4().hex, 'threadId': thread,
            'toolName': 'devwisp_ssh_mcp', 'reason': str(params.get('message', 'SSH MCP approval'))[:1000],
            'command': str(params.get('message', ''))[:16000], 'cwd': os.getcwd()}, ensure_ascii=False) + '\n').encode())
        await writer.drain()
        result = json.loads(await asyncio.wait_for(reader.readline(), 600))
        return {'action': 'accept', 'content': {'confirm': True}} if result.get('allowed') is True else {'action': 'decline'}
    except (OSError, ValueError, TimeoutError):
        return {'action': 'decline'}
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def run(command: list[str]) -> None:
    current = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
    incoming = asyncio.StreamReader(limit=4 * 1024 * 1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(incoming), sys.stdin.buffer)
    lock = asyncio.Lock()
    approvals: dict[object, asyncio.Task] = {}

    async def send(message: dict) -> None:
        async with lock:
            process.stdin.write((json.dumps(message) + '\n').encode())
            await process.stdin.drain()

    async def reply(message: dict) -> None:
        try:
            result = await approve(message.get('params', {}))
            await send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
        finally:
            approvals.pop(message['id'], None)

    async def upstream() -> None:
        while line := await incoming.readline():
            message = json.loads(line)
            if message.get('method') == 'initialize':
                message.setdefault('params', {}).setdefault('capabilities', {})['elicitation'] = {'form': {}}
            await send(message)

    async def downstream() -> None:
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get('method') == 'elicitation/create' and 'id' in message:
                approvals[message['id']] = asyncio.create_task(reply(message))
            elif message.get('method') == 'notifications/cancelled' and message.get('params', {}).get('requestId') in approvals:
                approvals[message['params']['requestId']].cancel()
            else:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        transport.close()
        cleanup = tasks + list(approvals.values())
        for task in cleanup:
            task.cancel()
        await asyncio.gather(*cleanup, return_exceptions=True)
        if process.returncode is None:
            process.terminate()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 3)
        if process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == '__main__':
    command = sys.argv[1:]
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        raise SystemExit(2)
    try:
        asyncio.run(run(command))
    except asyncio.CancelledError:
        pass
    except (OSError, ValueError, RuntimeError):
        print('SSH MCP approval bridge closed.', file=sys.stderr)
        raise SystemExit(1)
