import requests
import time
import uuid

BASE_URL = "http://localhost:8002"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"
RECEIVER_ID = "22222222-2222-2222-2222-222222222222"

def print_test(name):
    print(f"\n[{name}]")

def test_api():
    print("Ожидание запуска API...")
    for _ in range(10):
        try:
            requests.get(f"{BASE_URL}/docs")
            break
        except requests.ConnectionError:
            time.sleep(2)
    
    # 1. Банк - Получение счетов
    print_test("GET /bank/accounts/by-client/{client_id}")
    res = requests.get(f"{BASE_URL}/bank/accounts/by-client/{CLIENT_ID}")
    assert res.status_code == 200, res.text
    accounts = res.json()
    print(f"Найдено {len(accounts)} счетов для клиента 1.")
    
    if len(accounts) == 0:
        print("Счета не найдены. Сидер мог не завершиться или БД пуста.")
        return
        
    main_account_id = accounts[0]["id"]
    print(f"Главный счет: {main_account_id}, Баланс: {accounts[0]['balance']}")

    # 2. Банк - Выпуск карты
    print_test("POST /bank/accounts/{account_id}/cards/issue")
    res = requests.post(f"{BASE_URL}/bank/accounts/{main_account_id}/cards/issue")
    assert res.status_code == 200, res.text
    new_card = res.json()
    print(f"Выпущена новая карта: {new_card['card_number']} со статусом {new_card['status']}")

    # 3. Банк - Перевод денег
    print_test("POST /bank/transfers")
    # Получаем счет получателя
    res = requests.get(f"{BASE_URL}/bank/accounts/by-client/{RECEIVER_ID}")
    receiver_accounts = res.json()
    receiver_account_id = receiver_accounts[0]["id"]
    
    transfer_data = {
        "from_account_id": main_account_id,
        "amount": 50.0,
        "transfer_type": "account",
        "destination": receiver_accounts[0]["account_number"],
        "description": "Тестовый перевод"
    }
    res = requests.post(f"{BASE_URL}/bank/transfers", json=transfer_data)
    assert res.status_code == 200, res.text
    print(f"Перевод успешен: {res.json()}")

    # 4. Инвест - Открытие вклада
    print_test("POST /invest/deposits/open")
    deposit_data = {
        "client_id": CLIENT_ID,
        "from_bank_account_id": main_account_id,
        "initial_amount": 1000.0,
        "term_months": 12
    }
    res = requests.post(f"{BASE_URL}/invest/deposits/open", json=deposit_data)
    assert res.status_code == 200, res.text
    deposit = res.json()
    deposit_id = deposit["id"]
    print(f"Открыт вклад: {deposit['account_number']} с балансом {deposit['balance']}")

    # 5. Инвест - Закрытие вклада
    print_test(f"POST /invest/deposits/{deposit_id}/close")
    close_data = {
        "to_bank_account_id": main_account_id
    }
    res = requests.post(f"{BASE_URL}/invest/deposits/{deposit_id}/close", json=close_data)
    assert res.status_code == 200, res.text
    print(f"Вклад закрыт, возвращено: {res.json()['returned_amount']}")

    # 6. Цифровой рубль - Получение кошелька
    print_test("GET /ruble/wallets/{client_id}")
    # Проверка наличия кошелька
    res = requests.get(f"{BASE_URL}/ruble/wallets/{CLIENT_ID}")
    
    if res.status_code == 404:
        print("Кошелек ЦР не найден (возможно, сидер не создал его).")
    else:
        wallet = res.json()
        print(f"Найден кошелек ЦР: {wallet['wallet_number']}, баланс: {wallet['balance']}")
        wallet_id = wallet["id"]
        
        # 7. Цифровой рубль - Пополнение кошелька
        print_test("POST /ruble/wallets/{wallet_id}/fund")
        fund_data = {
            "bank_account_id": main_account_id,
            "amount": 200.0
        }
        res = requests.post(f"{BASE_URL}/ruble/wallets/{wallet_id}/fund", json=fund_data)
        assert res.status_code == 200, res.text
        print("Кошелек успешно пополнен.")
        
    print("\n✅ Все интеграционные тесты пройдены успешно!")

if __name__ == "__main__":
    test_api()
