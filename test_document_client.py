"""真实 SDK 请求构造 + mock HTTP，绝不向飞书发请求。"""
import unittest
from types import SimpleNamespace as Item
from unittest.mock import Mock, patch
import lark_oapi as lark
from config import Settings, DOCUMENT_TITLE
from document_client import DocumentClient
from feishu_client import create_client

class DocumentClientTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.drive.v1.permission_member.list.return_value.success.return_value = True
        self.client.drive.v1.permission_member.list.return_value.data.items = []
        self.settings = Settings("fake-id", "fake-secret", True, "testfolder")
        self.documents = DocumentClient(self.client, self.settings)
        blocker = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_gate_false_blocks_create_and_grant(self):
        documents = DocumentClient(self.client, Settings("fake-id", "fake-secret"))
        with self.assertRaises(PermissionError):
            documents.create_document()
        with self.assertRaises(PermissionError):
            documents.grant_collaborator("doc-test", "ou_requester")
        self.assertEqual(self.client.mock_calls, [])

    def test_create_fixed_title_tenant_only(self):
        response = self.client.docx.v1.document.create.return_value
        response.success.return_value = True
        response.data.document = Item(document_id="doc-test", revision_id=1)
        self.assertEqual(self.documents.create_document().document_id, "doc-test")
        request = self.client.docx.v1.document.create.call_args.args[0]
        self.assertEqual(request.token_types, {lark.AccessTokenType.TENANT})
        self.assertEqual(request.body.title, DOCUMENT_TITLE)
        self.assertEqual(request.body.folder_token, "testfolder")
        self.assertEqual(request.uri, "/open-apis/docx/v1/documents")

    def test_grant_openid_edit_docx(self):
        response = self.client.drive.v1.permission_member.create.return_value
        response.success.return_value = True
        response.data.member = Item(member_id="ou_requester", member_type="openid", perm="edit")
        self.documents.grant_collaborator("doc-test", "ou_requester")
        request = self.client.drive.v1.permission_member.create.call_args.args[0]
        self.assertEqual(request.token_types, {lark.AccessTokenType.TENANT})
        self.assertEqual(request.type, "docx")
        self.assertFalse(request.need_notification)
        self.assertEqual((request.body.member_id, request.body.member_type, request.body.perm),
                         ("ou_requester", "openid", "edit"))

    def test_grant_response_mismatch_rejected(self):
        response = self.client.drive.v1.permission_member.create.return_value
        response.success.return_value = True
        response.data.member = Item(member_id="another", member_type="openid", perm="view")
        with self.assertRaises(RuntimeError):
            self.documents.grant_collaborator("doc-test", "ou_requester")

    def test_metadata_url_is_official_response(self):
        response = self.client.drive.v1.meta.batch_query.return_value
        response.success.return_value = True
        response.data.metas = [Item(doc_token="doc-test", doc_type="docx",
                                    url="https://tenant.feishu.cn/docx/doc-test")]
        self.assertEqual(self.documents.get_document_url("doc-test"), response.data.metas[0].url)
        request = self.client.drive.v1.meta.batch_query.call_args.args[0]
        self.assertTrue(request.body.with_url)
        self.assertEqual(len(request.body.request_docs), 1)
        self.assertEqual(request.token_types, {lark.AccessTokenType.TENANT})
        response.data.metas[0].url = "https://feishu.cn.evil.example/doc-test"
        with self.assertRaises(RuntimeError):
            self.documents.get_document_url("doc-test")

    def test_actual_sdk_http_is_tenant_and_single_post(self):
        client = create_client(self.settings)
        response = Mock(status_code=200, headers={"Content-Type": "application/json"})
        response.content = b'{"code":0,"data":{"document":{"document_id":"doc-test","revision_id":1}}}'
        with patch("lark_oapi.core.token.manager.TokenManager.get_self_tenant_token", return_value="fake-tenant-token"), patch("requests.request", return_value=response) as request:
            DocumentClient(client, self.settings).create_document()
            request.assert_called_once()
            self.assertEqual(request.call_args.args[0], "POST")
            self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer fake-tenant-token")
            self.assertTrue(request.call_args.args[1].endswith("/docx/v1/documents"))

    def test_sdk_timeout_is_not_retried(self):
        client = create_client(self.settings)
        with patch("lark_oapi.core.token.manager.TokenManager.get_self_tenant_token", return_value="fake-tenant-token"), patch("requests.request", side_effect=TimeoutError()) as request:
            with self.assertRaises(TimeoutError):
                DocumentClient(client, self.settings).create_document()
            request.assert_called_once()

if __name__ == "__main__":
    unittest.main()
