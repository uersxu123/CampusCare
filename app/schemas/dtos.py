from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.enums import MAX_INPUT_CHARS


class ChatRequest(BaseModel):
    requestId: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
    )
    message: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    sessionId: Optional[str] = None

    @field_validator("message")
    @classmethod
    def require_visible_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message 不能为空白")
        return value


class ChatStreamEvent(BaseModel):
    requestId: Optional[str] = None
    turnId: Optional[str] = None
    sessionId: Optional[str] = None
    status: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    finishReason: Optional[str] = None
    completionVerified: Optional[bool] = None
    partial: Optional[bool] = None
    retryable: Optional[bool] = None
    continuationCount: Optional[int] = None
    outputTokens: Optional[int] = None
    type: str


class KnowledgeIngestRequest(BaseModel):
    source: str
    content: str


class KnowledgeIngestResponse(BaseModel):
    source: str
    chunks: int


class KnowledgeDocumentMetadataRequest(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    domain: str = Field(min_length=1, max_length=64)
    tags: list[str] = Field(default_factory=list)
    site: str = Field(default="ALL", min_length=1, max_length=64)
    version: str = Field(default="1", min_length=1, max_length=64)
    verifiedAt: Optional[datetime] = None
    expiresAt: Optional[datetime] = None


class KnowledgeReprocessRequest(BaseModel):
    parserProfile: str = Field(default="auto", min_length=1, max_length=64)
    chunkingProfile: str = Field(default="structure_token_v2", min_length=1, max_length=64)


class KnowledgePublishRequest(BaseModel):
    verifiedAt: Optional[datetime] = None


class ReportResponse(BaseModel):
    id: int
    sessionId: str
    username: str
    displayName: str
    content: str
    intent: str
    emotion: str
    emotionScore: float
    riskLevel: str
    confidence: float
    summary: str
    createdAt: datetime


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: datetime


class ConversationListItemResponse(BaseModel):
    sessionId: str
    title: str
    preview: str
    messageCount: int
    createdAt: datetime
    updatedAt: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationListItemResponse]


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    archived: bool = False
    archivedAt: Optional[datetime] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
    messages: list[ConversationMessageResponse]


class ConversationArchiveResponse(BaseModel):
    sessionId: str
    archived: bool
    archivedAt: datetime


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: datetime
    channel: Optional[str] = None
    recipient: Optional[str] = None
    filePath: Optional[str] = None


class RiskCaseResponse(BaseModel):
    id: int
    reportId: int
    riskLevel: str
    status: str
    owner: str
    summary: str
    handoffSummary: str
    acknowledgedBy: Optional[str] = None
    acknowledgedAt: Optional[datetime] = None
    createdAt: datetime
    updatedAt: datetime


class CaseNoteResponse(BaseModel):
    id: int
    caseId: int
    actor: str
    note: str
    createdAt: datetime


class ToolJobResponse(BaseModel):
    id: int
    reportId: int
    kind: str
    status: str
    attempts: int
    maxAttempts: int
    dependsOnJobId: Optional[int] = None
    runAfter: datetime
    lastError: str
    createdAt: datetime
    updatedAt: datetime


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: int
    kind: str
    reason: str
    payload: str
    createdAt: datetime


class AgentRunTraceResponse(BaseModel):
    id: int
    sessionId: str
    reportId: Optional[int] = None
    username: str
    intent: str
    riskLevel: str
    originalInput: str
    sanitizedInput: str
    memoryBrief: str
    agentSteps: list[dict[str, Any]]
    evidenceItems: list[dict[str, Any]]
    toolDiagnostics: dict[str, Any]
    responseMessages: list[dict[str, Any]]
    assessment: dict[str, Any]
    contextManifest: dict[str, Any]
    createdAt: datetime


class UserMemoryResponse(BaseModel):
    memoryId: str
    category: str
    content: str
    sourceMessageId: Optional[int] = None
    createdAt: datetime
    updatedAt: datetime
    lastUsedAt: Optional[datetime] = None
    origin: str = "EXPLICIT"
    version: int = 1
    status: str = "ACTIVE"
    sensitivity: str = "NORMAL"
    visibilityScope: str = "ALL_AGENTS"
    indexStatus: str = "PENDING"


class UserMemoryListResponse(BaseModel):
    items: list[UserMemoryResponse]


class UserMemoryDeleteResponse(BaseModel):
    memoryId: str
    deleted: bool


class ToolAuditResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: Optional[int] = None
    toolName: str
    policy: str
    allowed: bool
    status: str
    reason: str
    payload: dict[str, Any]
    createdAt: datetime
    updatedAt: datetime


class AiMessage(BaseModel):
    role: str
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


def authority(role: str) -> dict[str, Any]:
    return {"authority": role}
