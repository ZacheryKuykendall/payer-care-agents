"""Decision-model abstraction.

The router needs one thing: a calibrated probability distribution over a fixed option
set. ``Microsoft-Decision-1`` provides exactly that, but a reference architecture that
only works with one vendor model is not a reference architecture, so everything here
is written against the :class:`DecisionClient` protocol.
"""

from payer_agents.decisions.base import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionClient,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    NoulQuestion,
    Question,
    QuestionType,
    ScoreAnswer,
    ScoreQuestion,
    parse_decision_payload,
)
from payer_agents.decisions.fallback import CircuitBreaker, ResilientDecisionClient
from payer_agents.decisions.foundry import FoundryDecisionClient
from payer_agents.decisions.mock import HeuristicDecisionClient, MockDecisionClient

__all__ = [
    "ChoiceAnswer",
    "ChoiceQuestion",
    "CircuitBreaker",
    "DecisionClient",
    "DecisionError",
    "DecisionRequest",
    "DecisionResponse",
    "FoundryDecisionClient",
    "HeuristicDecisionClient",
    "MockDecisionClient",
    "NoulAnswer",
    "NoulQuestion",
    "Question",
    "QuestionType",
    "ResilientDecisionClient",
    "ScoreAnswer",
    "ScoreQuestion",
    "parse_decision_payload",
]
