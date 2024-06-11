import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse
from langchain_community.utilities.sql_database import SQLDatabase
from starlette.background import BackgroundTask

from dataline.models.conversation.schema import (
    ConversationOut,
    ConversationWithMessagesWithResultsOut,
    CreateConversationIn,
    UpdateConversationRequest,
)
from dataline.models.llm_flow.schema import SQLQueryRunResult
from dataline.models.message.schema import MessageOptions, MessageWithResultsOut
from dataline.models.result.schema import ResultOut
from dataline.old_models import SuccessListResponse, SuccessResponse
from dataline.repositories.base import AsyncSession, get_session, get_session_no_commit
from dataline.services.connection import ConnectionService
from dataline.services.conversation import ConversationService
from dataline.services.llm_flow.toolkit import execute_sql_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversations"])


@router.get("/conversation/{conversation_id}")
async def get_conversation(
    conversation_id: UUID,
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(),
) -> SuccessResponse[ConversationOut]:
    conversation = await conversation_service.get_conversation(session, conversation_id=conversation_id)
    return SuccessResponse(data=conversation)


@router.get("/conversations")
async def conversations(
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(),
) -> SuccessListResponse[ConversationWithMessagesWithResultsOut]:
    conversations = await conversation_service.get_conversations(session)
    return SuccessListResponse(
        data=conversations,
    )


@router.get("/conversation/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: UUID,
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(),
) -> SuccessListResponse[MessageWithResultsOut]:
    conversation = await conversation_service.get_conversation_with_messages(session, conversation_id=conversation_id)
    messages = [MessageWithResultsOut.model_validate(message) for message in conversation.messages]
    return SuccessListResponse(data=messages)


@router.post("/conversation")
async def create_conversation(
    conversation_in: CreateConversationIn,
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(),
) -> SuccessResponse[ConversationOut]:
    conversation = await conversation_service.create_conversation(
        session, connection_id=conversation_in.connection_id, name=conversation_in.name
    )
    return SuccessResponse(
        data=conversation,
    )


@router.patch("/conversation/{conversation_id}")
async def update_conversation(
    conversation_id: UUID,
    conversation_in: UpdateConversationRequest,
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(),
) -> SuccessResponse[ConversationOut]:
    conversation = await conversation_service.update_conversation_name(
        session, conversation_id=conversation_id, name=conversation_in.name
    )
    return SuccessResponse(data=conversation)


@router.delete("/conversation/{conversation_id}")
async def delete_conversation(
    conversation_id: UUID,
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(),
) -> None:
    return await conversation_service.delete_conversation(session, conversation_id)


@router.post("/conversation/{conversation_id}/query")
def query(
    conversation_id: UUID,
    query: str,
    message_options: Annotated[MessageOptions, Body(embed=True)],
    session: AsyncSession = Depends(get_session_no_commit),
    conversation_service: ConversationService = Depends(),
):
    async def commit_and_close_session():
        try:
            # Commit only if no exception occurs
            await session.commit()
        except Exception:
            # If any exception encountered, rollback all changes
            await session.rollback()
            raise
        finally:
            await session.close()

    return StreamingResponse(
        conversation_service.query(session, conversation_id, query, secure_data=message_options.secure_data),
        media_type="text/event-stream",
        background=BackgroundTask(commit_and_close_session),  # this only runs after the query is finished
    )


@router.get("/conversation/{conversation_id}/run-sql")
async def execute_sql(
    conversation_id: UUID,
    sql: str,
    linked_id: UUID,
    limit: int = 10,
    execute: bool = True,
    session: AsyncSession = Depends(get_session),
    conversation_service: ConversationService = Depends(ConversationService),
    connection_service: ConnectionService = Depends(ConnectionService),
) -> SuccessResponse[ResultOut]:
    # Get conversation
    # Will raise error that's auto captured by middleware if not exists
    conversation = await conversation_service.get_conversation(session, conversation_id=conversation_id)

    # Get connection
    connection_id = conversation.connection_id
    connection = await connection_service.get_connection(session, connection_id)

    # Refresh chart data
    db = SQLDatabase.from_uri(connection.dsn)
    query_run_data = execute_sql_query(
        db,
        sql,
    )

    # Execute query
    result = SQLQueryRunResult(
        columns=query_run_data.columns,
        rows=query_run_data.rows,
        for_chart=False,
        linked_id=linked_id,
    )

    return SuccessResponse(data=result.serialize_result())
