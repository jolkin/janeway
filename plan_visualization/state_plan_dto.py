"""
State plan DTOs copied from pykirk for parsing Kirk plan JSON.

These mirror pykirk/src/pykirk/shared/dtos/state_plan.py and dispatch.py
so the visualization module can parse plans independently.
"""

from enum import Enum
from typing import Union, Optional, List
from pydantic import BaseModel, Field, ConfigDict


class EpisodeDurationType(str, Enum):
    """Type of episode duration"""

    REQUIREMENT = "simpleDuration"
    CONTINGENT = "simpleContingentDuration"


class EventDTO(BaseModel):
    """Temporal event in the plan"""

    id: str = Field(validation_alias="$id")


class EventRefDTO(BaseModel):
    """Reference to an event by ID"""

    ref: str


class StateVariableDTO(BaseModel):
    """State variable in the plan"""

    id: str = Field(validation_alias="$id")


class TemporalConstraintExpressionDTO(BaseModel):
    """Temporal constraint between two events"""

    type: str = Field(validation_alias="$type")
    from_: EventRefDTO = Field(validation_alias="from")
    to_: EventRefDTO = Field(validation_alias="to")
    lowerBound: int | str | float
    upperBound: int | str | float

    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True)


class StateConstraintExpressionDTO(BaseModel):
    """State-based constraint expression"""

    type: str = Field(validation_alias="$type")
    left: dict | str
    right: str | bool | list


class AnnotationDTO(BaseModel):
    """Constraint annotations"""

    causalLink: Union[bool, StateConstraintExpressionDTO] = Field(default=False)


class ConstraintDTO(BaseModel):
    """General constraint in the plan"""

    id: str = Field(validation_alias="$id")
    type: str = Field(validation_alias="$type")
    expression: Union[TemporalConstraintExpressionDTO, StateConstraintExpressionDTO]
    annotations: Optional[AnnotationDTO] = Field(
        default=None, validation_alias="$annotations"
    )


class EpisodeDurationDTO(BaseModel):
    """Duration specification for an episode"""

    type: EpisodeDurationType = Field(validation_alias="$type")
    lowerBound: int | str | float
    upperBound: int | str | float


class EpisodeDTO(BaseModel):
    """Episode with start/end events and duration constraints"""

    id: str = Field(validation_alias="$id")
    startEvent: str
    endEvent: str
    duration: EpisodeDurationDTO
    startConstraints: list = Field(default_factory=list)
    overAllConstraints: list = Field(default_factory=list)
    endConstraints: list = Field(default_factory=list)
    activityName: str = ""
    activityArgs: list = Field(default_factory=list)


class StateSpaceDTO(BaseModel):
    """State space containing all events"""

    type: str = Field(validation_alias="$type")
    events: list[EventDTO]


class StatePlanDTO(BaseModel):
    """
    Complete state plan from external planner.

    Top-level DTO representing a plan that will be converted to a
    Temporal Network for dispatch execution.
    """

    stateSpace: StateSpaceDTO
    startEvent: str
    constraints: list[ConstraintDTO]
    goalEpisodes: list[EpisodeDTO]
    valueEpisodes: list[EpisodeDTO]


class ExecutionDTO(BaseModel):
    """Represent an event execution."""

    event: str
    execution_time: float
    is_controllable: bool
