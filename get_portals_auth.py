import asyncio
from aportalsmp.auth import update_auth

async def main():
    # Вставьте сюда ваши api_id и api_hash с my.telegram.org
    API_ID = 35446672
    API_HASH = "b941cbdc28c785221561472458ad01a5"

    print("Авторизация в Telegram и получение authData...")
    try:
        auth_data = await update_auth(api_id=API_ID, api_hash=API_HASH)
        print(f"\n✅ ВАШ authData для Portals:\n{auth_data}")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
