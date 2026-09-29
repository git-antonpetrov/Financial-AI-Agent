import urllib.request
import json
import urllib.error
import os
from dotenv import load_dotenv

load_dotenv()

url = 'https://admin.fin-ai-agent.ru/api/agents/requests'
requests = [
    {
        'agent_name': 'bank',
        'document_name_ru': 'Новые правила ЦБ РФ 2024 (Указание Банка России)',
        'document_name_en': 'New Rules of the Central Bank of the Russian Federation 2024',
        'justification_ru': 'Нам необходимо обновить базу знаний в связи с новыми указаниями ЦБ по валютным операциям для корректных ответов клиентам.',
        'justification_en': 'We need to update the knowledge base due to new CBR instructions on currency operations for correct client responses.',
        'token': os.getenv('AGENT_BANK_BOOTSTRAP_TOKEN', '<BANK_TOKEN>')
    },
    {
        'agent_name': 'invest',
        'document_name_ru': 'Отчет Сбербанка за 2023 (МСФО)',
        'document_name_en': 'Sberbank 2023 IFRS Report',
        'justification_ru': 'Требуется финансовый отчет Сбербанка для анализа дивидендной политики и формирования инвест-идей на следующий квартал.',
        'justification_en': 'Sberbank financial report is required for dividend policy analysis and generating investment ideas for the next quarter.',
        'token': os.getenv('AGENT_INVEST_BOOTSTRAP_TOKEN', '<INVEST_TOKEN>')
    },
    {
        'agent_name': 'digital',
        'document_name_ru': 'Технические требования к API НСПК v4',
        'document_name_en': 'NSPK API v4 Technical Requirements',
        'justification_ru': 'Обновленная документация Национальной Системы Платежных Карт для интеграции новых методов оплаты по СБП.',
        'justification_en': 'Updated documentation of the National Payment Card System for integrating new SBP payment methods.',
        'token': os.getenv('AGENT_DIGITAL_BOOTSTRAP_TOKEN', '<DIGITAL_TOKEN>')
    }
]

for req in requests:
    data = json.dumps({
        'agent_name': req['agent_name'],
        'document_name_ru': req['document_name_ru'],
        'document_name_en': req['document_name_en'],
        'justification_ru': req['justification_ru'],
        'justification_en': req['justification_en']
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
