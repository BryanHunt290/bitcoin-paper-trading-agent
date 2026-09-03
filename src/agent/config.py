from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional


class BedrockConfig(BaseModel):
    aws_region: Optional[str] = None
    bedrock_model_id: str = Field(..., description='Bedrock model identifier (e.g., model-name)')
    bedrock_connect_timeout_seconds: int = Field(5, ge=1, le=60)
    bedrock_read_timeout_seconds: int = Field(30, ge=1, le=120)
    bedrock_max_attempts: int = Field(3, ge=1, le=10)
    max_agent_iterations: int = Field(3, ge=1, le=20)
    max_tool_calls: int = Field(10, ge=1, le=200)
    bedrock_max_response_bytes: int = Field(64 * 1024, ge=1024)

    model_config = {
        'extra': 'forbid'
    }

    @field_validator('bedrock_model_id')
    def model_id_must_not_be_empty(cls, v):
        if not v or (isinstance(v, str) and not v.strip()):
            raise ValueError('bedrock_model_id must be provided')
        return v

    @field_validator('aws_region', mode='before')
    def region_or_none(cls, v):
        if v == '':
            return None
        return v
