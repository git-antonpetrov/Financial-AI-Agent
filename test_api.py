import jwt, datetime, urllib.request

token = jwt.encode({'sub': 'admin', 'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)}, '[REDACTED_JWT_SECRET]', algorithm='HS256')
req = urllib.request.Request('https://admin.fin-ai-agent.ru/api/agent-requests', headers={'Authorization': 'Bearer ' + token})
try:
    with urllib.request.urlopen(req) as res:
        print(res.read().decode())
except Exception as e:
    print(e)
