from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.agents.event_driven_runtime import EventDrivenAgentRuntimeService
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_user, require_admin
from app.models.entities import UserAccount
from app.schemas.dtos import (
    ChatRequest,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    KnowledgeDocumentMetadataRequest,
    KnowledgePublishRequest,
    KnowledgeReprocessRequest,
    UserMemoryDeleteResponse,
    UserMemoryListResponse,
    UserMemoryResponse,
    authority,
)
from app.services.chat import ChatService
from app.services.chat_turns import ChatTurnNotFoundError
from app.services.conversations import (
    ArchivedConversationError,
    ConversationNotFoundError,
    StudentConversationService,
)
from app.services.knowledge import KnowledgeService
from app.services.knowledge_ingestion.admin import KnowledgeAdminService
from app.services.model_assets import finetuned_model_status
from app.services.report import ReportService
from app.services.skills import MindBridgeSkillLibrary
from app.services.user_memory import UserMemoryService

router = APIRouter()


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    if request.sessionId:
        try:
            StudentConversationService(db, get_settings()).require_available(user.id, request.sessionId)
        except ArchivedConversationError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ConversationNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
    service = ChatService(db, get_settings())
    return StreamingResponse(service.start_chat(user, request), media_type="text/event-stream")


@router.get("/api/chat/turns/{request_id}/stream")
async def reconnect_chat_stream(
    request_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    service = ChatService(db, get_settings())
    try:
        service._turn_state(user.id, request_id)
    except ChatTurnNotFoundError as exc:
        raise HTTPException(404, "生成任务不存在") from exc
    return StreamingResponse(service.stream_turn(user.id, request_id), media_type="text/event-stream")


@router.get("/api/conversations")
def student_conversations(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
):
    return StudentConversationService(db, get_settings()).list_active(user.id, limit)


@router.get("/api/conversations/{session_id}")
def student_conversation(
    session_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return StudentConversationService(db, get_settings()).get_active(user.id, session_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/conversations/{session_id}/archive")
def archive_student_conversation(
    session_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return StudentConversationService(db, get_settings()).archive(user.id, session_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    framework = agent_framework_status(settings)
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": framework,
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "CoordinatorAgent", "status": "READY", "description": "维护任务板、预算、安全门槛、冲突仲裁和最终采纳"},
            {"name": "UnderstandingAgent", "status": "READY", "description": "独立理解用户输入，发布 intent artifact"},
            {"name": "SafetyAgent", "status": "READY", "description": "独立风险评估、SAFETY_OVERRIDE 和候选回复安全审查"},
            {"name": "GeneralChatAgent", "status": "READY", "description": "通用对话与只读外部信息工具"},
            {"name": "AcademicPlanningAgent", "status": "READY", "description": "学业规划与本地知识检索"},
            {"name": "CampusAffairsAgent", "status": "READY", "description": "校园事务与本地知识检索"},
            {"name": "PsychologicalSupportAgent", "status": "READY", "description": "心理支持与本地知识检索"},
            {"name": "ResponseAgent", "status": "READY", "description": "根据黑板 artifact 发布候选回复方案"},
        ],
        "skills": MindBridgeSkillLibrary.status_items(),
        "runtimeHarness": {
            "name": "MindBridgeAgentHarness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "event-driven-multi-agent",
            "maxRounds": min(
                int(settings.agent_max_rounds),
                int(settings.agent_max_rounds_hard_limit),
            ),
            "scheduler": "claim-based-actor-runtime",
        },
        "collaboration": {
            "scheduler": "claim-based",
            "state": "append-only-blackboard",
            "messageBus": "per-agent inbox over shared mailbox",
            "fixedWorkflow": False,
            "agentIsolation": {
                "prompt": "per-agent system prompt",
                "context": "projected view from one TurnContextPacket",
                "model": "per-agent model profile",
                "tools": "per-agent tool permissions",
            },
        },
    }


@router.get("/api/reports/me")
def my_reports(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports(user.id)


@router.get("/api/user/memories", response_model=UserMemoryListResponse)
def user_memories(
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    rows = UserMemoryService(db).list_active(user.id)
    return UserMemoryListResponse(
        items=[
            UserMemoryResponse(
                memoryId=row.public_id,
                category=row.category,
                content=row.content,
                sourceMessageId=row.source_message_id,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
                lastUsedAt=row.last_used_at,
                origin=row.origin,
                version=row.version,
                status=row.status,
                sensitivity=row.sensitivity,
                visibilityScope=row.visibility_scope,
                indexStatus=row.index_status,
            )
            for row in rows
        ]
    )


@router.delete(
    "/api/user/memories/{memory_id}",
    response_model=UserMemoryDeleteResponse,
)
def delete_user_memory(
    memory_id: str,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if not UserMemoryService(db).delete(user.id, memory_id):
        raise HTTPException(404, "记忆不存在")
    return UserMemoryDeleteResponse(memoryId=memory_id, deleted=True)


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).alert_records()


@router.get("/api/admin/cases")
def admin_cases(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).risk_cases()


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(case_id: int, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).case_notes(case_id)


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).dead_letters()


@router.get("/api/admin/agent-traces")
def admin_agent_traces(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).agent_run_traces()


@router.get("/api/admin/tool-audits")
def admin_tool_audits(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_audits()


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(session_id: str, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        return ReportService(db).conversation(session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    chunks = KnowledgeService(db, get_settings()).ingest_file(file.filename or "uploaded-file", await file.read())
    return KnowledgeIngestResponse(source=file.filename or "uploaded-file", chunks=chunks)


@router.post("/api/admin/knowledge/documents", status_code=202)
async def create_knowledge_document(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    title: str = Form(...),
    canonical_key: str = Form(""),
    domain: str = Form(...),
    tags: str = Form(""),
    site: str = Form("ALL"),
    version: str = Form("1"),
    parser_profile: str = Form("auto"),
    chunking_profile: str = Form("structure_token_v2"),
    verified_at: str = Form(""),
    expires_at: str = Form(""),
):
    filename = file.filename or "uploaded-file"
    try:
        service = KnowledgeAdminService(db, get_settings())
        job = service.enqueue_document(
            filename=filename,
            data=await file.read(),
            mime_type=file.content_type or "application/octet-stream",
            title=title,
            canonical_key=canonical_key,
            domain=domain,
            tags=_parse_tags(tags),
            site=site,
            version=version,
            parser_profile=parser_profile,
            chunking_profile=chunking_profile,
            verified_at=_parse_optional_datetime(verified_at),
            expires_at=_parse_optional_datetime(expires_at),
        )
        payload = service.get_job(job.id)
        payload["canonicalKey"] = canonical_key.strip() or filename.rsplit(".", 1)[0]
        return payload
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.get("/api/admin/knowledge/documents")
def list_knowledge_documents(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    return {"items": KnowledgeAdminService(db, get_settings()).list_documents()}


@router.get("/api/admin/knowledge/documents/{document_id}")
def get_knowledge_document(
    document_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return KnowledgeAdminService(db, get_settings()).get_document(document_id)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.get("/api/admin/knowledge/documents/{document_id}/preview")
def preview_knowledge_document(
    document_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return KnowledgeAdminService(db, get_settings()).preview_document(document_id)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.patch("/api/admin/knowledge/documents/{document_id}")
def update_knowledge_document(
    document_id: int,
    request: KnowledgeDocumentMetadataRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return KnowledgeAdminService(db, get_settings()).update_metadata(
            document_id,
            title=request.title,
            domain=request.domain,
            tags=tuple(request.tags),
            site=request.site,
            version=request.version,
            verified_at=request.verifiedAt,
            expires_at=request.expiresAt,
        )
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.post("/api/admin/knowledge/documents/{document_id}/reprocess", status_code=202)
def reprocess_knowledge_document(
    document_id: int,
    request: KnowledgeReprocessRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        service = KnowledgeAdminService(db, get_settings())
        job = service.enqueue_reprocess(
            document_id,
            parser_profile=request.parserProfile,
            chunking_profile=request.chunkingProfile,
        )
        return service.get_job(job.id)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.post("/api/admin/knowledge/documents/{document_id}/publish")
def publish_knowledge_document(
    document_id: int,
    request: KnowledgePublishRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return KnowledgeAdminService(db, get_settings()).publish(document_id, verified_at=request.verifiedAt)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.post("/api/admin/knowledge/documents/{document_id}/deactivate")
def deactivate_knowledge_document(
    document_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return KnowledgeAdminService(db, get_settings()).deactivate(document_id)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.get("/api/admin/knowledge/jobs/{job_id}")
def get_knowledge_job(
    job_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return KnowledgeAdminService(db, get_settings()).get_job(job_id)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


@router.post("/api/admin/knowledge/jobs/{job_id}/retry", status_code=202)
def retry_knowledge_job(
    job_id: int,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        service = KnowledgeAdminService(db, get_settings())
        return service.get_job(service.retry_job(job_id).id)
    except ValueError as exc:
        raise _knowledge_admin_http_error(exc) from exc


def _parse_tags(raw: str) -> tuple[str, ...]:
    value = raw.strip()
    if not value:
        return ()
    if value.startswith("["):
        import json

        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("knowledge_tags_invalid")
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    return tuple(item.strip() for item in value.replace("，", ",").split(",") if item.strip())


def _parse_optional_datetime(raw: str):
    from datetime import datetime

    value = raw.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError("knowledge_datetime_invalid") from exc


def _knowledge_admin_http_error(exc: ValueError) -> HTTPException:
    code = str(exc)
    if code in {"knowledge_document_not_found", "knowledge_ingestion_job_not_found", "knowledge_artifact_not_found"}:
        return HTTPException(404, code)
    if code in {"knowledge_ingestion_job_already_active", "knowledge_ingestion_job_not_retryable"}:
        return HTTPException(409, code)
    return HTTPException(400, code)
