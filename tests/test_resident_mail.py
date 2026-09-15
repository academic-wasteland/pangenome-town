import json
from types import SimpleNamespace

import pytest

from pangenome_town import mail
from pangenome_town.exchange import Envelope


def test_named_resident_delivery_requires_gas_city_receipt(towns,monkeypatch):
    message=Envelope.new('question','yamatai','ubar',{'text':'wizard hat'})
    monkeypatch.setattr(mail,'gc_binary',lambda:'/usr/bin/gc')
    monkeypatch.setattr(mail.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=''))
    with pytest.raises(mail.MailError,match='receipt'):
        mail.send_to_resident(towns['ubar'],message,'bloodninja')
    wake_requests=[]
    class WakeResponse:
        status=202
        def __enter__(self): return self
        def __exit__(self,*args): pass
    def wake(request,**kwargs):
        wake_requests.append(request)
        return WakeResponse()
    monkeypatch.setattr(mail.urllib.request,'urlopen',wake)
    calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'id':'mail-1'}))
    monkeypatch.setattr(mail.subprocess,'run',run)
    assert mail.send_to_resident(towns['ubar'],message,'bloodninja')['id']=='mail-1'
    body=calls[0][calls[0].index('-m')+1]
    assert 'pangenome-town send --to yamatai --reply-to '+message.id in body
    assert 'pangenome-town answer' not in body
    assert wake_requests[0].full_url.endswith('/v0/city/ubar/session/bloodninja/submit')
    assert json.loads(wake_requests[0].data)['intent']=='default'
    def unavailable(*args,**kwargs): raise OSError('offline')
    monkeypatch.setattr(mail.urllib.request,'urlopen',unavailable)
    receipt=mail.send_to_resident(towns['ubar'],message,'bloodninja')
    assert receipt['id']=='mail-1' and receipt['wake_requested'] is False
