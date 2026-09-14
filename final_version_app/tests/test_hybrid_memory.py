"""Hybrid memory invariants; all model calls are replaced locally."""
from unittest.mock import patch
import json
import pytest
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
from final_version_app.application.hybrid_memory import HybridMemoryManager

def mem(tmp_path, **kwargs):
    return HybridMemoryManager(tmp_path/"skills", "thr_test", **kwargs)

def test_small_task_keeps_raw_and_does_not_summarize(tmp_path):
    m=mem(tmp_path)
    messages=[HumanMessage(content="precise requirement"),AIMessage(content="working")]
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",side_effect=AssertionError("no model needed")):
        assert m.compact_messages(messages) is messages
        m.maybe_schedule_extraction(messages)
    assert len(m.state["messages"])==2

def test_summary_and_raw_tail_are_both_kept(tmp_path):
    m=mem(tmp_path,recent_tokens=100)
    messages=[HumanMessage(content="Initial requirement: SQLite")] + [AIMessage(content="work "*200) for _ in range(15)]
    messages += [HumanMessage(content="Correction: PostgreSQL"), AIMessage(content="continue")]
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",
               return_value=AIMessage(content="## Current State\nWorking on database.\n## Task specification\nSQLite, subject to latest user corrections.")):
        result=m.compact_messages(messages)
    assert "<working-memory>" in result[0].content
    assert any(x.content=="Correction: PostgreSQL" for x in result)
    assert m.state["working"]["through_sequence"]>0
    assert "Initial requirement" in m.search("SQLite")

def test_summary_cursor_reused_across_restart(tmp_path):
    m=mem(tmp_path,recent_tokens=100)
    messages=[HumanMessage(content="original goal")] + [AIMessage(content="work "*200) for _ in range(15)]
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",
               return_value=AIMessage(content="## Current State\nRetained goal.")) as model:
        compacted=m.compact_messages(messages)
        calls=model.call_count
        restored=mem(tmp_path,recent_tokens=100)
        twice=restored.compact_messages(compacted)
        assert model.call_count==calls
    assert sum(bool(x.additional_kwargs.get("memory_internal")) for x in twice)==1

def test_summary_failure_never_discards_raw_or_advances_working(tmp_path):
    m=mem(tmp_path,recent_tokens=100)
    messages=[HumanMessage(content="important")] + [AIMessage(content="x"*900) for _ in range(15)]
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",side_effect=RuntimeError("offline")):
        with pytest.raises(RuntimeError):m.compact_messages(messages)
    assert "working" not in m.state
    assert len(messages)==16 and len(m.state["messages"])==16

def test_latest_user_survives_long_tool_chain_and_pairs(tmp_path):
    m=mem(tmp_path,recent_tokens=50)
    prompt=HumanMessage(content="Do not remove validation")
    messages=[prompt]
    for i in range(12):
        messages += [AIMessage(content="",tool_calls=[{"id":str(i),"name":"read","args":{}}]),
                     ToolMessage(content="log "*400,tool_call_id=str(i))]
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",return_value=AIMessage(content="## Current State\nWorking.")):
        result=m.compact_messages(messages)
    assert prompt in result
    calls={c["id"] for x in result if isinstance(x,AIMessage) for c in x.tool_calls}
    assert {x.tool_call_id for x in result if isinstance(x,ToolMessage)}<=calls

def test_all_retiring_sources_are_represented_in_batches(tmp_path):
    m=mem(tmp_path,recent_tokens=50,summary_batch_chars=2000)
    messages=[HumanMessage(content="FACT_"+str(i)+"x"*1200) for i in range(30)]
    seen=[]
    def summarize(messages, **kwargs):
        prompt=messages[0].content
        seen.extend(json.loads(prompt.split("New source excerpts:\n",1)[1]))
        return AIMessage(content="## Current State\nCombined state.")
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",side_effect=summarize):
        m.compact_messages(messages)
    expected=sum(s["sequence"]<=m.state["working"]["through_sequence"]
                 for e in m.state["episodes"] for s in m._source(e))
    assert len(seen)==expected and len({s["source_id"] for s in seen})==expected



def test_provider_heading_variants_preserve_summary(tmp_path):
    m=mem(tmp_path,recent_tokens=50)
    messages=[HumanMessage(content="SQLite requirement")] + [AIMessage(content="work "*200) for _ in range(15)]
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",
               return_value=AIMessage(content="# Current State\nKeep SQLite.\n### Task specification\nExact requirement.")):
        result=m.compact_messages(messages)
    assert "Keep SQLite." in result[0].content
    assert "Exact requirement." in result[0].content


def test_upgrade_cold_skill_checkpoint_warms_archived_history(tmp_path):
    from final_version_app.application.skill_memory import SkillMemoryManager
    old=SkillMemoryManager(tmp_path/"skills", "thr_test", auto_summary=False)
    messages=[HumanMessage(content="Old requirement SQLite")] + [AIMessage(content="work "*200) for _ in range(30)]
    cold=old.compact_messages(messages)
    assert len(cold)<len(messages)
    hybrid=mem(tmp_path)
    with patch("final_version_app.application.hybrid_memory.invoke_langchain",
               return_value=AIMessage(content="## Current State\nOld requirement SQLite")) as model:
        warmed=hybrid.compact_messages(cold)
    assert model.call_count>0
    assert "Old requirement SQLite" in warmed[0].content
    assert hybrid.state["working"]["through_sequence"]>0
