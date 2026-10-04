"""API tests with a scripted provider (no LLM, real read-only DB)."""
import pytest
from fastapi.testclient import TestClient

from app.api import MAX_QUESTION_CHARS, SessionStore, create_app
from app.clarification import AmbiguityAnalysis
from app.config import get_settings
from app.llm.provider import LLMProvider
from app.schemas import SQLGeneration

pytestmark = pytest.mark.integration


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, analysis=None, sql="SELECT COUNT(*) AS n FROM dw.dim_customer"):
        self.analysis, self.sql = analysis, sql

    def generate_structured(self, system, user, schema, *, temperature=0.0):
        if schema is AmbiguityAnalysis:
            return self.analysis
        return SQLGeneration(sql=self.sql, assumptions=["a1"])

    def generate_text(self, system, user, *, temperature=0.2):
        return "There are N customers."


def client(**kw) -> TestClient:
    return TestClient(create_app(provider=Scripted(**kw), settings=get_settings()))


def session(c: TestClient) -> str:
    return c.post("/api/sessions").json()["session_id"]


def test_health_and_examples():
    c = client()
    assert c.get("/api/health").json()["status"] == "ok"
    assert len(c.get("/api/examples").json()) >= 3


def test_ask_answer_flow():
    c = client(analysis=AmbiguityAnalysis(status="clear", confidence=0.95))
    r = c.post("/api/ask", json={"session_id": session(c), "question": "How many customers do we have?"})
    body = r.json()
    assert r.status_code == 200 and body["kind"] == "answer"
    assert body["answer"]["row_count"] == 1 and body["answer"]["rows_preview"][0]["n"] > 0


def test_clarify_then_answer_flow():
    analysis = AmbiguityAnalysis(status="ambiguous", ambiguity_type="metric",
                                 interpretations=["revenue", "orders"], confidence=0.4)
    c = client(analysis=analysis)
    sid = session(c)
    first = c.post("/api/ask", json={"session_id": sid, "question": "Who is our best customer?"}).json()
    assert first["kind"] == "clarify" and first["clarification"]["options"] == ["revenue", "orders"]
    second = c.post("/api/clarify", json={"session_id": sid, "original_question": "Who is our best customer?",
                                          "ambiguity_type": first["clarification"]["ambiguity_type"],
                                          "choice": "revenue"}).json()
    assert second["kind"] == "answer" and second["answer"]["clarification_involved"] is True
    # session memory: the same ambiguity type is not asked again in this session
    third = c.post("/api/ask", json={"session_id": sid, "question": "Which region performs best?"}).json()
    assert third["kind"] == "answer"


def test_unsafe_sql_is_refused_not_500():
    c = client(analysis=AmbiguityAnalysis(status="clear", confidence=0.95), sql="SELECT * FROM public.customers")
    r = c.post("/api/ask", json={"session_id": session(c), "question": "show customers"})
    assert r.status_code == 200 and r.json()["kind"] == "refuse"


def test_out_of_scope_refusal():
    c = client(analysis=AmbiguityAnalysis(status="out_of_scope", refusal_reason="Not a data question."))
    r = c.post("/api/ask", json={"session_id": session(c), "question": "tell me a joke"}).json()
    assert r["kind"] == "refuse" and r["refusal_reason"] == "Not a data question."


@pytest.mark.parametrize("payload", [
    {"session_id": "x", "question": ""},
    {"session_id": "x", "question": "q" * (MAX_QUESTION_CHARS + 1)},
    {"question": "no session"},
])
def test_input_validation(payload):
    assert client().post("/api/ask", json=payload).status_code == 422


def test_unknown_session_is_404():
    assert client().post("/api/ask", json={"session_id": "nope", "question": "hi"}).status_code == 404


def test_session_store_is_bounded():
    store = SessionStore(capacity=3)
    ids = [store.create() for _ in range(5)]
    with pytest.raises(KeyError):
        store.get(ids[0])
    assert store.get(ids[-1])
