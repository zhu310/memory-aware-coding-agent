from final_version_app.domain import research

def test_chinese_charset_links_and_paginated_long_lines(tmp_path,monkeypatch):
    monkeypatch.setattr(research,'WORKDIR',tmp_path)
    body=('<html><p>中文资料</p><a href="/next">下一页</a><script>secret instruction</script><p>'+'甲'*40000+'末尾证据</p></html>').encode('gb18030')
    monkeypatch.setattr(research,'fetch_public',lambda url:{'url':url,'content_type':'text/html; charset=gb18030','content':body})
    result=research.read_web('https://example.com/article');assert '中文资料' in result['text'] and 'secret instruction' not in result['text']
    assert 'https://example.com/next' in result['links']
    parts=[result['text']];seen=[]
    while result['next_line']:
        assert result['next_line'] not in seen;seen.append(result['next_line'])
        result=research.read_web('https://example.com/article',result['next_line']);parts.append(result['text'])
    assert '末尾证据' in ''.join(parts)

def test_search_failure_metadata_and_cache(monkeypatch):
    import httpx
    research._cache.clear();research._calls.clear()
    monkeypatch.setenv('SEARXNG_URL','http://search')
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args,**kwargs):return httpx.Response(200,request=httpx.Request('GET','http://search/search'),json={'results':[{'title':'Official','url':'https://example.com','content':'Source'}],'unresponsive_engines':[['engine','CAPTCHA']]})
    monkeypatch.setattr(research.httpx,'Client',Client)
    first=research.search_web('test');second=research.search_web('test')
    assert first['unresponsive_engines']==[['engine','CAPTCHA']]
    assert not first['cached'] and second['cached']
