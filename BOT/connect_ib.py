# connect_ib.py
# בדיקת חיבור ל-Interactive Brokers עם ריטריי, timeout מורחב ודיאגנוסטיקה
# שימוש נכון ב-asyncio.sleep בתוך קוד אסינכרוני (ללא ib/util.sleep)

from ib_insync import IB, util
import asyncio
from typing import Tuple, Optional

HOST = '127.0.0.1'
# סדר ניסיונות: Gateway Paper, Gateway Live, TWS Paper, TWS Live
PORT_CANDIDATES = [4002, 4001, 7497, 7496]
CLIENT_ID = 1
CONNECT_TIMEOUT = 30  # כדי למנוע Timeout בזמן handshake איטי


def advice_for_handshake_timeout(port: int) -> str:
    return (
        "\n📋 Checklist לתיקון Handshake Timeout:\n"
        f"- ודא שאתה באמת על {'IB Gateway' if port in (4001, 4002) else 'TWS'} ומחובר (לא במסך לוגין), בפרופיל הנכון (Paper/Live).\n"
        "- Configure > API settings:\n"
        "  • Enable ActiveX and Socket Clients = ✔️\n"
        "  • Trusted IPs כוללים 127.0.0.1 (או ריק לזמן בדיקה)\n"
        "  • בטל חלונות קופצים (FYI / Bulletin / Agreements)\n"
        "- ודא שהפורט נכון: Gateway 4002/4001 | TWS 7497/7496.\n"
        "- נסה CLIENT_ID אחר (למשל 2 או 9) למניעת התנגשויות.\n"
        "- אשר ב-Firewall/AV את IB/TWS ואת פרויקט פייתון.\n"
    )


def safe_connection_info(ib: IB) -> str:
    """
    מחזיר מחרוזת מידע על חיבור בצורה תואמת-גרסאות:
    משתמש ב-connectionStats() אם קיים, אחרת מציג ServerVersion בלבד.
    """
    sv = ib.client.serverVersion()
    stats = getattr(ib.client, "connectionStats", None)
    if callable(stats):
        try:
            cs = ib.client.connectionStats()
            start = getattr(cs, "startDateTime", None) or getattr(cs, "startTime", None)
            return f"ServerVersion={sv} | Start={start}"
        except Exception:
            pass
    return f"ServerVersion={sv}"


async def try_connect_once(ib: IB, host: str, port: int, client_id: int) -> Tuple[bool, Optional[str]]:
    """
    מנסה התחברות אחת עם timeout מוגדל. מחזיר (הצליח?, הודעת שגיאה אם יש).
    """
    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=CONNECT_TIMEOUT)
        # אם הגענו לכאן, ה-handshake הושלם. נוודא תקשורת מול השרת:
        try:
            server_time = await asyncio.wait_for(ib.reqCurrentTimeAsync(), timeout=5)
            print(f"⏱️ Server time (UTC): {server_time}")
        except asyncio.TimeoutError:
            print("⚠️ Connected, אך לא התקבל זמן שרת תוך 5s (נמשיך).")
        return True, None
    except asyncio.TimeoutError:
        return False, f"Handshake timeout אחרי {CONNECT_TIMEOUT}s בפורט {port}."
    except ConnectionRefusedError:
        return False, f"Connection refused בפורט {port}."
    except Exception as e:
        return False, f"Unexpected error בפורט {port}: {e!r}"


async def main():
    util.logToConsole()
    ib = IB()

    print(f"Attempting to connect to IB on {HOST} (clientId={CLIENT_ID})…")
    last_err = None
    used_port = None

    for port in PORT_CANDIDATES:
        print(f"→ Trying port {port} …")
        ok, err = await try_connect_once(ib, HOST, port, CLIENT_ID)
        if ok:
            used_port = port
            print(f"✅ Connected on {HOST}:{port} | {safe_connection_info(ib)}")
            break
        else:
            print(f"❌ {err}")
            last_err = (port, err)

    if not ib.isConnected():
        print("\n--- Connection failed on all ports ---")
        if last_err:
            port, err = last_err
            print(err)
            print(advice_for_handshake_timeout(port))
        else:
            print("No specific error captured.")
        return

    # מחוברים — נביא Account Summary בסיסי
    try:
        await ib.reqAccountSummaryAsync()
        await asyncio.sleep(2)  # לאפשר לעדכונים להגיע

        accounts = ib.managedAccounts()
        print(f"Accounts: {accounts}")
        if not accounts:
            print("⚠️ אין חשבונות. בדוק פרופיל/לוגין ב-Gateway/TWS.")
        else:
            account = accounts[0]
            # <<< התיקון כאן: גישה ישירה לנתונים במקום קריאה לפונקציה סינכרונית
            summary = [v for v in ib.accountValues() if v.account == account]

            if not summary:
                print("…Waiting briefly for account summary…")
                await asyncio.sleep(1.5)
                summary = [v for v in ib.accountValues() if v.account == account]

            def get(tag, default="Not available"):
                for item in summary:
                    if item.tag == tag:
                        try:
                            return f"${float(item.value):,.2f}"
                        except Exception:
                            return item.value
                return default

            print("\n--- Account Summary ---")
            print(f"Account ID: {account}")
            print(f"NetLiquidation: {get('NetLiquidation')}")
            print(f"AvailableFunds: {get('AvailableFunds')}")
            print(f"ExcessLiquidity: {get('ExcessLiquidity')}")
            print(f"BuyingPower: {get('BuyingPower')}")
            print("-----------------------")

    finally:
        if ib.isConnected():
            print("Disconnecting…")
            ib.disconnect()
            print("Disconnected.")


if __name__ == "__main__":
    util.run(main())