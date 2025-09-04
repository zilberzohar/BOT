# ==============================================================================
# File: notification_manager.py (Updated)
# ==============================================================================
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config_live as config


def send_test_email():
    """
    Connects to the SMTP server and sends a test email to verify credentials.
    Returns a tuple (success: bool, message: str).
    """
    try:
        if 'YOUR_APP_PASSWORD_HERE' in config.EMAIL_PASSWORD:
            return False, "Please replace 'YOUR_APP_PASSWORD_HERE' in config_live.py with your Gmail App Password."

        msg = MIMEMultipart()
        msg['From'] = config.EMAIL_SENDER
        msg['To'] = config.EMAIL_RECEIVER
        msg['Subject'] = "Trading Bot - Test Email"
        body = "This is a test email from your trading bot. If you received this, the email configuration is correct."
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECEIVER, msg.as_string())

        return True, "Test email sent successfully!"

    except smtplib.SMTPAuthenticationError:
        return False, "SMTP Authentication Error: Check your email or App Password in config_live.py."
    except Exception as e:
        return False, f"Failed to send email: {e}"


def send_trade_notification(trade_summary: dict):
    """
    Sends a detailed notification about a completed trade.
    """
    try:
        pnl_amount = trade_summary['pnl_amount']
        pnl_percent = trade_summary['pnl_percent']
        result = "Profit ✅" if pnl_amount > 0 else "Loss ❌"

        subject = f"Trade Closed: {result} on {trade_summary['ticker']}"

        body = f"""
        Hello,

        Your trading bot has just closed a position.

        --- Trade Summary ---
        - Ticker: {trade_summary['ticker']}
        - Direction: {trade_summary['direction']}
        - Entry Time: {trade_summary['entry_time']}
        - Entry Price: ${trade_summary['entry_price']:.2f}
        - Exit Time: {trade_summary['exit_time']}
        - Exit Price: ${trade_summary['exit_price']:.2f}
        - Quantity: {trade_summary['quantity']}
        - Exit Reason: {trade_summary['exit_reason']}

        --- Performance ---
        - Profit/Loss: ${pnl_amount:.2f} ({pnl_percent:.2f}%)
        - Result: {result}

        Regards,
        The Algo Trading Bot
        """

        msg = MIMEMultipart()
        msg['From'] = config.EMAIL_SENDER
        msg['To'] = config.EMAIL_RECEIVER
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            server.sendmail(config.EMAIL_SENDER, config.EMAIL_RECEIVER, msg.as_string())

        print(f"Successfully sent trade notification for {trade_summary['ticker']}")
        return True, "Trade notification sent."

    except Exception as e:
        print(f"Failed to send trade notification: {e}")
        return False, f"Failed to send trade notification: {e}"
