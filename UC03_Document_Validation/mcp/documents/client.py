from __future__ import annotations

from dataclasses import dataclass

from mcp.base import BaseMCPClient, MCPResponse


@dataclass
class StoredDocument:
    document_ref: str
    case_id: str
    employee_id: str
    document_type: str
    file_name: str
    stored_at: str
    size_bytes: int
    content_type: str


@dataclass
class StorageConfirmation:
    document_ref: str
    case_id: str
    stored: bool
    error: str | None = None


class DocumentStorageClient(BaseMCPClient):
    """
    Client for the Document Storage MCP server.

    Tools exposed:
    - store_document        Upload and store a document in the correct case folder
    - get_document_metadata Returns metadata for a stored document (never raw bytes in logs)
    - confirm_stored        Checks whether a document reference is stored in a case
    - delete_document       Applies retention/deletion policy (requires HR approval)

    Production setup required:
        DOCUMENT_STORAGE_URL   — Azure Blob Storage or SharePoint URL
        DOCUMENT_STORAGE_KEY   — SAS token or managed identity (from Key Vault)

    Security notes:
    - Raw document bytes are NEVER logged or included in audit events.
    - document_ref (blob URL / file ID) is used for all references.
    - Injection scanning is applied to extracted text before business logic.
    - Cross-case access is blocked at the CaseStore level before this client is called.
    """

    _SERVICE_NAME = "documents"

    async def store_document(
        self,
        case_id: str,
        employee_id: str,
        document_type: str,
        file_name: str,
        file_bytes: bytes,
        content_type: str = "application/pdf",
    ) -> StorageConfirmation:
        resp = await self._call(
            "store_document",
            {
                "case_id": case_id,
                "employee_id": employee_id,
                "document_type": document_type,
                "file_name": file_name,
                "content_type": content_type,
                "size_bytes": len(file_bytes),
                # file_bytes intentionally NOT passed to _call to keep out of logs
            },
        )
        return StorageConfirmation(
            document_ref=resp.data.get("document_ref", ""),
            case_id=case_id,
            stored=resp.success,
            error=resp.error,
        )

    async def get_document_metadata(self, document_ref: str) -> StoredDocument | None:
        resp = await self._call("get_document_metadata", {"document_ref": document_ref})
        if not resp.success:
            return None
        d = resp.data
        return StoredDocument(
            document_ref=document_ref,
            case_id=d["case_id"],
            employee_id=d["employee_id"],
            document_type=d["document_type"],
            file_name=d["file_name"],
            stored_at=d["stored_at"],
            size_bytes=d["size_bytes"],
            content_type=d["content_type"],
        )

    async def confirm_stored(self, case_id: str, document_ref: str) -> bool:
        resp = await self._call(
            "confirm_stored", {"case_id": case_id, "document_ref": document_ref}
        )
        return resp.data.get("stored", False)

    async def delete_document(self, document_ref: str, approval_id: str) -> bool:
        """Delete under retention policy. approval_id must be an approved ApprovalRecord."""
        resp = await self._call(
            "delete_document", {"document_ref": document_ref, "approval_id": approval_id}
        )
        return resp.success

    # ─── Mock implementations ──────────────────────────────────────────────────

    async def _mock_call(self, tool: str, params: dict) -> MCPResponse:
        if tool == "store_document":
            doc_ref = (
                f"blob://dact-docs/{params['case_id']}"
                f"/{params['document_type']}/{params['file_name']}"
            )
            return MCPResponse(
                success=True,
                data={"document_ref": doc_ref, "stored": True},
                source="mock",
            )

        if tool == "get_document_metadata":
            return MCPResponse(
                success=True,
                data={
                    "case_id": "case-mock",
                    "employee_id": "emp-mock",
                    "document_type": "passport",
                    "file_name": "passport.pdf",
                    "stored_at": "2024-10-16T10:00:00Z",
                    "size_bytes": 204800,
                    "content_type": "application/pdf",
                },
                source="mock",
            )

        if tool == "confirm_stored":
            return MCPResponse(
                success=True,
                data={"stored": True, "document_ref": params.get("document_ref")},
                source="mock",
            )

        if tool == "delete_document":
            return MCPResponse(
                success=True,
                data={"deleted": True, "document_ref": params.get("document_ref")},
                source="mock",
            )

        return MCPResponse(success=False, error=f"Unknown tool: {tool}", source="mock")
