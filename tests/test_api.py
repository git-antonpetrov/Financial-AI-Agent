import os, jwt, datetime, urllib.request

jwt_secret = os.getenv("JWT_SECRET_KEY")
if not jwt_secret:
    raise ValueError("Переменная окружения JWT_SECRET_KEY не задана")

token = jwt.encode({'sub': 'admin', 'exp': datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)}, jwt_secret, algorithm='HS256')
req = urllib.request.Request('https://admin.fin-ai-agent.ru/api/agent-requests', headers={'Authorization': 'Bearer ' + token})
try:
    with urllib.request.urlopen(req) as res:
        print(res.read().decode())
except Exception as e:
    print(e)
