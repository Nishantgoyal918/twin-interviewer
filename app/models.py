"""Pydantic schemas for domain objects (plan section 7).

Models the LLM fills (TwinSpec, ObserverOutput) have no defaults and no free-form dicts, so
they work with OpenAI strict structured outputs.
"""

from __future__ import annotations

import warnings
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# "register" is the natural field name; it only shadows ABCMeta.register, which we never use.
warnings.filterwarnings("ignore", message='Field name "register"')

Register = Literal["instructing", "storytelling", "opinion", "reacting", "small_talk", "other"]
REGISTERS: tuple[str, ...] = ("instructing", "storytelling", "opinion", "reacting", "small_talk")
Engagement = Literal["engaged", "flat", "avoidant", "tired", "uncertain"]


# ---- TwinSpec (7.1) -------------------------------------------------------------------------

class Subject(BaseModel):
    name: str
    relation_to_creator: str | None
    languages: list[str]
    code_switching: bool


class ContentDomain(BaseModel):
    id: str
    name: str
    priority: Literal["high", "med", "low"]
    open_list: bool
    slot_template: list[str]


class RegisterWeights(BaseModel):
    instructing: float
    storytelling: float
    opinion: float
    reacting: float
    small_talk: float


class SessionPrefs(BaseModel):
    target_minutes: int
    max_minutes: int
    text_idle_end_hours: int


class TwinSpec(BaseModel):
    subject: Subject
    audience: str
    purpose: str
    interviewer_stance: str
    motivation_archetypes: list[
        Literal["utility", "expertise", "legacy", "being_heard", "recognition"]
    ]
    content_domains: list[ContentDomain]
    register_weights: RegisterWeights
    emotional_range_weight: float
    modalities: list[Literal["voice", "text"]]
    existing_corpus: list[str]
    sensitivities: list[str]
    creator_seeds: list[str]
    session_prefs: SessionPrefs


# ---- Observer (7.4) -------------------------------------------------------------------------

class Fact(BaseModel):
    kind: Literal["fact", "preference", "person", "story_hook", "followup"]
    text: str


class SlotUpdate(BaseModel):
    domain_id: str
    item: str
    slot: str
    status: Literal["partial", "done"]


class ObserverOutput(BaseModel):
    # JSON name is "register"; the Python name avoids BaseModel.register, which would otherwise
    # become the field's default and make it optional in the strict schema.
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)

    facts: list[Fact]
    topics: list[str]
    talk_register: Register = Field(alias="register")
    new_interests: list[str]
    deflection: bool
    deflected_topic: str | None
    lexical_engagement: Literal["high", "medium", "low", "unclear"]
    slot_updates: list[SlotUpdate]


# ---- Planner (7.3) --------------------------------------------------------------------------

class Goal(BaseModel):
    id: str
    kind: Literal["content_slot", "register", "choice", "wrap_up", "seed"]
    domain_id: str | None = None
    item: str | None = None
    slot: str | None = None
    register: str | None = None
    bridge: str | None = None
    set_at_turn: int = 0


class SessionBudget(BaseModel):
    target_minutes: int
    max_duration_s: int
    elapsed_s: float = 0
    energy_estimate: float = 1.0


class SessionState(BaseModel):
    session_id: str
    twin_id: str
    modality: Literal["voice", "text"]
    engagement_trend: list[str] = Field(default_factory=list)
    session_budget: SessionBudget
    current_goal: Goal | None = None
    guidance: str = ""
    bridge_sources: list[str] = Field(default_factory=list)
    session_baseline: dict[str, dict[str, float]] = Field(default_factory=dict)
    subject_word_counts: list[int] = Field(default_factory=list)
    wrap_up: bool = False
    last_analyzed_idx: int = -1
    # Breadth over depth: how long the current goal has been waiting, how long they've been
    # running with their own thread, which goals this session gave up on, which domains it
    # has already turned to.
    goal_since_s: float = 0
    engaged_run: int = 0
    skipped_goals: list[str] = Field(default_factory=list)
    domains_visited: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    engagement: Engagement
    policy: str
    goal: Goal | None
    guidance: str
    wrap_up: bool
    dropped_topic: str | None = None
    tags: list[dict] = Field(default_factory=list)
    firm: bool = False   # a move the interviewer should make now, not a soft hint


# ---- Signals (7.5) --------------------------------------------------------------------------

class Tag(BaseModel):
    tag: str
    value: str
    z: float | None = None
    confidence: float
