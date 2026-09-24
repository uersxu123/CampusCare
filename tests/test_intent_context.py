import json

from app.services.intent_prompts import build_intent_prompt


def test_intent_prompt_uses_one_json_boundary_and_current_turn():
    messages = build_intent_prompt(
        {
            "summary_version": 3,
            "current_goal": {"text": "了解国家奖学金申请条件"},
            "active_topics": ["国家奖学金", "申请条件"],
            "recent_messages": [{"id": 12, "role": "user", "content": "国家奖学金怎么申请？"}],
            "clarification_state": None,
        },
        "那研究生呢？",
    )
    prompt = "\n".join(message.content for message in messages)
    assert "ROUTE_INPUT_JSON:" in prompt
    assert '"currentInput":"那研究生呢？"' in prompt
    assert prompt.count("ROUTE_INPUT_JSON:") == 1
    payload = json.loads(prompt.split("ROUTE_INPUT_JSON:", 1)[1])
    assert payload["currentInput"] == "那研究生呢？"
    assert payload["contextView"]["current_goal"]["text"] == "了解国家奖学金申请条件"
    assert payload["contextView"]["recent_messages"][0]["id"] == 12
    assert payload["sourceCatalog"] == [{"id": "current:0", "text": "那研究生呢？"}]
    assert "国家奖学金申请条件" in prompt
    assert "历史上下文不得自行生成新的 WorkItem" in messages[0].content
    assert "currentInput 是本轮唯一需要路由的用户输入" in messages[0].content


def test_intent_prompt_declares_transform_payload_boundary():
    messages = build_intent_prompt({}, '把“我最近失眠睡不着”改写得更正式。')
    prompt = "\n".join(message.content for message in messages)
    assert "文本变换边界" in prompt
    assert "被处理文本里出现 ACADEMIC/CAMPUS/MENTAL 主题词不能改变这个判断" in prompt
    assert '把“我最近失眠睡不着”改写得更正式' in prompt
    assert '翻译“国家助学金申请条件”' in prompt
