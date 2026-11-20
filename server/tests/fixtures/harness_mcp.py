"""Isolated MCP confirmation fixture; never connects to an SSH host."""
import json
import sys
import os

pending = None
for line in sys.stdin:
    message = json.loads(line)
    if os.environ.get('HARNESS_TEST_TRACE'):
        with open(os.environ['HARNESS_TEST_TRACE'], 'a') as trace: trace.write(json.dumps(message) + '\n')
    method = message.get('method')
    if method == 'initialize':
        assert 'elicitation' in message['params']['capabilities']
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'test-ssh', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'confirm_test', 'description': 'Test browser confirmation', 'inputSchema': {'type': 'object', 'properties': {}}}]}
    elif method == 'tools/call':
        pending = message['id']
        print(json.dumps({'jsonrpc': '2.0', 'id': 'fixture-approval', 'method': 'elicitation/create', 'params': {
            'message': 'Confirm harmless fixture command', 'requestedSchema': {'type': 'object', 'properties': {'confirm': {'type': 'boolean'}}}}}), flush=True)
        continue
    elif message.get('id') == 'fixture-approval':
        result = {'content': [{'type': 'text', 'text': json.dumps(message.get('result', {}))}]}
        message['id'] = pending
    else:
        if 'id' in message:
            print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
