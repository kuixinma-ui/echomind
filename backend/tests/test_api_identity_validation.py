"""Reject non-UTF8 identity fields without echoing them in validation responses."""
import asyncio
import json

import httpx
import pytest


@pytest.mark.parametrize('user,conv', [('alice\ud800','same'),('alice','same\udfff')])
def test_chat_rejects_invalid_identity_before_service_and_returns_safe_422(monkeypatch,user,conv):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED','1')
    import api.main as main
    seen=[]
    class Service:
        async def chat(self,message,user_id,conv_id):
            seen.append((user_id,conv_id))
            return {'conv_id':'safe','response':'ok','intent':'query','agent_type':'general',
                    'escalated':False,'latency_ms':0}
    monkeypatch.setattr(main,'_chat_service',Service())
    async def run():
        transport=httpx.ASGITransport(app=main.app,raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
            body=json.dumps({'message':'hello','user_id':user,'conv_id':conv},ensure_ascii=True).encode('ascii')
            response=await client.post('/chat',content=body,headers={'Content-Type':'application/json'})
        assert response.status_code == 422
        assert 'Unicode' in response.json()['detail']
        assert seen == []
    asyncio.run(run())


def test_chat_keeps_valid_unicode_colon_and_none_conversation(monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED','1')
    import api.main as main
    seen=[]
    class Service:
        async def chat(self,message,user_id,conv_id):
            seen.append((user_id,conv_id))
            return {'conv_id':'safe','response':'ok','intent':'query','agent_type':'general',
                    'escalated':False,'latency_ms':0}
    monkeypatch.setattr(main,'_chat_service',Service())
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
            response=await client.post('/chat',json={'message':'hello','user_id':'用户:甲😀','conv_id':None})
        assert response.status_code == 200
        assert seen == [('用户:甲😀',None)]
    asyncio.run(run())
