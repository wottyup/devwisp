from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from devwisp.app import create_app
from devwisp.codex_app_server import CodexAppServer


class AnswerImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        with patch.dict(os.environ, {'DEVWISP_AUTH_FILE': str(self.root / 'auth.json'),
                                    'DEVWISP_SHARED_STATE_FILE': str(self.root / 'shared.sqlite3')}):
            self.app = create_app(self.root)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.app.state.deepseek.close()
        await self.app.state.claude_code.close()
        self.app.state.shared_state.close()
        self.directory.cleanup()

    def auth_headers(self):
        return {'Authorization': 'Bearer ' + self.app.state.auth.pair(self.app.state.auth.code)}

    async def test_authenticated_upload_saves_image_and_returns_server_generated_path(self):
        content = b'\x89PNG\r\n\x1a\n' + b'example'
        body = {'images': [{'name': '../../screenshot.png', 'media_type': 'image/png',
                            'data': base64.b64encode(content).decode()}]}
        denied = await self.client.post('/api/v1/codex/answer-images', json=body)
        self.assertEqual(denied.status_code, 401)
        response = await self.client.post('/api/v1/codex/answer-images', json=body, headers=self.auth_headers())
        self.assertEqual(response.status_code, 200, response.text)
        image = response.json()['images'][0]
        path = Path(image['path'])
        self.assertTrue(path.is_relative_to(self.root))
        self.assertNotEqual(path.name, 'screenshot.png')
        self.assertEqual(path.read_bytes(), content)

    async def test_invalid_base64_format_and_too_many_images_are_rejected(self):
        headers = self.auth_headers()
        image = {'name': 'image', 'media_type': 'image/png', 'data': '!!!!'}
        for body in ({'images': [image]}, {'images': [image] * 9}, {'images': []},
                     {'images': [{**image, 'media_type': 'image/svg+xml'}]}):
            response = await self.client.post('/api/v1/codex/answer-images', json=body, headers=headers)
            self.assertEqual(response.status_code, 422, response.text)

    async def test_codex_tool_question_accepts_image_reference_in_custom_answer(self):
        server = CodexAppServer('codex', self.root)
        server._server_requests[1] = {'method': 'item/tool/requestUserInput', 'params': {'questions': [
            {'id': 'q', 'question': 'Which?', 'isOther': True, 'options': [{'label': 'A'}]}]}}
        server._write = AsyncMock()
        answer = '已附加图片，请读取以下本机图片文件：\n' + str(self.root / 'screen.png')
        await server.answer_server_request(1, {'answers': {'q': {'answers': [answer]}}})
        self.assertEqual(server._write.call_args.args[0]['result']['answers']['q']['answers'], [answer])

    async def test_upload_obeys_size_and_storage_quota_without_partial_files(self):
        headers = self.auth_headers()
        image = {'name': 'screen.png', 'media_type': 'image/png',
                 'data': base64.b64encode(b'example').decode()}
        image_directory = self.app.state.message_image_directory
        before = set(image_directory.iterdir())
        for constant, value, expected in [('MAX_IMAGE_BYTES', 4, 422),
                                          ('MAX_TOTAL_IMAGE_BYTES', 4, 422),
                                          ('MAX_STORED_IMAGE_BYTES', 4, 507)]:
            with patch('devwisp.app.' + constant, value):
                response = await self.client.post('/api/v1/codex/answer-images', json={'images': [image]}, headers=headers)
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(set(image_directory.iterdir()), before)
