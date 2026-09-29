import urllib.request
import json
import urllib.error
import os

url = 'https://admin.fin-ai-agent.ru/api/agents/requests'
requests = [
    {
        'agent_name': 'bank',
        'document_name': 'Новые правила ЦБ РФ 2024 (Указание Банка России)',
        'justification': 'Нам необходимо обновить базу знаний в связи с новыми указаниями ЦБ по валютным операциям для корректных ответов клиентам.',
        'token': os.getenv('AGENT_BANK_BOOTSTRAP_TOKEN', '<BANK_TOKEN>')
    },
    {
        'agent_name': 'invest',
        'document_name': 'Отчет Сбербанка за 2023 (МСФО)',
        'justification': 'Требуется финансовый отчет Сбербанка для анализа дивидендной политики и формирования инвест-идей на следующий квартал.',
        'token': os.getenv('AGENT_INVEST_BOOTSTRAP_TOKEN', '<INVEST_TOKEN>')
    },
    {
        'agent_name': 'digital',
        'document_name': 'Технические требования к API НСПК v4',
        'justification': 'Обновленная документация Национальной Системы Платежных Карт для интеграции новых методов оплаты по СБП.',
        'token': os.getenv('AGENT_DIGITAL_BOOTSTRAP_TOKEN', '<DIGITAL_TOKEN>')
    }
]

for req in requests:
    data = json.dumps({
        'agent_name': req['agent_name'],
        'document_name': req['document_name'],
        'justification': req['justification']
    }).encode('utf-8')
    
    headers = {
        'Content-Type': 'application/json',
        'authorization': f"Bearer {req['token']}"
    }
    
    request = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request) as response:
            res_data = response.read().decode('utf-8')
            print(f"Successfully added request for {req['agent_name']}")
    except urllib.error.HTTPError as e:
        print(f"Failed to add request for {req['agent_name']}")
