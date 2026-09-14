"""Behavioral invariants for Skill-backed historical memory (no network)."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from final_version_app.application.skill_memory import SkillMemoryManager
from final_version_app.application.session_memory import annotate_messages, message_uuid
from final_version_app.application.compression import microcompact, apply_tool_result_budget, auto_compact
from final_version_app.domain.skills import SkillLoader
from final_version_app.engine.runtime import AgentRuntime
from final_version_app.storage import JsonlEventStore, JsonThreadStore


def manager(tmp_path, name="thr_a", **kwargs):
    return SkillMemoryManager(tmp_path / "skills", name, auto_summary=False, **kwargs)


def test_all_messages_archived_over_80_and_recover_after_restart(tmp_path):
    memory = manager(tmp_path, chunk_chars=2000)
    messages = [HumanMessage(content=f"requirement-{i}") for i in range(110)] + [AIMessage(content="done")]
    memory.observe(messages)
    assert len(memory.state["messages"]) == 111
    restarted = manager(tmp_path)
    assert len(restarted.state["messages"]) == 111
    found = json.loads(restarted.search("requirement-0"))
    assert any("requirement-0" in s["text"] for e in found["results"] for s in e["snippets"])
    for episode in restarted.state["episodes"]:
        assert len(restarted._source(episode)) == len(episode["source_ids"])


def test_compact_twice_and_short_new_turn_still_archived(tmp_path):
    memory = manager(tmp_path)
    messages = [HumanMessage(content=f"turn {i}") for i in range(20)]
    compacted = memory.compact_messages(messages)
    compacted.append(HumanMessage(content="CORRECTION now use PostgreSQL"))
    memory.maybe_schedule_extraction(compacted)
    twice = memory.compact_messages(compacted)
    assert sum(bool(m.additional_kwargs.get("memory_internal")) for m in twice) == 1
    assert len(memory.state["messages"]) == 21
    assert "PostgreSQL" in memory.search("PostgreSQL")


def test_failed_summary_keeps_verbatim_and_coverage(tmp_path):
    memory = manager(tmp_path)
    memory.observe([HumanMessage(content="must preserve exact symbol KEEP_ME")])
    with patch("final_version_app.application.skill_memory.invoke_langchain", side_effect=RuntimeError("offline")):
        memory.summarize_pending()
    assert "KEEP_ME" in memory.search("KEEP_ME")
    assert len(memory.state["messages"]) == 1
    assert memory.state["episodes"][0]["summary_error"] == "RuntimeError"


def test_noop_invalid_summary_cannot_destroy_sources(tmp_path):
    memory = manager(tmp_path)
    memory.observe([HumanMessage(content="latest requirement")])
    with patch("final_version_app.application.skill_memory.invoke_langchain", return_value=AIMessage(content="{}")):
        memory.summarize_pending()
    assert "latest requirement" in manager(tmp_path).search("latest")
    assert memory.state["episodes"][0]["summary_status"] == "source_excerpt"


def test_thread_isolation_and_path_traversal(tmp_path):
    a,b = manager(tmp_path,"thr_a"),manager(tmp_path,"thr_b")
    a.observe([HumanMessage(content="PRIVATE_A_SECRET")])
    assert json.loads(b.search("PRIVATE_A_SECRET"))["results"] == []
    with pytest.raises(ValueError):
        b.read(a.state["episodes"][0]["id"])
    with pytest.raises(ValueError):
        b.read("../thr_a/index")
    with pytest.raises(ValueError):
        manager(tmp_path,"../escape")


def test_pagination_recovers_full_text(tmp_path):
    memory = manager(tmp_path)
    memory.observe([HumanMessage(content="head" + "x"*15000 + "UNIQUE_TAIL")])
    eid = memory.state["episodes"][0]["id"]
    text,offset = "",0
    while offset is not None:
        page = json.loads(memory.read(eid,offset,1000))
        text += page["text"]
        offset = page["next_offset"]
    assert "UNIQUE_TAIL" in text
    assert text == (memory.root/(eid+".md")).read_text(encoding="utf-8")


def test_new_skills_discovered_and_updated(tmp_path):
    loader = SkillLoader(tmp_path/"skills")
    manager(tmp_path)
    assert "project-memory" in loader.descriptions()
    assert "memory_search" in loader.load("project-memory")


def test_uuid_and_microcompact_idempotent(tmp_path):
    messages = [ToolMessage(content="data"*2000,tool_call_id=str(i)) for i in range(10)]
    annotate_messages(messages)
    ids = [message_uuid(m) for m in messages]
    memory = manager(tmp_path)
    memory.observe(messages)
    apply_tool_result_budget(messages)
    microcompact(messages)
    once = [m.content for m in messages]
    apply_tool_result_budget(messages)
    microcompact(messages)
    assert ids == [message_uuid(m) for m in messages]
    assert once == [m.content for m in messages]
    assert len(memory._source(memory.state["episodes"][0])[0]["content"]) == 8000


def test_compaction_keeps_tool_call_pair(tmp_path):
    memory = manager(tmp_path)
    call = AIMessage(content="",tool_calls=[{"name":"read","id":"c","args":{}}])
    messages = [HumanMessage(content="old") for _ in range(10)] + [call]
    messages += [ToolMessage(content="result",tool_call_id="c")] + [HumanMessage(content="tail") for _ in range(5)]
    result = memory.compact_messages(messages)
    calls = {c["id"] for m in result if isinstance(m,AIMessage) for c in m.tool_calls}
    assert {m.tool_call_id for m in result if isinstance(m,ToolMessage)} <= calls


def test_archive_failure_never_falls_back_to_lossy_summary(tmp_path):
    memory = manager(tmp_path)
    with patch.object(memory,"observe",side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            auto_compact([HumanMessage(content="critical")],session_memory=memory)


def test_decisions_superseded_only_with_valid_user_source(tmp_path):
    memory = manager(tmp_path)
    first = HumanMessage(content="Use SQLite")
    memory.observe([first])
    def response(key,value,ids):
        return AIMessage(content=json.dumps({"title":"database","summary":value,
            "decisions":[{"key":key,"value":value,"source_ids":ids}]}))
    with patch("final_version_app.application.skill_memory.invoke_langchain",
               return_value=response("database","SQLite",[message_uuid(first)])):
        memory.summarize_pending()
    correction = HumanMessage(content="Correction: use PostgreSQL")
    memory.observe([correction])
    with patch("final_version_app.application.skill_memory.invoke_langchain",
               return_value=response("database","PostgreSQL",[message_uuid(correction)])):
        memory.summarize_pending()
    decisions = memory.state["decisions"]
    assert decisions[0]["status"] == "superseded"
    assert decisions[1]["supersedes"] == [decisions[0]["id"]]
    assert decisions[1]["status"] == "active"
    memory.observe([AIMessage(content="assistant guesses MySQL")])
    with patch("final_version_app.application.skill_memory.invoke_langchain",
               return_value=response("database","MySQL",["invented-source"])):
        memory.summarize_pending()
    assert len(memory.state["decisions"]) == 2


def test_fork_independent_source_snapshot(tmp_path):
    parent,child = manager(tmp_path,"parent"),manager(tmp_path,"child")
    parent.observe([HumanMessage(content="original decision")])
    child.fork_from(parent)
    child.observe([HumanMessage(content="child-only correction")])
    assert "original decision" in child.search("original")
    assert json.loads(parent.search("child-only"))["results"] == []
    assert manager(tmp_path,"child").state["forked_from"] == "parent"


def test_runtime_factory_scopes_tools_and_resume(tmp_path):
    contexts = {}
    def factory(tid):
        memory = manager(tmp_path,tid)
        context = (SimpleNamespace(session_memory=memory),SimpleNamespace(), "system")
        contexts[tid] = context
        return context
    def execute(messages, services, tools, system, *, observer, cancellation):
        services.session_memory.observe(messages)
        response=AIMessage(content="ok")
        messages.append(response)
        observer.message_appended(response)
    def runtime():
        return AgentRuntime(services=SimpleNamespace(),tool_runtime=SimpleNamespace(),system_prompt="",
            event_store=JsonlEventStore(tmp_path/"runtime"),thread_store=JsonThreadStore(tmp_path/"runtime"),
            turn_executor=execute,thread_context_factory=factory)
    first=runtime()
    a,b=first.start_thread(),first.start_thread()
    first.run_turn(a.thread_id,"ALPHA_PRIVATE")
    first.run_turn(b.thread_id,"BETA_PRIVATE")
    assert json.loads(contexts[b.thread_id][0].session_memory.search("ALPHA_PRIVATE"))["results"] == []
    second=runtime()
    second.resume_thread(a.thread_id)
    second.run_turn(a.thread_id,"continue")
    assert "ALPHA_PRIVATE" in contexts[a.thread_id][0].session_memory.search("ALPHA_PRIVATE")



def test_old_compacted_thread_bootstraps_original_event_history(tmp_path):
    from final_version_app.protocol import EventKind, RuntimeEvent
    from final_version_app.engine.event_observer import RuntimeLoopObserver
    from final_version_app.engine.message_codec import encode_messages
    store = JsonlEventStore(tmp_path/"runtime")
    threads = JsonThreadStore(tmp_path/"runtime")
    old = AgentRuntime(services=SimpleNamespace(),tool_runtime=SimpleNamespace(),system_prompt="",
                       event_store=store,thread_store=threads,turn_executor=lambda *a,**k:None)
    record=old.start_thread()
    observer=RuntimeLoopObserver(store.append,record.thread_id,"turn_original")
    message=HumanMessage(content="ORIGINAL_REQUIREMENT_BEFORE_OLD_COMPACTION")
    annotate_messages([message])
    observer.message_appended(message)
    store.append(RuntimeEvent(thread_id=record.thread_id,kind=EventKind.CONTEXT_COMPACTED,
                              payload={"messages":encode_messages([HumanMessage(content="lossy old summary")])}))
    def factory(tid):
        return SimpleNamespace(session_memory=manager(tmp_path,tid)),SimpleNamespace(),""
    upgraded=AgentRuntime(services=SimpleNamespace(),tool_runtime=SimpleNamespace(),system_prompt="",
                          event_store=store,thread_store=threads,turn_executor=lambda *a,**k:None,
                          thread_context_factory=factory)
    upgraded.resume_thread(record.thread_id)
    memory=upgraded.thread_context(record.thread_id)[0].session_memory
    assert "ORIGINAL_REQUIREMENT_BEFORE_OLD_COMPACTION" in memory.search("ORIGINAL_REQUIREMENT_BEFORE_OLD_COMPACTION")


def test_failed_manifest_commit_does_not_claim_coverage(tmp_path):
    from final_version_app.application import skill_memory as module
    memory=manager(tmp_path)
    original=module.atomic_text
    def failing(path,text):
        if path.name=="index.json":
            raise OSError("simulated disk full")
        return original(path,text)
    messages=[HumanMessage(content="must not drop this")]
    with patch.object(module,"atomic_text",side_effect=failing):
        with pytest.raises(OSError):
            memory.observe(messages)
    assert memory.state["messages"] == {}
    memory.observe(messages)
    assert len(manager(tmp_path).state["messages"]) == 1



def test_background_summary_single_flight_does_not_block_archive(tmp_path):
    from threading import Event
    memory = SkillMemoryManager(tmp_path/"skills","thr_background",auto_summary=True)
    entered, release = Event(), Event()
    calls=[]
    def summarize():
        calls.append(True)
        entered.set()
        release.wait(2)
    messages=[HumanMessage(content=("constraint "+str(i)+" ")*40) for i in range(10)]
    with patch.object(memory,"summarize_pending",side_effect=summarize):
        memory.maybe_schedule_extraction(messages)
        assert entered.wait(1)
        memory.maybe_schedule_extraction(messages+[HumanMessage(content="new while summary running")])
        assert len(calls)==1
        assert len(memory.state["messages"])==11
        release.set()
        memory.wait_for_extraction(2)
    assert "new while summary running" in memory.search("running")



def test_memory_page_preserves_json_and_middle_evidence():
    content=json.dumps({"text":"head"+"x"*5000+"MIDDLE_EVIDENCE"+"y"*5000,"next_offset":12000})
    messages=[ToolMessage(content=content,tool_call_id="memory",
                          additional_kwargs={"memory_page":True})]
    apply_tool_result_budget(messages)
    parsed=json.loads(messages[0].content)
    assert "MIDDLE_EVIDENCE" in parsed["text"]
    assert parsed["next_offset"]==12000


def test_compact_header_does_not_hide_first_source(tmp_path):
    memory=manager(tmp_path)
    memory.observe([HumanMessage(content="FIRST_SOURCE_REQUIREMENT")] +
                   [HumanMessage(content="filler "+str(i)) for i in range(60)])
    episode=memory.state["episodes"][0]
    text=json.loads(memory.read(episode["id"],limit=3000))["text"]
    assert "FIRST_SOURCE_REQUIREMENT" in text.split("## Source messages",1)[1]



def test_legacy_summary_is_not_promoted_to_user_decision(tmp_path):
    memory=manager(tmp_path)
    genuine=HumanMessage(content="<session-memory> quoted by actual user",
                         additional_kwargs={"runtime_turn_id":"turn_real"})
    memory.observe([HumanMessage(content="<session-memory> synthetic old summary"),genuine])
    assert len(memory.state["messages"]) == 1
    assert "quoted by actual user" in memory.search("quoted")



def test_atomic_write_near_windows_path_limit(tmp_path):
    from final_version_app.application.skill_memory import atomic_text
    padding=max(1,210-len(str(tmp_path.resolve()))-1)
    directory=tmp_path/("d"*padding)
    target=directory/"long-target-file-name-for-memory.md"
    atomic_text(target,"durable source")
    assert target.read_text(encoding="utf-8")=="durable source"
    assert list(directory.glob(".tmp-*"))==[]

