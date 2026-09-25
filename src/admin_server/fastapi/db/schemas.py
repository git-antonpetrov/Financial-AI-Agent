from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class CheckHashRequest(BaseModel):
    file_hash: str
    filename: str
    agent_name: str

class CheckHashResponse(BaseModel):
    status: str

class CheckDateRequest(BaseModel):
    system_name: str
    short_name: str
    file_hash: str
    filename: str
    agent_name: str

class CheckDateResponse(BaseModel):
    status: str

class LLMAnalyzeRequest(BaseModel):
    text: str

class LLMAnalyzeResponse(BaseModel):
    system_name: str
    short_name: str

class LLMRepealedRequest(BaseModel):
    snippets: List[str]

class LLMRepealedResponse(BaseModel):
    repealed_docs: List[str]


class AgentRequestResponse(BaseModel):
    id: int
    agent_name: str
    document_name: str
    justification: str
    status: str
    created_at: datetime
    
    class Config:
        from_attributes = True

class AgentRegisterRequest(BaseModel):
    agent_name: str
    public_key: str

class AgentRegisterResponse(BaseModel):
    status: str
    message: str

class AgentRequestJWT(BaseModel):
    token: str
