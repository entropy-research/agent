import asyncio
from contextlib import asynccontextmanager
import json
import os
from time import sleep
from typing import Any, Dict, List, Optional

import fastapi
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
import json
from contextlib import asynccontextmanager
from devon_agent.agents.default.agent import AgentArguments, TaskAgent
from devon_agent.models import ENGINE, Base, init_db, load_data
from devon_agent.session import (
    Event,
    Session,
    SessionArguments,
)
from fastapi.middleware.cors import CORSMiddleware


from fastapi.responses import StreamingResponse

# API
# SESSION
# - get sessions
# - create session
# - start session
# repond session
# interrupt session
# stop session
# delete session
# get session event history
# get session event stream


origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

sessions: Dict[str, Session] = {}
running_sessions: List[Session] = []


def get_user_input(session: str):
    if session not in session_buffers:
        while True:
            if session not in session_buffers:
                sleep(0.1)
                continue
            else:
                break

        result = session_buffers[session]
        del session_buffers[session]
        return result
    else:
        result = session_buffers[session]
        del session_buffers[session]
        return result



@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):

    # Hacky but it works
    global sessions
    app.persist = False
    if app.persist:
    
        await init_db()

        AsyncSessionLocal = sessionmaker(
            bind=ENGINE, class_=AsyncSession, expire_on_commit=False
        )
        async with AsyncSessionLocal() as db_session:
            app.db_session = db_session
            data = await load_data(db_session)
            data = {
                k: Session.from_dict(v, lambda: get_user_input(k), persist=True)
                for (k, v) in data.items()
            }
            sessions = data
            for k, v in sessions.items():
                v.setup()
                # background_tasks.add_task(v.run_event_loop)
                
    yield


app = fastapi.FastAPI(
    lifespan=lifespan,
)

app.persist = False
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


session_buffers: Dict[str, str] = {}


@app.get("/")
def read_root():
    return {"content": "Hello from Devon!"}


@app.get("/sessions")
def get_sessions():
    # TODO: figure out the right information to send
    return [
        {"name": session_name, "path": session_data.base_path}
        for session_name, session_data in sessions.items()
    ]


@app.post("/sessions/{session}")
def create_session(
    session: str,
    path: str,
    config: AgentArguments,
    background_tasks: fastapi.BackgroundTasks,
):
    if not os.path.exists(path):
        raise fastapi.HTTPException(status_code=404, detail="Path not found")

    if session in sessions:
        raise fastapi.HTTPException(
            status_code=400, detail=f"Session with id {session} already exists"
        )

    agent = TaskAgent(name="Devon", temperature=0.0, args=config)

    sessions[session] = Session(
        SessionArguments(
            path, user_input=lambda: get_user_input(session), name=session
        ),
        agent,
        app.persist
    )

    sessions[session].init_state()

    sessions[session].setup()
    background_tasks.add_task(sessions[session].run_event_loop)
    running_sessions.append(session)

    return session


@app.delete("/sessions/{session}")
def delete_session(session: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")

    sessions[session].delete_from_db()
    del sessions[session]
    if session in running_sessions:
        running_sessions.remove(session)

    return session


@app.patch("/sessions/{session}/start")
def start_session(session: str, background_tasks: fastapi.BackgroundTasks, api_key: Optional[str] = None):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")

    session_obj = sessions.get(session)
    session_obj.agent.api_key = api_key
    if session not in running_sessions:
        background_tasks.add_task(sessions[session].run_event_loop)
        running_sessions.append(session)

    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    
    session_obj.start()

    return session


@app.patch("/sessions/{session}/pause")
def pause_session(session: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")

    session_obj = sessions.get(session)
    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_obj.pause()

    return session


@app.patch("/sessions/{session}/terminate")
def terminate(session: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")

    session_obj = sessions.get(session)
    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_obj.terminate()

    return session


@app.patch("/sessions/{session}/reset")
def reset_session(session: str, background_tasks: fastapi.BackgroundTasks):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")

    session_obj = sessions.get(session)

    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_buffers[session]="terminate"
    session_obj.terminate()
    session_obj.init_state()
    session_obj.setup()
    if session in session_buffers:
        del session_buffers[session]
    background_tasks.add_task(session_obj.run_event_loop)

    return session


@app.get("/sessions/{session}/status")
def get_session_status(session : str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_obj = sessions.get(session)
    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    return session_obj.status


@app.get("/sessions/{session}/state")
def get_session_state(session: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_obj = sessions.get(session)
    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    
    state = session_obj.state.to_dict()
    state["path"] = session_obj.base_path
    return state


@app.post("/sessions/{session}/response")
def create_response(session: str, response: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_buffers[session] = response
    return session_buffers[session]


# Event State code
class ServerEvent(BaseModel):
    type: str  # types: ModelResponse, ToolResponse, UserRequest, Interrupt, Stop
    content: Any
    producer: str | None
    consumer: str | None


@app.post("/sessions/{session}/event")
def create_event(session: str, event: ServerEvent):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    sessions[session].event_log.append(event.model_dump())
    return event


@app.get("/sessions/{session}/events")
def read_events(session: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    events = sessions.get(session, None).event_log
    return events


@app.get("/sessions/{session}/events/stream")
async def read_events_stream(session: str):
    if session not in sessions:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")
    session_obj: Session = sessions.get(session)
    if not session_obj:
        raise fastapi.HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        initial_index = len(session_obj.event_log)
        while True:
            current_index = len(session_obj.event_log)
            if current_index > initial_index:
                for event in session_obj.event_log[initial_index:current_index]:
                    yield f"data: {json.dumps(event)}\n\n"
                initial_index = current_index
            else:
                await asyncio.sleep(0.1)  # Sleep to prevent busy waiting

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    import sys

    port = 8000  # Default port
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("Warning: Invalid port number provided. Using default port 8000.")

        if os.environ.get("OPENAI_API_KEY"):
            app.api_key = os.environ.get("OPENAI_API_KEY")
            app.model = "gpt4-o"
            app.prompt_type = "openai"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            app.api_key = os.environ.get("ANTHROPIC_API_KEY")
            app.model = "claude-opus"
            app.prompt_type = "anthropic"
        else:
            raise ValueError("API key not provided.")

        if os.environ.get("DEVON_MODEL"):
            app.model = os.environ.get("DEVON_MODEL")

    uvicorn.run(app, host="0.0.0.0", port=port)
