"""Local-only sender for authenticated, fixed-target preview transfers."""
from pathlib import Path
import http.client,json,tempfile
from preview_transfer import export_preview


def send_preview(job, target, username, password, progress=lambda **kwargs:None):
    status=json.loads((job/'status.json').read_text(encoding='utf-8'))
    if status['state']!='completed' or not (job/'output/preview.html').is_file():raise ValueError('请先完成重建')
    host,port=target
    with tempfile.TemporaryDirectory(prefix='ct-send-',dir=job/'cache') as temp:
        package=Path(temp)/'preview.zip';progress(stage='packaging',percent=0)
        connection=http.client.HTTPConnection(host,port,timeout=900)
        try:
            body=json.dumps({'username':username,'password':password}).encode()
            connection.request('POST','/api/auth/login',body,{'X-Local-App':'1','Content-Type':'application/json'})
            response=connection.getresponse();cookie=(response.getheader('Set-Cookie') or '').split(';')[0];response.read()
            if response.status!=200 or not cookie.startswith('ct_session='):raise ValueError('服务器账号或密码不正确')
            connection.request('GET','/api/auth/session',headers={'Cookie':cookie});response=connection.getresponse();raw=response.read()
            if response.status!=200:raise ValueError('服务器登录状态不可用')
            csrf=json.loads(raw)['csrf']
            connection.request('GET','/api/activity',headers={'Cookie':cookie});response=connection.getresponse();raw=response.read()
            if response.status!=200:raise ValueError('无法检查服务器任务状态')
            if json.loads(raw).get('busy'):raise ValueError('服务器正在处理检查，请稍后上传')
            export_preview(job/'output',package,{'source_id':status.get('transfer_source_id',job.name),'note_name':status.get('note_name','')})
            total=package.stat().st_size
            connection.putrequest('POST','/api/import-preview');connection.putheader('Cookie',cookie);connection.putheader('X-Local-App','1');connection.putheader('X-CSRF-Token',csrf);connection.putheader('Content-Type','application/octet-stream');connection.putheader('Content-Length',str(total));connection.endheaders()
            sent=0;progress(stage='uploading',percent=0)
            with package.open('rb') as source:
                while block:=source.read(1024**2):
                    connection.send(block);sent+=len(block);progress(stage='uploading',percent=int(sent*100/total))
            progress(stage='importing',percent=100);response=connection.getresponse();raw=response.read(8192)
            try:result=json.loads(raw)
            except (ValueError,UnicodeDecodeError):raise ValueError('服务器返回异常，请稍后重试') from None
            if response.status!=200:raise ValueError(str(result.get('error','服务器接收失败'))[:200])
            return result
        finally:connection.close()
