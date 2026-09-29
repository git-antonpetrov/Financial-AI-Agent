import jwt, datetime, urllib.request

token = jwt.encode({'sub': 'admin', 'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)}, 'T2vN8yP4kL6bZ0xS3dG9mK1jH7rW5cF1T2vN8yP4kL6bZ0xS3dG9mK1jH7rW5cF1', algorithm='HS256')
req = urllib.request.Request('https://admin.fin-ai-agent.ru/api/agent-requests', headers={'Authorization': 'Bearer ' + token})
try:
    with urllib.request.urlopen(req) as res:
        print(res.read().decode())
except Exception as e:
    print(e)
